"""
Importação / sincronização de Saldos de Estoque via CSV/XLSX.

- Colunas mapeadas: Filial, Armazém, Posição (endereço), SKU e UMA OU MAIS colunas
  de quantidade. As colunas de quantidade selecionadas são SOMADAS por linha.
- Filial, Armazém e Posição inexistentes são criados dinamicamente.
- O saldo do StockItem é SOBREPOSTO com a nova quantidade (não acumulado ao saldo
  anterior do banco). Linhas repetidas do mesmo SKU+endereço no arquivo podem ser
  somadas entre si (padrão) ou a última prevalecer.
- Opcional: zerar saldos não presentes no arquivo (dentro das filiais/armazéns do arquivo).
- Processamento em lotes com commit e expunge_all por lote.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from models import Product, StockItem, StorageLocation
from services.addressing import LocationResolver, normalize_code, scope_filter
from services.file_reader import chunked, get_cell, iter_rows, parse_decimal
from services.product_importer import ImportResult
from services.stock_service import CHUNK, MasterDataCache, refresh_occupancy, resolve_skus, sync_default_locations
from services.i18n import N_, _

CHUNK_SIZE = 500

STOCK_FIELDS: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "branch_code": (N_("Filial"), False, ("filial", "codfilial", "branch", "loja", "empresa")),
    "warehouse_code": (N_("Armazém"), False, ("armazem", "armazém", "deposito", "depósito", "warehouse")),
    "location": (N_("Posição / Endereço"), True, ("posicao", "posição", "endereco", "endereço", "location", "local")),
    "sku": ("SKU", True, ("sku", "codigo", "código", "cod", "codproduto", "referencia", "item")),
    "bin": (N_("Bin (opcional, ex.: CX-M #2)"), False, ("bin", "nbin", "numerobin", "nºbin", "binfisica", "caixa", "bindestino")),
}
QTY_SYNONYMS = ("quantidade", "qtd", "qtde", "quantity", "saldo", "estoque", "qty")


@dataclass
class StockImportOptions:
    has_header: bool = True
    default_branch_code: str = ""
    default_warehouse_code: str = ""
    create_missing_products: bool = False
    duplicate_mode: str = "sum"  # "sum" | "last"
    zero_missing: bool = False
    allow_negative: bool = False


def import_stock(
    db: Session,
    path: Path,
    mapping: dict[str, Optional[int]],
    qty_columns: list[int],
    opts: StockImportOptions,
) -> ImportResult:
    t0 = time.perf_counter()
    if mapping.get("sku") is None:
        raise ValueError(_("A coluna SKU é obrigatória."))
    if mapping.get("location") is None:
        raise ValueError(_("A coluna Posição/Endereço é obrigatória."))
    if mapping.get("branch_code") is None and not normalize_code(opts.default_branch_code):
        raise ValueError(_("Mapeie a coluna Filial ou informe uma filial padrão."))
    if not qty_columns:
        raise ValueError(_("Selecione ao menos uma coluna de quantidade."))

    result = ImportResult()
    master = MasterDataCache(db)
    resolver = LocationResolver(db)  # índices de endereços por depósito (persistentes entre lotes)
    touched_pairs: set[tuple[int, int]] = set()  # (product_id, location_id) já gravados nesta importação
    touched_locations: set[int] = set()
    touched_products: set[int] = set()
    scopes: set[tuple[int, Optional[int]]] = set()
    bin_rows: dict[int, dict[tuple[int, str], Decimal]] = {}  # endereço -> (produto, bin) -> qtd

    for chunk in chunked(iter_rows(path, opts.has_header), CHUNK_SIZE):
        result.chunks += 1
        result.rows_read += len(chunk)
        parsed: list[dict] = []
        for line, row in chunk:
            try:
                sku = normalize_code(get_cell(row, mapping.get("sku")))
                if not sku:
                    raise ValueError(_("SKU vazio"))
                location = normalize_code(get_cell(row, mapping.get("location")))
                if not location:
                    raise ValueError(_("Posição/endereço vazio"))
                branch = normalize_code(get_cell(row, mapping.get("branch_code"))) or normalize_code(
                    opts.default_branch_code
                )
                if not branch:
                    raise ValueError(_("Filial vazia"))
                warehouse = normalize_code(get_cell(row, mapping.get("warehouse_code"))) or normalize_code(
                    opts.default_warehouse_code
                )
                qty = Decimal("0")
                for col in qty_columns:
                    raw = get_cell(row, col)
                    try:
                        val = parse_decimal(raw)
                    except ValueError:
                        raise ValueError(_("Quantidade inválida na coluna {v}: {raw}", v=col + 1, raw=f"{raw!r}"))
                    qty += val or Decimal("0")
                if qty < 0 and not opts.allow_negative:
                    raise ValueError(_("Quantidade negativa ({qty}) não permitida", qty=qty))
                parsed.append(
                    {"line": line, "sku": sku, "location": location, "branch": branch, "warehouse": warehouse, "qty": qty,
                     "bin": normalize_code(get_cell(row, mapping.get("bin")))}
                )
            except ValueError as exc:
                result.error(line, str(exc))
        if not parsed:
            continue

        snap = master.snapshot()
        pairs_snap = set(touched_pairs)
        counters = (result.products_created, result.locations_created, result.stock_items_written, result.rows_ok)
        try:
            new_locs, new_prods, new_scopes = _process_chunk(
                db, parsed, master, resolver, touched_pairs, opts, result, bin_rows
            )
            db.commit()
            touched_locations |= new_locs
            touched_products |= new_prods
            scopes |= new_scopes
        except Exception as exc:
            db.rollback()
            master.restore(snap)
            resolver.reset()
            touched_pairs.clear()
            touched_pairs.update(pairs_snap)
            (result.products_created, result.locations_created, result.stock_items_written, result.rows_ok) = counters
            for item in parsed:
                result.error(item["line"], _("Lote descartado por erro inesperado: {exc}", exc=exc))
        finally:
            db.expunge_all()

    # ---- Zera saldos ausentes no arquivo (sincronização completa por escopo) ----
    from services.import_sync import blocked_reason

    why = blocked_reason(result.error_count, result.rows_ok) if opts.zero_missing else None
    if why:
        result.notes.append(why.replace("excluído", "zerado"))
    if opts.zero_missing and scopes and not why:
        for branch_id, warehouse_id in scopes:
            stmt = scope_filter(
                select(StockItem.id, StockItem.product_id, StockItem.location_id).join(
                    StorageLocation, StorageLocation.id == StockItem.location_id
                ),
                branch_id,
                warehouse_id,
            ).where(StockItem.quantity != Decimal("0"))
            to_zero = [r for r in db.execute(stmt).all() if (r.product_id, r.location_id) not in touched_pairs]
            for i in range(0, len(to_zero), CHUNK):
                part = to_zero[i : i + CHUNK]
                db.query(StockItem).filter(StockItem.id.in_([r.id for r in part])).update(
                    {StockItem.quantity: Decimal("0")}, synchronize_session=False
                )
                touched_locations.update(r.location_id for r in part)
                touched_products.update(r.product_id for r in part)
            result.stock_items_zeroed += len(to_zero)
        db.commit()

    # ---- Consistência: ocupação dos endereços ----
    if touched_locations:
        refresh_occupancy(db, touched_locations)
        db.commit()
        from services.bin_content_service import refresh_contents

        refresh_contents(db, touched_locations)  # divide o saldo pelas bins (mantém o informado, se coerente)
        db.commit()
    if bin_rows:
        result.bin_contents_set = _apply_bin_rows(db, bin_rows, result)
        db.commit()
    # ---- Endereço padrão = endereço onde o produto está ----
    if touched_products:
        result.default_locations_set = sync_default_locations(db, touched_products)
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
    touched_pairs: set[tuple[int, int]],
    opts: StockImportOptions,
    result: ImportResult,
    bin_rows: Optional[dict] = None,
):
    master.ensure_branches(p["branch"] for p in parsed)
    master.ensure_warehouses(p["warehouse"] for p in parsed if p["warehouse"])
    master.ensure_links(
        (master.branches[p["branch"]], master.warehouses[p["warehouse"]]) for p in parsed if p["warehouse"]
    )

    # Produtos
    skus = {p["sku"] for p in parsed}
    products, sku_notes = resolve_skus(db, skus)
    result.notes.extend(sku_notes)
    missing = sorted(skus - products.keys())
    if missing and opts.create_missing_products:
        for sku in missing:
            prod = Product(sku=sku, name=sku)
            db.add(prod)
            db.flush()
            products[sku] = prod.id
            result.products_created += 1

    # Endereços + agregação por (produto, endereço) dentro do lote
    aggregated: dict[tuple[int, int], Decimal] = {}
    scopes: set[tuple[int, Optional[int]]] = set()
    for p in parsed:
        product_id = products.get(p["sku"])
        if product_id is None:
            result.error(p["line"], _("SKU {sku} não cadastrado (habilite 'criar produtos inexistentes').", sku=p['sku']))
            continue
        branch_id = master.branches[p["branch"]]
        warehouse_id = master.warehouses[p["warehouse"]] if p["warehouse"] else None
        ref, created, err, note = resolver.resolve(branch_id, warehouse_id, p["location"])
        if err:
            result.error(p["line"], err)
            continue
        if created:
            result.locations_created += 1
        if note:
            result.warn(p["line"], note)
        loc_id = ref.id
        scopes.add((ref.branch_id, ref.warehouse_id))
        key = (product_id, loc_id)
        if opts.duplicate_mode == "sum":
            aggregated[key] = aggregated.get(key, Decimal("0")) + p["qty"]
        else:
            aggregated[key] = p["qty"]
        if bin_rows is not None and p.get("bin"):
            br = bin_rows.setdefault(loc_id, {})
            if opts.duplicate_mode != "sum":
                for k in [k for k in br if k[0] == product_id]:
                    del br[k]
            br[(product_id, p["bin"])] = br.get((product_id, p["bin"]), Decimal("0")) + p["qty"]
        result.rows_ok += 1

    if not aggregated:
        return set(), set(), scopes

    # Upsert dos StockItems (sobrepõe o saldo do banco)
    keys = list(aggregated.keys())
    existing: dict[tuple[int, int], StockItem] = {}
    for i in range(0, len(keys), 400):
        part = keys[i : i + 400]
        for si in db.scalars(
            select(StockItem).where(tuple_(StockItem.product_id, StockItem.location_id).in_(part))
        ):
            existing[(si.product_id, si.location_id)] = si

    for key, qty in aggregated.items():
        si = existing.get(key)
        if si is None:
            db.add(StockItem(product_id=key[0], location_id=key[1], quantity=qty))
        elif key in touched_pairs and opts.duplicate_mode == "sum":
            # Mesmo par já gravado em lote anterior DESTA importação: soma as linhas do arquivo.
            si.quantity = si.quantity + qty
        else:
            si.quantity = qty
        touched_pairs.add(key)
        result.stock_items_written += 1
    db.flush()

    return {k[1] for k in keys}, {k[0] for k in keys}, scopes


def parse_bin_label(label: str, phys: list, type_codes: dict[int, str]) -> Optional[tuple[int, int]]:
    """'CX-M #2', 'CX-M 2', 'CX-M-2' -> (tipo, 2). Só o número ('2') vale quando o endereço tem um único tipo."""
    import re

    label = label.strip().upper()
    m = re.fullmatch(r"(.+?)\s*(?:#|Nº|N°|NO\.?|\s|-)\s*(\d+)", label)
    codes = {v.upper(): k for k, v in type_codes.items()}
    if m and m.group(1).strip() in codes:
        t, seq = codes[m.group(1).strip()], int(m.group(2))
    elif label.isdigit():
        tids = {b.type_id for b in phys}
        if len(tids) != 1:
            return None
        t, seq = tids.pop(), int(label)
    else:
        return None
    return (t, seq) if any(b.type_id == t and b.seq == seq for b in phys) else None


def _apply_bin_rows(db: Session, bin_rows: dict, result: ImportResult) -> int:
    """Grava a divisão por bin informada no arquivo (endereço a endereço; precisa cobrir todo o saldo)."""
    from models import BinType, StorageLocation
    from services.bin_content_service import physical_bins, set_contents

    codes = dict(db.execute(select(BinType.id, BinType.code)).all())
    phys = physical_bins(db, bin_rows.keys())
    ok = 0
    for lid, rows in bin_rows.items():
        address = db.get(StorageLocation, lid).address
        out = []
        bad = None
        for (pid, label), q in rows.items():
            tb = parse_bin_label(label, phys.get(lid, []), codes)
            if tb is None:
                bad = _("bin '{label}' não existe no endereço", label=label)
                break
            out.append((tb[0], tb[1], pid, q))
        err = bad or set_contents(db, lid, out)
        if err:
            result.notes.append(_("{address}: divisão por bin não gravada ({err}); usada a distribuição automática.", address=address, err=err))
        else:
            ok += 1
    return ok
