"""
Importação de Produtos via CSV/XLSX.

- Apenas SKU é obrigatório; demais colunas são mapeadas dinamicamente (índice ou nome).
- Filial e Armazém (opcionais) indicam o depósito do endereço padrão; são criados
  automaticamente a partir dos códigos quando não existirem.
- O endereço informado é ligado ao produto como endereço padrão.
- Processamento em lotes (chunking): cada lote é commitado e a sessão é limpa
  (expunge_all), mantendo o uso de memória constante para arquivos grandes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Product
from services.addressing import LocationResolver, normalize_code
from services.file_reader import chunked, get_cell, iter_rows, parse_decimal
from services.material_service import MaterialResolver
from services.stock_service import MasterDataCache, refresh_occupancy, resolve_skus

CHUNK_SIZE = 500

# campo -> (rótulo, obrigatório, sinônimos para auto-mapeamento)
PRODUCT_FIELDS: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "sku": ("SKU", True, ("sku", "codigo", "código", "cod", "codproduto", "codigoproduto", "referencia", "item")),
    "name": ("Nome / Descrição", False, ("nome", "name", "descricao", "descrição", "produto", "description")),
    "material_type": ("Tipo de material", False, ("tipodematerial", "tipomaterial", "tipo material", "tipo de material",
                                                   "material", "materialtype", "tpmaterial", "tipomat", "classematerial")),
    "height": ("Altura", False, ("altura", "height", "alt")),
    "width": ("Largura", False, ("largura", "width", "larg")),
    "length": ("Comprimento", False, ("comprimento", "length", "profundidade", "comp")),
    "weight": ("Peso", False, ("peso", "weight", "pesobruto", "pesoliquido")),
    "min_level": ("Nível mínimo (min level)", False, ("minlevel", "min level", "nivelminimo", "nível mínimo", "estoqueminimo",
                                                     "estoque minimo", "estoque mínimo", "minimo", "mínimo", "min", "estmin")),
    "max_level": ("Nível máximo (max level)", False, ("maxlevel", "max level", "nivelmaximo", "nível máximo", "estoquemaximo",
                                                     "estoque maximo", "estoque máximo", "maximo", "máximo", "max", "estmax")),
    "branch_code": ("Código da Filial", False, ("filial", "codfilial", "codigofilial", "branch", "loja", "empresa")),
    "warehouse_code": (
        "Código do Armazém",
        False,
        ("armazem", "armazém", "deposito", "depósito", "codarmazem", "warehouse", "local estoque"),
    ),
    "location": ("Endereço padrão", False, ("endereco", "endereço", "posicao", "posição", "location", "localizacao")),
}
DECIMAL_FIELDS = ("height", "width", "length", "weight", "min_level", "max_level")


@dataclass
class ImportOptions:
    has_header: bool = True
    default_branch_code: str = ""
    default_warehouse_code: str = ""
    update_existing: bool = True
    create_locations: bool = True
    update_default_location: bool = True  # endereço da planilha substitui o endereço padrão atual
    sync: bool = False  # excluir os produtos que não estão no arquivo (só os sem saldo)


@dataclass
class ImportResult:
    rows_read: int = 0
    rows_ok: int = 0
    products_created: int = 0
    products_updated: int = 0
    branches_created: int = 0
    warehouses_created: int = 0
    branch_warehouse_links_created: int = 0
    locations_created: int = 0
    stock_items_written: int = 0
    stock_items_zeroed: int = 0
    chunks: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    error_count: int = 0
    warnings: list[tuple[int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # associações automáticas (códigos/SKUs equivalentes)
    default_locations_set: int = 0
    bin_contents_set: int = 0
    products_deleted: int = 0
    elapsed_ms: int = 0

    MAX_LISTED = 500

    def error(self, line: int, msg: str) -> None:
        self.error_count += 1
        if len(self.errors) < self.MAX_LISTED:
            self.errors.append((line, msg))

    def warn(self, line: int, msg: str) -> None:
        if len(self.warnings) < self.MAX_LISTED:
            self.warnings.append((line, msg))


def _parse_row(line: int, row: list[str], mapping: dict[str, Optional[int]], opts: ImportOptions) -> dict:
    sku = normalize_code(get_cell(row, mapping.get("sku")))
    if not sku:
        raise ValueError("SKU vazio")
    if len(sku) > 60:
        raise ValueError("SKU excede 60 caracteres")
    data: dict = {"line": line, "sku": sku, "name": get_cell(row, mapping.get("name"))[:255],
                  "material_type": get_cell(row, mapping.get("material_type"))}
    for f in DECIMAL_FIELDS:
        raw = get_cell(row, mapping.get(f))
        try:
            val = parse_decimal(raw)
        except ValueError:
            raise ValueError(f"{PRODUCT_FIELDS[f][0]} inválido(a): {raw!r}")
        if val is not None and val < 0:
            raise ValueError(f"{PRODUCT_FIELDS[f][0]} não pode ser negativo(a)")
        data[f] = val
    data["branch_code"] = normalize_code(get_cell(row, mapping.get("branch_code"))) or normalize_code(
        opts.default_branch_code
    )
    data["warehouse_code"] = normalize_code(get_cell(row, mapping.get("warehouse_code"))) or normalize_code(
        opts.default_warehouse_code
    )
    data["location"] = normalize_code(get_cell(row, mapping.get("location")))
    return data


def import_products(
    db: Session, path: Path, mapping: dict[str, Optional[int]], opts: ImportOptions
) -> ImportResult:
    t0 = time.perf_counter()
    if mapping.get("sku") is None:
        raise ValueError("A coluna SKU é obrigatória no mapeamento.")
    result = ImportResult()
    master = MasterDataCache(db)
    resolver = LocationResolver(db)
    touched_products: set[int] = set()

    for chunk in chunked(iter_rows(path, opts.has_header), CHUNK_SIZE):
        result.chunks += 1
        result.rows_read += len(chunk)
        parsed: list[dict] = []
        for line, row in chunk:
            try:
                parsed.append(_parse_row(line, row, mapping, opts))
            except ValueError as exc:
                result.error(line, str(exc))
        if not parsed:
            continue

        snap = master.snapshot()
        counters = (result.products_created, result.products_updated, result.locations_created, result.rows_ok,
                    result.default_locations_set)
        try:
            _process_chunk(db, parsed, master, resolver, opts, result, touched_products)
            db.commit()
        except Exception as exc:  # falha inesperada: descarta somente este lote
            db.rollback()
            master.restore(snap)
            resolver.reset()
            (result.products_created, result.products_updated, result.locations_created, result.rows_ok,
             result.default_locations_set) = counters
            for item in parsed:
                result.error(item["line"], f"Lote descartado por erro inesperado: {exc}")
        finally:
            db.expunge_all()  # libera memória do identity map

    if touched_products:
        # Endereço padrão = onde o produto está: quem já tem saldo lançado fica no endereço do saldo
        from services.stock_service import sync_default_locations

        sync_default_locations(db, touched_products)
        # Endereços padrão mudaram: endereço com item atrelado fica ocupado
        refresh_occupancy(db, None)
        db.commit()

    if opts.sync:
        from services.import_sync import blocked_reason, sync_products

        why = blocked_reason(result.error_count, result.rows_ok)
        if why:
            result.notes.append(why)
        else:
            result.products_deleted, notes = sync_products(db, touched_products)
            result.notes += notes
            db.commit()
    result.notes = list(dict.fromkeys(master.notes + result.notes))
    result.branches_created = master.created_branches
    result.warehouses_created = master.created_warehouses
    result.branch_warehouse_links_created = master.created_links
    result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return result


def _process_chunk(
    db: Session,
    parsed: list[dict],
    master: MasterDataCache,
    resolver: LocationResolver,
    opts: ImportOptions,
    result: ImportResult,
    touched_products: set[int],
) -> None:
    # 1) Filiais e armazéns (criação automática apenas por código)
    master.ensure_branches(p["branch_code"] for p in parsed)
    master.ensure_warehouses(p["warehouse_code"] for p in parsed)
    master.ensure_links(
        (master.branches[p["branch_code"]], master.warehouses[p["warehouse_code"]])
        for p in parsed
        if p["branch_code"] and p["warehouse_code"]
    )

    # 2) Produtos (upsert por SKU). Linhas repetidas no lote: última informação não vazia prevalece.
    skus = {p["sku"] for p in parsed}
    materials = MaterialResolver(db)
    sku_ids, sku_notes = resolve_skus(db, skus)  # inclui SKUs numéricos equivalentes ('123' -> '00123')
    result.notes.extend(sku_notes)
    by_id = {prod.id: prod for prod in db.scalars(select(Product).where(Product.id.in_(set(sku_ids.values()))))}
    products: dict[str, Product] = {sku: by_id[pid] for sku, pid in sku_ids.items() if pid in by_id}
    existing_skus = set(products.keys())
    updated_skus: set[str] = set()
    for p in parsed:
        prod = products.get(p["sku"])
        if prod is None:
            prod = Product(sku=p["sku"], name=p["name"] or p["sku"], material_type=materials(p["material_type"]))
            for f in DECIMAL_FIELDS:
                setattr(prod, f, p[f])
            db.add(prod)
            products[p["sku"]] = prod
            result.products_created += 1
        elif p["sku"] in existing_skus and opts.update_existing:
            if p["name"]:
                prod.name = p["name"]
            if materials(p["material_type"]):
                prod.material_type = materials(p["material_type"])
            for f in DECIMAL_FIELDS:
                if p[f] is not None:
                    setattr(prod, f, p[f])
            if p["sku"] not in updated_skus:
                updated_skus.add(p["sku"])
                result.products_updated += 1
        elif p["sku"] not in existing_skus:
            # SKU repetido no mesmo lote (criado acima): complementa dados
            if p["name"]:
                prod.name = p["name"]
            if materials(p["material_type"]):
                prod.material_type = materials(p["material_type"])
            for f in DECIMAL_FIELDS:
                if p[f] is not None:
                    setattr(prod, f, p[f])
    db.flush()

    # 3) Endereço padrão (filial/armazém, se informados, indicam o depósito do endereço)
    for p in parsed:
        prod = products[p["sku"]]
        touched_products.add(prod.id)
        b_code, w_code = p["branch_code"], p["warehouse_code"]
        branch_id = master.branches.get(b_code) if b_code else None
        warehouse_id = master.warehouses.get(w_code) if w_code else None
        if p["location"]:
            ref, created, err, note = resolver.resolve(
                branch_id, warehouse_id if branch_id else None, p["location"],
                create=opts.create_locations and branch_id is not None,
            )
            if err:
                result.warn(p["line"], f"Endereço não vinculado: {err}")
            else:
                if created:
                    result.locations_created += 1
                if note:
                    result.warn(p["line"], note)
                if prod.default_location_id is None or (
                    opts.update_default_location and prod.default_location_id != ref.id
                ):
                    prod.default_location_id = ref.id
                    result.default_locations_set += 1
        elif b_code or w_code:
            result.warn(p["line"], "Filial/armazém informados sem endereço: nada a vincular ao produto.")
        result.rows_ok += 1
    db.flush()
