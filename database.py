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
DATABASE_URL = os.getenv("NEOKIOSKI_DB_URL", f"sqlite:///{(BASE_DIR / 'neokioski.db').as_posix()}")

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
                     ("cost", "BIGINT")):
        if "bin_types" in tables and col not in btcols:
            conn.execute(text(f"ALTER TABLE bin_types ADD COLUMN {col} {ddl}"))
    if "bin_types" in tables and "div_w" not in btcols:
        # frações da versão anterior (1/n numa direção) viram grade n×1 ou 1×n
        conn.execute(text("UPDATE bin_types SET div_w = fraction_den / fraction_num, div_l = 1 "
                          "WHERE parent_id IS NOT NULL AND fraction_num > 0 AND split_axis != 'length'"))
        conn.execute(text("UPDATE bin_types SET div_w = 1, div_l = fraction_den / fraction_num "
                          "WHERE parent_id IS NOT NULL AND fraction_num > 0 AND split_axis = 'length'"))
    stcols = {c["name"] for c in insp.get_columns("structure_types")} if "structure_types" in tables else set()
    for col in ("golden_from", "golden_to"):
        if "structure_types" in tables and col not in stcols:
            conn.execute(text(f"ALTER TABLE structure_types ADD COLUMN {col} BIGINT"))
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


def init_db() -> None:
    import models  # noqa: F401  (registra os modelos no metadata)
    from sqlalchemy import text

    # Tabelas novas primeiro (a migração referencia shelf_levels); location_bins existente é mantida.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_missing_columns(conn)
        _migrate_location_bins(conn)
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
        refresh_occupancy(db, None)
        from services.stock_service import sync_default_locations

        sync_default_locations(db)  # endereço padrão = onde o produto está (dados antigos)
        from services.level_service import fit_structure

        from services.level_service import spill_overflow

        spill_overflow(db)  # bins acima da capacidade do nível ocupam os níveis seguintes da estante
        from services.bin_content_service import refresh_contents

        refresh_contents(db)  # conteúdo por bin física (distribuição presumida onde não foi informado)
        fit_structure(db)  # vão menor que a bin (dados antigos): a estrutura acompanha a bin
        db.commit()
