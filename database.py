"""
Configuração do banco de dados SQLite + SQLAlchemy 2.x.

- PRAGMA foreign_keys=ON  -> garante ON DELETE CASCADE / SET NULL no SQLite.
- PRAGMA journal_mode=WAL -> melhor concorrência leitura/escrita.
- DecimalType            -> armazena Decimal de forma EXATA (inteiro escalado),
                            evitando erros de ponto flutuante do SQLite.
"""
from __future__ import annotations

import os
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

BASE_DIR = Path(__file__).resolve().parent
# banco: SLOTTA_DB_URL (ou NEOKIOSKI_DB_URL, o nome antigo); sem ela, slotta.db na pasta do app — ou neokioski.db, se
# existir (instalação de antes da troca do nome: o banco continua o mesmo)
_DB_FILE = BASE_DIR / ("neokioski.db" if (BASE_DIR / "neokioski.db").exists() and not (BASE_DIR / "slotta.db").exists()
                       else "slotta.db")
DATABASE_URL = os.getenv("SLOTTA_DB_URL") or os.getenv("NEOKIOSKI_DB_URL") or f"sqlite:///{_DB_FILE.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


def fold_text(value) -> str:
    """Normaliza texto para busca: sem acentos e minúsculo ('Açúcar' -> 'acucar')."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    # Função SQL usada nas buscas: ignora acentos e maiúsculas (o LIKE do SQLite só trata ASCII).
    dbapi_connection.create_function("nk_fold", 1, fold_text, deterministic=True)
    # Código de endereço para comparar (zeros à esquerda conforme a configuração; não é determinística: muda com ela)
    from services.app_settings import sql_canon

    dbapi_connection.create_function("nk_canon", 1, sql_canon)
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class DecimalType(TypeDecorator):
    """Decimal exato persistido como inteiro escalado (padrão: 4 casas decimais).

    Funciona com comparações (>, <, =), ORDER BY e func.sum() diretamente no SQL,
    pois todos os valores compartilham a mesma escala.
    """

    impl = BigInteger
    cache_ok = True

    def __init__(self, scale: int = 4, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scale = scale
        self._quant = Decimal(1).scaleb(-scale)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        try:
            dec = value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation as exc:  # pragma: no cover - proteção
            raise ValueError(f"Valor decimal inválido: {value!r}") from exc
        return int(dec.scaleb(self.scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(int(value)).scaleb(-self.scale).quantize(self._quant)

    @property
    def python_type(self):
        return Decimal


def get_db() -> Iterator[Session]:
    """Dependência FastAPI: uma sessão por requisição."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_location_bins(conn) -> None:
    """Versões anteriores: location_bins sem shelf_level_id e com unicidade (endereço, tipo).
    Reconstrói a tabela no formato novo preservando os dados (bins ficam no nível próprio)."""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if "location_bins" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("location_bins")}
    if "shelf_level_id" in cols:
        return
    conn.execute(text("ALTER TABLE location_bins RENAME TO location_bins_old"))
    for idx in insp.get_indexes("location_bins_old"):
        if idx.get("name"):
            conn.execute(text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
    for idx_name in ("ix_location_bins_location_id", "ix_location_bins_bin_type_id"):
        conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))
    Base.metadata.tables["location_bins"].create(conn)
    conn.execute(text(
        "INSERT INTO location_bins (id, location_id, bin_type_id, quantity, shelf_level_id, created_at, updated_at) "
        "SELECT id, location_id, bin_type_id, quantity, NULL, created_at, updated_at FROM location_bins_old "
        "WHERE location_id IN (SELECT id FROM storage_locations) AND bin_type_id IN (SELECT id FROM bin_types)"
    ))
    conn.execute(text("DROP TABLE location_bins_old"))


_CAPS_MIGRATED = False  # True quando a coluna calc_bins acabou de ser criada (migração da capacidade dos níveis)


def _add_missing_columns(conn) -> None:
    """Colunas novas em tabelas existentes (SQLite: ALTER TABLE ADD COLUMN)."""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    tables = insp.get_table_names()
    if "shelf_levels" in tables and "height_cm" not in {c["name"] for c in insp.get_columns("shelf_levels")}:
        conn.execute(text("ALTER TABLE shelf_levels ADD COLUMN height_cm BIGINT"))
    pcols = {c["name"] for c in insp.get_columns("products")} if "products" in tables else set()
    # min/max level ficavam no produto (versões anteriores; agora na política de estoque por filial): a coluna é
    # criada em bancos bem antigos só para a migração de políticas encontrar o formato esperado
    for col in ("min_level", "max_level"):
        if "products" in tables and col not in pcols and "default_location_id" in pcols:
            conn.execute(text(f"ALTER TABLE products ADD COLUMN {col} BIGINT"))
    btcols = {c["name"] for c in insp.get_columns("bin_types")} if "bin_types" in tables else set()
    for col, ddl in (("parent_id", "INTEGER REFERENCES bin_types(id)"), ("fraction_num", "INTEGER"),
                     ("fraction_den", "INTEGER"), ("split_axis", "VARCHAR(10) NOT NULL DEFAULT 'width'"),
                     ("dims_auto", "BOOLEAN NOT NULL DEFAULT 0"), ("div_w", "INTEGER"), ("div_l", "INTEGER"),
                     ("cost", "BIGINT"), ("open_top", "BOOLEAN NOT NULL DEFAULT 0"), ("over_cm", "BIGINT"),
                     ("allow_split", "BOOLEAN NOT NULL DEFAULT 0"), ("store_as", "VARCHAR(10) NOT NULL DEFAULT 'units'")):
        if "bin_types" in tables and col not in btcols:
            conn.execute(text(f"ALTER TABLE bin_types ADD COLUMN {col} {ddl}"))
    if "bin_types" in tables and "div_w" not in btcols:
        # frações da versão anterior (1/n numa direção) viram grade n×1 ou 1×n
        conn.execute(text("UPDATE bin_types SET div_w = fraction_den / fraction_num, div_l = 1 "
                          "WHERE parent_id IS NOT NULL AND fraction_num > 0 AND split_axis != 'length'"))
        conn.execute(text("UPDATE bin_types SET div_w = 1, div_l = fraction_den / fraction_num "
                          "WHERE parent_id IS NOT NULL AND fraction_num > 0 AND split_axis = 'length'"))
    stc = {c["name"] for c in insp.get_columns("structure_types")} if "structure_types" in tables else set()
    for col, ddl in (("first_level_cm", "BIGINT"), ("level_heights", "JSON")):
        if "structure_types" in tables and col not in stc:
            conn.execute(text(f"ALTER TABLE structure_types ADD COLUMN {col} {ddl}"))
    if "bay_structures" in tables and "first_level_cm" not in {c["name"] for c in insp.get_columns("bay_structures")}:
        conn.execute(text("ALTER TABLE bay_structures ADD COLUMN first_level_cm BIGINT"))
    if "bay_structures" in tables and "cap_params" not in {c["name"] for c in insp.get_columns("bay_structures")}:
        conn.execute(text("ALTER TABLE bay_structures ADD COLUMN cap_params JSON"))
    if "structure_kinds" in tables and "shared_sides" not in {c["name"] for c in insp.get_columns("structure_kinds")}:
        conn.execute(text("ALTER TABLE structure_kinds ADD COLUMN shared_sides BOOLEAN NOT NULL DEFAULT 0"))
    lccols = {c["name"] for c in insp.get_columns("level_capacities")} if "level_capacities" in tables else set()
    if "level_capacities" in tables and "calc_bins" not in lccols:
        # capacidade calculada × limite manual: o que estava gravado vira limite manual; na inicialização, as
        # estantes com estrutura ganham a capacidade calculada e o limite igual ou acima dela é descartado
        conn.execute(text("ALTER TABLE level_capacities ADD COLUMN calc_bins INTEGER"))
        conn.execute(text("ALTER TABLE level_capacities ADD COLUMN manual_bins INTEGER"))
        conn.execute(text("UPDATE level_capacities SET manual_bins = max_bins"))
        global _CAPS_MIGRATED
        _CAPS_MIGRATED = True
    lcols = {c["name"] for c in insp.get_columns("storage_locations")} if "storage_locations" in tables else set()
    if "storage_locations" in tables and "is_generic" not in lcols:
        conn.execute(text("ALTER TABLE storage_locations ADD COLUMN is_generic BOOLEAN NOT NULL DEFAULT 0"))
    stcols = {c["name"] for c in insp.get_columns("structure_types")} if "structure_types" in tables else set()
    for col in ("golden_from", "golden_to"):
        if "structure_types" in tables and col not in stcols:
            conn.execute(text(f"ALTER TABLE structure_types ADD COLUMN {col} BIGINT"))
    if "structure_kind_pieces" in tables and "color" not in {c["name"] for c in insp.get_columns("structure_kind_pieces")}:
        conn.execute(text("ALTER TABLE structure_kind_pieces ADD COLUMN color VARCHAR(7)"))  # cor da peça no 3D
    kcols = {c["name"] for c in insp.get_columns("structure_kinds")} if "structure_kinds" in tables else set()
    for col, ddl in (("condition", "VARCHAR(20) NOT NULL DEFAULT ''"), ("door_type", "VARCHAR(10) NOT NULL DEFAULT ''"),
                     ("door_leaves", "INTEGER NOT NULL DEFAULT 1"), ("door_hinge", "VARCHAR(5) NOT NULL DEFAULT 'left'"),
                     ("door_angle", "INTEGER NOT NULL DEFAULT 90"), ("door_time_s", "BIGINT")):  # porta e condição
        if "structure_kinds" in tables and col not in kcols:
            conn.execute(text(f"ALTER TABLE structure_kinds ADD COLUMN {col} {ddl}"))
    pc2 = {c["name"] for c in insp.get_columns("products")} if "products" in tables else set()
    for col, ddl in (("store_pref", "VARCHAR(10) NOT NULL DEFAULT 'auto'"),
                     ("unitization", "VARCHAR(10) NOT NULL DEFAULT 'branch'"), ("mixed_packs", "BOOLEAN NOT NULL DEFAULT 0")):
        if "products" in tables and pc2 and col not in pc2:  # unitização do SKU
            conn.execute(text(f"ALTER TABLE products ADD COLUMN {col} {ddl}"))
    if "branches" in tables and "unitization" not in {c["name"] for c in insp.get_columns("branches")}:
        conn.execute(text("ALTER TABLE branches ADD COLUMN unitization VARCHAR(10) NOT NULL DEFAULT 'off'"))
    if "products" in tables and "storage_condition" not in {c["name"] for c in insp.get_columns("products")}:
        conn.execute(text("ALTER TABLE products ADD COLUMN storage_condition VARCHAR(20) NOT NULL DEFAULT ''"))
    if "structure_types" in tables and "kind_id" not in stcols:  # tipo de estrutura cadastrado
        conn.execute(text("ALTER TABLE structure_types ADD COLUMN kind_id INTEGER REFERENCES structure_kinds(id)"))
    for tbl, cols in (("bin_types", btcols), ("structure_types", stcols)):  # aparência no 3D
        if tbl in tables and "look_model" not in cols:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN look_model VARCHAR(20) NOT NULL DEFAULT ''"))
        if tbl in tables and "appearance" not in cols:
            conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN appearance JSON"))
    if "products" in tables and "material_type" not in {c["name"] for c in insp.get_columns("products")}:
        conn.execute(text("ALTER TABLE products ADD COLUMN material_type VARCHAR(60) NOT NULL DEFAULT ''"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_material_type ON products (material_type)"))


def _migrate_stock_policies(conn) -> None:
    """Versões anteriores guardavam endereço padrão, min e max level no PRODUTO (valia para todas as filiais).
    Agora ficam na política de estoque (produto × filial). Migração única (tabela de políticas vazia):
      - endereço padrão -> política da filial desse endereço;
      - min/max level -> políticas das filiais onde o produto tem endereço padrão ou saldo; sem nenhuma
        referência, em todas as filiais.
    As colunas antigas ficam no banco (não são mais usadas)."""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if "products" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("products")}
    if not {"default_location_id", "min_level", "max_level"} <= cols:
        return
    if conn.execute(text("SELECT COUNT(*) FROM stock_policies")).scalar():
        return
    rows = conn.execute(text("SELECT p.id, p.default_location_id, p.min_level, p.max_level, l.branch_id FROM products p "
                             "LEFT JOIN storage_locations l ON l.id = p.default_location_id "
                             "WHERE p.default_location_id IS NOT NULL OR p.min_level IS NOT NULL "
                             "OR p.max_level IS NOT NULL")).all()
    if not rows:
        return
    stock_br: dict[int, set] = {}
    for pid, bid in conn.execute(text("SELECT DISTINCT s.product_id, l.branch_id FROM stock_items s "
                                      "JOIN storage_locations l ON l.id = s.location_id")):
        stock_br.setdefault(pid, set()).add(bid)
    all_br = [b for (b,) in conn.execute(text("SELECT id FROM branches"))]
    for pid, loc, mn, mx, loc_br in rows:
        brs = set(stock_br.get(pid, set())) | ({loc_br} if loc_br else set())
        if not brs and (mn is not None or mx is not None):
            brs = set(all_br)
        for b in brs:
            conn.execute(text("INSERT INTO stock_policies (branch_id, product_id, default_location_id, min_level, "
                              "max_level, created_at, updated_at) VALUES (:b, :p, :l, :mn, :mx, CURRENT_TIMESTAMP, "
                              "CURRENT_TIMESTAMP)"),
                         {"b": b, "p": pid, "l": loc if b == loc_br else None, "mn": mn, "mx": mx})


def _migrate_product_cases(conn) -> None:
    """Versão anterior: a caixa e o palete ficavam no produto (case_qty, case_*, pallet_*). Passam a ser a embalagem
    padrão do produto (product_packagings) e a paletização dela (packaging_pallets). Roda uma vez por produto."""
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    if "case_qty" not in {c["name"] for c in insp.get_columns("products")}:
        return
    rows = conn.execute(text(
        "SELECT p.id, p.case_qty, p.case_model_id, p.case_height, p.case_width, p.case_length, p.case_weight, "
        "p.pallet_type_id, p.pallet_layer, p.pallet_layers FROM products p WHERE p.case_qty IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM product_packagings k WHERE k.product_id = p.id)")).all()
    for pid, qty, mid, h, w, ln, wt, pal, lay, lays in rows:
        res = conn.execute(text(
            "INSERT INTO product_packagings (product_id, name, inner_qty, model_id, height, width, length, weight, "
            "is_default, sort, created_at, updated_at) VALUES (:p, 'Caixa', :q, :m, :h, :w, :l, :wt, 1, 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"p": pid, "q": qty, "m": mid, "h": h, "w": w, "l": ln, "wt": wt})
        if pal:
            conn.execute(text(
                "INSERT INTO packaging_pallets (packaging_id, pallet_type_id, layer, layers, created_at, updated_at) "
                "VALUES (:k, :t, :a, :b, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                {"k": res.lastrowid, "t": pal, "a": lay, "b": lays})
        conn.execute(text("UPDATE products SET case_qty = NULL WHERE id = :p"), {"p": pid})


def init_db() -> None:
    import models  # noqa: F401  (registra os modelos no metadata)
    from sqlalchemy import text

    # Tabelas novas primeiro (a migração referencia shelf_levels); location_bins existente é mantida.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_missing_columns(conn)
        _migrate_location_bins(conn)
        _migrate_product_cases(conn)
    # O conceito de Vínculo Operacional foi removido: descarta a tabela de versões anteriores.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS product_warehouse_locations"))
    # Garante a regra de ocupação também para dados gravados por versões anteriores.
    from services.stock_service import refresh_occupancy

    with engine.begin() as conn:
        _migrate_stock_policies(conn)
    with SessionLocal() as db:
        from services.app_settings import load as load_settings

        load_settings(db)  # configurações (ordenação e equivalência dos endereços) antes de qualquer cálculo
        from services.structure_kinds import ensure_kinds

        ensure_kinds(db)  # tipos de estrutura e peças padrão; estruturas antigas ligadas ao tipo pelo nome
        refresh_occupancy(db, None)
        from services.stock_service import sync_default_locations

        sync_default_locations(db)  # endereço padrão = onde o produto está (dados antigos)
        from services.level_service import fit_structure

        from services.structure_service import recalc_capacities

        # capacidade dos níveis das estantes com estrutura: recalculada pelas medidas (dados antigos e mudanças)
        recalc_capacities(db, drop_redundant=_CAPS_MIGRATED)
        from services.level_service import spill_overflow

        spill_overflow(db)  # bins acima da capacidade do nível ocupam os níveis seguintes da estante
        from services.bin_content_service import refresh_contents

        refresh_contents(db)  # conteúdo por bin física (distribuição presumida onde não foi informado)
        fit_structure(db)  # vão menor que a bin (dados antigos): a estrutura acompanha a bin
        db.commit()
