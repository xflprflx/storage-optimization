"""CRUD de Locais de Armazenamento (endereços) + Geração em Lote."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, delete, exists, func, select, union
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import FormData

from database import get_db
from models import (
    BinType,
    Branch,
    LocationBin,
    Product,
    ShelfLevel,
    StockItem,
    StockPolicy,
    StorageLocation,
    Warehouse,
    branch_warehouse,
)
from routers.common import (
    FormErrors,
    apply_sort,
    f_bool,
    f_int,
    f_str,
    form_data,
    paginate,
    redirect,
    render,
    sku_name_fields,
    text_search,
    to_int,
)
from services.file_reader import parse_decimal
from services.addressing import build_address, check_location_conflict, normalize_code
from services.batch_generator import (
    LEVEL_LABELS,
    LEVELS,
    MAX_COMBINATIONS,
    SHORT_LABELS,
    BatchException,
    BatchRequest,
    LevelRule,
    RangeType,
    run_batch,
)
from services.material_service import material_condition
from services.bin_service import BULK_MODES, bulk_assign, capacity_of, parse_bin_rows, set_location_bins, validate_types
from services.level_service import (
    capacity_warnings,
    bay_levels,
    bays_of_locations,
    fit_structure,
    spill_message,
    spill_overflow,
    structure_message,
    ensure_level,
    drop_unused_levels,
    level_key_of,
    location_level_options,
    usage_for,
    validate_bin_levels,
)
from services.stock_service import has_items_expr, location_has_items, refresh_occupancy

router = APIRouter(prefix="/locations", tags=["Endereços"])


def _lookups(db: Session) -> dict:
    branches = list(db.scalars(select(Branch).order_by(Branch.code)))
    warehouses = list(db.scalars(select(Warehouse).order_by(Warehouse.code)))
    links = db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)).all()
    wh_branches: dict[int, list[int]] = {}
    for b, w in links:
        wh_branches.setdefault(w, []).append(b)
    bin_types = list(db.scalars(select(BinType).order_by(BinType.code)))
    from models import StructureType

    structures = list(db.scalars(select(StructureType).order_by(StructureType.code)))
    return {"branches": branches, "warehouses": warehouses, "wh_branches": wh_branches, "bin_types": bin_types,
            "structures": structures}


def _affected_levels(db: Session, loc, bin_items) -> set:
    """Níveis físicos tocados pelas bins de um endereço (o próprio + os extras usados)."""
    keys = set()
    own = level_key_of(loc)
    if own:
        keys.add(own)
    extra_ids = {it[2] for it in bin_items if len(it) > 2 and it[2]}
    if extra_ids:
        for sl in db.scalars(select(ShelfLevel).where(ShelfLevel.id.in_(extra_ids))):
            keys.add((sl.branch_id, sl.warehouse_id, sl.aisle, sl.bay, sl.level))
    return keys


def _with_capacity_warnings(db: Session, msg: str, keys) -> tuple[str, str]:
    codes = dict(db.execute(select(BinType.id, BinType.code)).all())
    keys = set(keys)
    bays = {k[:4] for k in keys}
    spilled = spill_message(db, spill_overflow(db, bays))
    if spilled:
        db.commit()
        msg += " " + spilled
        for bk in bays:
            keys |= {lv["key"] for lv in bay_levels(db, *bk)}
    adj = structure_message(fit_structure(db, keys))
    if adj:
        db.commit()
        msg += " " + adj
    warns = capacity_warnings(db, keys, codes)
    if warns:
        return msg + " ATENÇÃO: " + " ".join(warns), "warning"
    return msg, "success"


def _level_context(db: Session, loc) -> dict:
    """Opções de nível extra (mesma estante) e uso atual dos níveis para o formulário do endereço."""
    options = location_level_options(db, loc)
    # Nível precisa existir na tabela para ser escolhido: cria os que só existem via endereços
    for opt in options:
        if opt["shelf_level_id"] is None:
            opt["shelf_level_id"] = ensure_level(db, opt["key"]).id
    db.commit()
    keys = [o["key"] for o in options]
    own = level_key_of(loc)
    if own:
        keys.append(own)
    usage = usage_for(db, keys)
    codes = dict(db.execute(select(BinType.id, BinType.code)).all())

    def describe(k):
        u = usage.get(k)
        if not u or not u.has_capacity:
            return "sem capacidade cadastrada"
        caps = " ou ".join(f"{m}× {codes.get(t, t)}" for t, m in u.capacity.items())
        f = u.fraction
        return f"cabem {caps} · uso {'?' if f is None else f'{f * 100:.0f}%'}"

    for o in options:
        o["info"] = describe(o["key"])
    return {"level_options": options, "own_level_info": describe(own) if own else None}


def _has_positive_stock(db: Session, location_id: int) -> bool:
    return bool(db.scalar(select(exists().where(StockItem.location_id == location_id, StockItem.quantity > Decimal("0")))))


def _validate(db: Session, form: FormData, location_id: int | None):
    errors = FormErrors()
    data = {
        "branch_id": f_int(form, "branch_id"),
        "warehouse_id": f_int(form, "warehouse_id"),
        "aisle": normalize_code(f_str(form, "aisle")),
        "bay": normalize_code(f_str(form, "bay")),
        "level": normalize_code(f_str(form, "level")),
        "position": normalize_code(f_str(form, "position")),
        "compartment": normalize_code(f_str(form, "compartment")),
    }
    if not data["branch_id"] or not db.get(Branch, data["branch_id"]):
        errors.add("branch_id", "Filial é obrigatória.")
    if data["warehouse_id"]:
        if not db.get(Warehouse, data["warehouse_id"]):
            errors.add("warehouse_id", "Armazém inválido.")
        elif data["branch_id"] and not db.scalar(
            select(exists().where(branch_warehouse.c.branch_id == data["branch_id"],
                                  branch_warehouse.c.warehouse_id == data["warehouse_id"]))
        ):
            errors.add("warehouse_id", "Este armazém não está vinculado à filial selecionada.")
    if not data["aisle"]:
        errors.add("aisle", "Corredor é obrigatório.")
    # Hierarquia estrita: um nível só pode ser preenchido se o anterior estiver preenchido.
    chain = ["aisle", "bay", "level", "position", "compartment"]
    for prev, cur in zip(chain, chain[1:]):
        if data[cur] and not data[prev]:
            errors.add(cur, f"{LEVEL_LABELS[cur]} exige {LEVEL_LABELS[prev]} preenchido (hierarquia estrita).")
    for k in chain:
        if len(data[k]) > 20:
            errors.add(k, "Máximo de 20 caracteres.")
    bin_items, bin_err = parse_bin_rows(form.getlist("bin_type_id"), form.getlist("bin_qty"), form.getlist("bin_level"))
    bin_err = bin_err or validate_types(db, [t for t, _, _ in bin_items])
    if not bin_err:
        bin_err, bin_items = validate_bin_levels(db, data, bin_items)
    if bin_err:
        errors.add("bins", bin_err)
    data["bins"] = bin_items
    if not errors:
        conflict = check_location_conflict(
            db, data["branch_id"], data["warehouse_id"],
            (data["aisle"], data["bay"], data["level"], data["position"], data["compartment"]),
            exclude_id=location_id,
        )
        if conflict:
            errors.add("__all__", conflict)
    return errors, data


FILTER_KEYS = ("branch_id", "warehouse_id", "q", "sku", "material", "occupied", "bin_type_id")


def location_contents(db: Session, ids: list[int]) -> dict[int, list[dict]]:
    """Itens atrelados a cada endereço: saldo (qualquer quantidade) e endereço padrão do produto."""
    merged: dict[tuple[int, int], dict] = {}

    def item(lid, pid, psku, pname, pmat=""):
        return merged.setdefault((lid, pid), {"id": pid, "sku": psku, "name": pname, "material": pmat or "",
                                              "quantity": None, "tags": set()})

    for r in db.execute(
        select(StockItem.location_id, Product.id, Product.sku, Product.name, Product.material_type, StockItem.quantity)
        .join(Product, Product.id == StockItem.product_id).where(StockItem.location_id.in_(ids))
    ):
        item(r[0], r[1], r[2], r[3], r[4])["quantity"] = r[5]
    for r in db.execute(select(StockPolicy.default_location_id, Product.id, Product.sku, Product.name,
                               Product.material_type)
                        .join(Product, Product.id == StockPolicy.product_id)
                        .where(StockPolicy.default_location_id.in_(ids))):
        item(*r)["tags"].add("padrão")
    out: dict[int, list[dict]] = {}
    for (lid, _), it in sorted(merged.items(), key=lambda kv: (kv[0][0], kv[1]["sku"])):
        out.setdefault(lid, []).append(it)
    return out


def linked_location_ids(sku_query: str = "", material: str = ""):
    """Subconsulta com os endereços ligados (saldo ou endereço padrão) a produtos que correspondem
    à busca por SKU/nome e/ou ao tipo de material."""
    via_stock = text_search(
        select(StockItem.location_id.label("lid")).join(Product, Product.id == StockItem.product_id),
        sku_query, Product.sku, Product.name, fields=sku_name_fields(Product))
    via_default = text_search(
        select(StockPolicy.default_location_id.label("lid")).join(Product, Product.id == StockPolicy.product_id)
        .where(StockPolicy.default_location_id.is_not(None)),
        sku_query, Product.sku, Product.name, fields=sku_name_fields(Product))
    cond = material_condition(material)
    if cond is not None:
        via_stock, via_default = via_stock.where(cond), via_default.where(cond)
    return union(via_stock, via_default)


def _apply_filters(stmt, f: dict):
    """Filtros da lista de endereços (reutilizados pela atribuição em massa 'todos os filtrados')."""
    L = StorageLocation
    bid = to_int(f.get("branch_id"))
    if bid:
        stmt = stmt.where(L.branch_id == bid)
    wh = f.get("warehouse_id") or ""
    if wh == "none":
        stmt = stmt.where(L.warehouse_id.is_(None))
    elif to_int(wh):
        stmt = stmt.where(L.warehouse_id == to_int(wh))
    stmt = text_search(stmt, f.get("q") or "", L.address, address_columns=(L.address,))
    sku = (f.get("sku") or "").strip()
    material = (f.get("material") or "").strip()
    if sku or material:
        # Endereços ligados a um produto que corresponda (SKU/nome e tipo de material): saldo ou endereço padrão
        stmt = stmt.where(L.id.in_(linked_location_ids(sku, material)))
    occ = f.get("occupied") or ""
    if occ == "1":
        stmt = stmt.where(L.is_occupied.is_(True))
    elif occ == "0":
        stmt = stmt.where(L.is_occupied.is_(False))
    bt = f.get("bin_type_id") or ""
    if bt == "none":
        stmt = stmt.where(~exists().where(LocationBin.location_id == L.id))
    elif bt == "any":
        stmt = stmt.where(exists().where(LocationBin.location_id == L.id))
    elif to_int(bt):
        stmt = stmt.where(exists().where(LocationBin.location_id == L.id, LocationBin.bin_type_id == to_int(bt)))
    return stmt


@router.get("")
def list_locations(
    request: Request,
    branch_id: str = "",
    warehouse_id: str = "",
    q: str = "",
    sku: str = "",
    material: str = "",
    occupied: str = "",
    bin_type_id: str = "",
    sort: str = "",
    dir: str = "",
    page: int = 1,
    per_page: int = 100,
    db: Session = Depends(get_db),
):
    L = StorageLocation
    stmt = (
        select(L)
        .join(Branch, Branch.id == L.branch_id)
        .outerjoin(Warehouse, Warehouse.id == L.warehouse_id)
        .options(joinedload(L.branch), joinedload(L.warehouse))
    )
    has_qty = and_(StockItem.location_id == L.id, StockItem.quantity != Decimal("0"))
    sku_count = select(func.count()).select_from(StockItem).where(has_qty).correlate(L).scalar_subquery()
    first_stock_sku = (select(func.min(Product.sku)).select_from(StockItem)
                       .join(Product, Product.id == StockItem.product_id)
                       .where(StockItem.location_id == L.id).correlate(L).scalar_subquery())
    first_default_sku = (select(func.min(Product.sku)).select_from(StockPolicy)
                         .join(Product, Product.id == StockPolicy.product_id)
                         .where(StockPolicy.default_location_id == L.id).correlate(L).scalar_subquery())
    hi = "\U0010ffff"  # sentinela: endereços sem produto vão para o fim
    first_sku = func.nullif(func.min(func.coalesce(first_stock_sku, hi), func.coalesce(first_default_sku, hi)), hi)
    total_qty = select(func.sum(StockItem.quantity)).where(has_qty).correlate(L).scalar_subquery()
    mat_stock = (select(func.min(func.nullif(Product.material_type, ""))).select_from(StockItem)
                 .join(Product, Product.id == StockItem.product_id)
                 .where(StockItem.location_id == L.id).correlate(L).scalar_subquery())
    mat_default = (select(func.min(func.nullif(Product.material_type, ""))).select_from(StockPolicy)
                   .join(Product, Product.id == StockPolicy.product_id)
                   .where(StockPolicy.default_location_id == L.id).correlate(L).scalar_subquery())
    first_material = func.nullif(func.min(func.coalesce(mat_stock, hi), func.coalesce(mat_default, hi)), hi)
    first_bin = (select(func.min(BinType.code)).select_from(LocationBin)
                 .join(BinType, BinType.id == LocationBin.bin_type_id)
                 .where(LocationBin.location_id == L.id).correlate(L).scalar_subquery())
    bin_total = (select(func.sum(LocationBin.quantity)).where(LocationBin.location_id == L.id)
                 .correlate(L).scalar_subquery())
    physical = (L.aisle, L.bay, L.level, L.position, L.compartment)
    filters = {"branch_id": branch_id, "warehouse_id": warehouse_id, "q": q, "sku": sku, "material": material,
               "occupied": occupied, "bin_type_id": bin_type_id}
    stmt = _apply_filters(stmt, filters)
    stmt, srt = apply_sort(stmt, sort, dir, {
        "branch": (Branch.code, Warehouse.code, *physical),
        "warehouse": (Warehouse.code, Branch.code, *physical),
        "address": (*physical, Branch.code, Warehouse.code),
        "aisle": L.aisle, "bay": L.bay, "level": L.level, "position": L.position, "compartment": L.compartment,
        "occupied": L.is_occupied,
        "bins": (first_bin, bin_total),
        "sku": (first_sku, sku_count),
        "material": (first_material, first_sku),
        "quantity": total_qty,
    }, default="branch", tiebreak=L.id)
    per_page = min(max(per_page, 10), 500)
    pg = paginate(db, stmt, page, per_page)

    # SKUs/quantidades e bins de cada endereço da página (uma consulta cada, para a página inteira)
    ids = [l.id for l in pg.items]
    contents: dict[int, list] = {}
    bins: dict[int, list[LocationBin]] = {}
    if ids:
        contents = location_contents(db, ids)
        for lb in db.scalars(
            select(LocationBin).join(BinType, BinType.id == LocationBin.bin_type_id)
            .options(joinedload(LocationBin.bin_type), joinedload(LocationBin.shelf_level))
            .where(LocationBin.location_id.in_(ids)).order_by(LocationBin.location_id, BinType.code)
        ):
            bins.setdefault(lb.location_id, []).append(lb)
    totals = {lid: sum((r["quantity"] or Decimal("0") for r in items), Decimal("0")) for lid, items in contents.items()}
    capacities = {lid: capacity_of(items) for lid, items in bins.items()}
    total_filtered = pg.total
    return render(request, "locations/list.html", page=pg, q=q, sku=sku, material=material, branch_id=to_int(branch_id),
                  warehouse_id=warehouse_id, occupied=occupied, bin_type_id=bin_type_id, per_page=per_page,
                  contents=contents, totals=totals, bins=bins, capacities=capacities, sort=srt,
                  filters=filters, total_filtered=total_filtered, bulk_modes=BULK_MODES, **_lookups(db))


@router.post("/bins")
def assign_bins(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Atribuição de bins em massa: endereços selecionados ou todos os que atendem aos filtros."""
    back = f_str(form, "back") or "/locations"
    if not back.startswith("/locations"):
        back = "/locations"
    scope = f_str(form, "scope") or "selected"
    mode = f_str(form, "mode") or "add"
    bin_type_id = f_int(form, "bin_type_id")
    qty = f_int(form, "bin_qty") or 1
    if scope == "filtered":
        filters = {k: f_str(form, f"f_{k}") for k in FILTER_KEYS}
        ids = list(db.scalars(_apply_filters(select(StorageLocation.id), filters)))
    else:
        ids = [i for i in (to_int(v) for v in form.getlist("ids")) if i]
    if not ids:
        return redirect(back, "Nenhum endereço selecionado.", "warning")
    if mode != "clear":
        err = validate_types(db, [bin_type_id] if bin_type_id else [])
        if err or not bin_type_id:
            return redirect(back, err or "Selecione o tipo de bin.", "danger")
    try:
        res = bulk_assign(db, ids, mode, bin_type_id, qty)
        db.flush()
        spilled = spill_message(db, spill_overflow(db, bays_of_locations(db, ids)))
        adj = (spilled + " " + structure_message(fit_structure(db))).strip()
        db.commit()
    except ValueError as exc:
        db.rollback()
        return redirect(back, str(exc), "danger")
    bt = db.get(BinType, bin_type_id) if bin_type_id else None
    label = bt.code if bt else ""
    msgs = {
        "add": f"{qty} bin(s) {label} adicionada(s) a {res.locations} endereço(s).",
        "set": f"{res.locations} endereço(s) agora com {qty} bin(s) {label}.",
        "replace": f"{res.locations} endereço(s) agora somente com {qty} bin(s) {label}.",
        "remove": f"Tipo {label} removido de {res.rows_removed} endereço(s).",
        "clear": f"Bins removidas de {res.locations} endereço(s).",
    }
    return redirect(back, (msgs[mode] + " " + adj).strip())


@router.get("/new")
def new_location(request: Request, db: Session = Depends(get_db)):
    return render(request, "locations/form.html", location=None, data={"bins": []}, errors={}, level_options=[],
                  **_lookups(db))


@router.post("/new")
def create_location(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "locations/form.html", status_code=422, location=None, data=data, errors=errors,
                      level_options=[], **_lookups(db))
    bin_items = data.pop("bins")
    loc = StorageLocation(**data, address=build_address(data["aisle"], data["bay"], data["level"], data["position"], data["compartment"]))
    db.add(loc)
    db.flush()
    set_location_bins(db, loc.id, bin_items)
    db.commit()
    msg, lvl = _with_capacity_warnings(db, f"Endereço {loc.address} criado.", _affected_levels(db, loc, bin_items))
    if f_bool(form, "add_another"):
        return redirect("/locations/new", msg, lvl)
    return redirect("/locations", msg, lvl)


@router.get("/batch")
def batch_form(request: Request, db: Session = Depends(get_db)):
    defaults = {
        "aisle": {"type": "ALPHABETIC", "start": "A", "end": "C", "value": ""},
        "bay": {"type": "NUMERIC", "start": "01", "end": "05", "value": ""},
        "level": {"type": "NUMERIC", "start": "01", "end": "04", "value": ""},
        "position": {"type": "NUMERIC", "start": "01", "end": "03", "value": ""},
        "compartment": {"type": "NONE", "start": "", "end": "", "value": ""},
    }
    return render(request, "locations/batch.html", rules=defaults, exceptions=[], data={"bins": []}, result=None, error=None,
                  levels=LEVELS, level_labels=LEVEL_LABELS, short_labels=SHORT_LABELS,
                  max_combinations=MAX_COMBINATIONS, **_lookups(db))


_EXC_RE = re.compile(r"^exc_target_(\d+)$")


def _parse_exceptions(form: FormData) -> tuple[list[dict], list[BatchException], Optional[str]]:
    """Lê as exceções do formulário (campos exc_<campo>_<n>), na ordem em que aparecem na tela."""
    indices = sorted({int(m.group(1)) for k in form.keys() if (m := _EXC_RE.match(k))})
    raw_list: list[dict] = []
    parsed: list[BatchException] = []
    error = None
    for n in indices:
        raw = {
            "target": f_str(form, f"exc_target_{n}"),
            "type": f_str(form, f"exc_type_{n}") or "NUMERIC",
            "start": f_str(form, f"exc_start_{n}"),
            "end": f_str(form, f"exc_end_{n}"),
            "value": f_str(form, f"exc_value_{n}"),
            "padding": f_str(form, f"exc_padding_{n}"),
            "cond": {lvl: f_str(form, f"exc_cond_{lvl}_{n}") for lvl in LEVELS[:-1]},
        }
        raw_list.append(raw)
        if raw["target"] not in LEVELS[1:]:
            error = error or f"Exceção {len(raw_list)}: escolha o nível afetado."
            continue
        try:
            rt = RangeType(raw["type"])
        except ValueError:
            error = error or f"Exceção {len(raw_list)}: tipo inválido."
            continue
        parents = LEVELS[: LEVELS.index(raw["target"])]
        conds = {lvl: raw["cond"][lvl] for lvl in parents}
        parsed.append(BatchException(
            target=raw["target"], conditions=conds,
            rule=LevelRule(raw["target"], rt, raw["start"], raw["end"], raw["value"], to_int(raw["padding"])),
        ))
    return raw_list, parsed, error


@router.post("/batch")
def batch_run(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    action = f_str(form, "action") or "preview"
    data = {
        "branch_id": f_int(form, "branch_id"),
        "warehouse_id": f_int(form, "warehouse_id"),
        "replace_unoccupied": f_bool(form, "replace_unoccupied"),
        "structure_type_id": f_int(form, "structure_type_id"),
    }
    rules_raw: dict[str, dict] = {}
    rules: dict[str, LevelRule] = {}
    error = None
    for name in LEVELS:
        r = {
            "type": f_str(form, f"{name}_type") or ("EXACT" if name == "aisle" else "NONE"),
            "start": f_str(form, f"{name}_start"),
            "end": f_str(form, f"{name}_end"),
            "value": f_str(form, f"{name}_value"),
            "padding": f_str(form, f"{name}_padding"),
        }
        rules_raw[name] = r
        try:
            rt = RangeType(r["type"])
        except ValueError:
            error = f"Tipo inválido para {LEVEL_LABELS[name]}."
            continue
        rules[name] = LevelRule(name, rt, r["start"], r["end"], r["value"], to_int(r["padding"]))

    exceptions_raw, exceptions, exc_error = _parse_exceptions(form)
    error = error or exc_error
    bin_items, bin_err = parse_bin_rows(form.getlist("bin_type_id"), form.getlist("bin_qty"))
    bin_items = [(t, q) for t, q, _ in bin_items]  # na geração em lote as bins ficam no nível do endereço
    error = error or bin_err or validate_types(db, [t for t, _ in bin_items])
    data["bins"] = bin_items

    if not error:
        if not data["branch_id"] or not db.get(Branch, data["branch_id"]):
            error = "Selecione a filial."
        elif data["warehouse_id"] and not db.scalar(
            select(exists().where(branch_warehouse.c.branch_id == data["branch_id"],
                                  branch_warehouse.c.warehouse_id == data["warehouse_id"]))
        ):
            error = "O armazém selecionado não está vinculado à filial."

    result = None
    if not error:
        try:
            req = BatchRequest(data["branch_id"], data["warehouse_id"], rules, data["replace_unoccupied"], exceptions,
                               bin_items)
            result = run_batch(db, req, execute=(action == "execute"))
            if action == "execute" and data["structure_type_id"] and result.new_paths:
                # estantes geradas recebem a estrutura: vão dos níveis e capacidade de cada tipo de bin
                from models import StructureType
                from services.structure_service import apply_structure

                st = db.get(StructureType, data["structure_type_id"])
                bays = sorted({(data["branch_id"], data["warehouse_id"], seg[0], seg[1])
                               for seg, _ in result.new_paths if len(seg) >= 2})
                if st is not None and bays:
                    apply_structure(db, bays, st, create_levels=False)
                    result.structure_note = f"Estrutura {st.code} aplicada a {len(bays)} estante(s)."
                db.commit()
            if action == "execute" and bin_items:
                spill_overflow(db, None)
                fit_structure(db)
                db.commit()
        except ValueError as exc:
            error = str(exc)
        except Exception as exc:  # integridade / falhas inesperadas
            error = f"Falha ao gravar o lote (nenhuma alteração foi aplicada): {exc}"

    preview_url = None
    if result is not None and not result.executed and (result.new_paths or result.conflict_addresses):
        from urllib.parse import urlencode

        from services.layout_service import store_preview

        token = store_preview(data["branch_id"], data["warehouse_id"], result.new_paths, result.removed_ids,
                              result.conflict_addresses, bin_items)
        q = {"branch_id": data["branch_id"], "warehouse_id": data["warehouse_id"] or "none", "preview": token, "embed": 1}
        # abre no corredor do lote (o primeiro, se forem vários; o filtro do mapa troca de corredor)
        aisles = result.values_per_level.get("aisle") or sorted({seg[0] for seg, _ in result.new_paths})
        if aisles:
            q["aisle"] = aisles[0]
        preview_url = "/layout?" + urlencode(q)
    return render(request, "locations/batch.html", status_code=422 if error else 200, rules=rules_raw, preview_url=preview_url,
                  exceptions=exceptions_raw, data=data, result=result, error=error, action=action, levels=LEVELS,
                  level_labels=LEVEL_LABELS, short_labels=SHORT_LABELS, max_combinations=MAX_COMBINATIONS,
                  **_lookups(db))


@router.post("/recalculate-occupancy")
def recalc_occupancy(db: Session = Depends(get_db)):
    refresh_occupancy(db, None)
    db.commit()
    return redirect("/locations", "Ocupação de todos os endereços recalculada a partir dos saldos.")


@router.post("/bulk-delete")
def bulk_delete(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    ids = [i for i in (to_int(v) for v in form.getlist("ids")) if i]
    back = f_str(form, "back") or "/locations"
    if not back.startswith("/locations"):
        back = "/locations"
    if not ids:
        return redirect(back, "Nenhum endereço selecionado.", "warning")
    deletable = list(db.scalars(
        select(StorageLocation.id).where(
            StorageLocation.id.in_(ids),
            StorageLocation.is_occupied.is_(False),
            ~has_items_expr(StorageLocation.id, correlate=StorageLocation),
        )
    ))
    lv_msg = ""
    if deletable:
        keys = [level_key_of(l) for l in db.scalars(select(StorageLocation).where(StorageLocation.id.in_(deletable)))]
        db.execute(delete(StorageLocation).where(StorageLocation.id.in_(deletable)))
        if f_str(form, "drop_levels"):
            lv_msg = _levels_msg(*drop_unused_levels(db, keys))
        db.commit()
    skipped = len(ids) - len(deletable)
    msg = f"{len(deletable)} endereço(s) excluído(s)." + lv_msg
    if skipped:
        msg += f" {skipped} ignorado(s) por estarem ocupados (com item atrelado)."
    return redirect(back, msg, "warning" if skipped else "success")


def _levels_msg(dropped: list[str], kept: list[str]) -> str:
    def few(xs):
        return ", ".join(xs[:8]) + ("…" if len(xs) > 8 else "")
    out = f" {len(dropped)} posição(ões) física(s) (nível) excluída(s): {few(dropped)}." if dropped else ""
    if kept:
        out += f" Mantida(s): {few(kept)}."
    return out


@router.get("/{location_id}/edit")
def edit_location(location_id: int, request: Request, db: Session = Depends(get_db)):
    loc = db.get(StorageLocation, location_id)
    if not loc:
        return redirect("/locations", "Endereço não encontrado.", "danger")
    data = {k: getattr(loc, k) for k in ("branch_id", "warehouse_id", "aisle", "bay", "level", "position", "compartment")}
    data["bins"] = [(lb.bin_type_id, lb.quantity, lb.shelf_level_id) for lb in loc.bins]
    items = location_contents(db, [location_id]).get(location_id, [])
    return render(request, "locations/form.html", location=loc, data=data, errors={}, items=items,
                  **_bin_context(db, loc), **_level_context(db, loc), **_lookups(db))


def _bin_context(db: Session, loc, error: str | None = None) -> dict:
    from services.bin_content_service import contents_view

    stock_items = list(db.scalars(select(StockItem).options(joinedload(StockItem.product))
                                  .where(StockItem.location_id == loc.id, StockItem.quantity > 0)))
    from routers.packing import Arranger

    from routers.packing import case_url

    view = contents_view(db, loc.id)
    arranger = Arranger(db)
    tags = [f"{v['type'].code if v['type'] else '?'} #{v['bin'].seq}" + (f" ↳{v['bin'].level}" if v['bin'].level else "") for v in view]
    case = case_url(f"endereço {loc.address}", "upright",
                    [("before", v["bin"].type_id, tags[i], [(c.product.sku, c.quantity) for c in v["contents"]])
                     for i, v in enumerate(view)])
    for i, v in enumerate(view):
        items = [(c.product.sku, c.quantity) for c in v["contents"]]
        v["pack"] = arranger.view(v["bin"].type_id, items, f"{loc.address} · {tags[i]}", 60, seq=v["bin"].seq) if items else None
        if v["pack"]:
            v["pack"]["url"] = f"{case}#b-before-{i}"
    in_bins: dict[int, float] = {}
    for v in view:
        for c in v["contents"]:
            in_bins[c.product_id] = in_bins.get(c.product_id, 0) + float(c.quantity)
    loose = [(si.product.sku, float(si.quantity) - in_bins.get(si.product_id, 0)) for si in stock_items
             if float(si.quantity) - in_bins.get(si.product_id, 0) > 1e-9]
    return {"bin_view": view, "stock_items": stock_items, "content_error": error, "loose": loose}


@router.post("/{location_id}/contents")
def save_contents(location_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    from services.bin_content_service import refresh_contents, set_contents

    loc = db.get(StorageLocation, location_id)
    if not loc:
        return redirect("/locations", "Endereço não encontrado.", "danger")
    back = f"/locations/{location_id}/edit"
    if form.get("auto"):
        refresh_contents(db, [location_id], force=True)
        db.commit()
        return redirect(back + "#conteudo", "Conteúdo das bins recalculado automaticamente.")
    rows = []
    for b, p, q in zip(form.getlist("c_bin"), form.getlist("c_product"), form.getlist("c_qty")):
        try:
            t, seq = (int(x) for x in str(b).split(":"))
            qty = parse_decimal(q)
        except ValueError:
            return redirect(back + "#conteudo", "Linha de conteúdo inválida.", "danger")
        if qty:
            rows.append((t, seq, to_int(p), qty))
    err = set_contents(db, location_id, rows)
    if err:
        db.rollback()
        data = {k: getattr(loc, k) for k in ("branch_id", "warehouse_id", "aisle", "bay", "level", "position", "compartment")}
        data["bins"] = [(lb.bin_type_id, lb.quantity, lb.shelf_level_id) for lb in loc.bins]
        items = location_contents(db, [location_id]).get(location_id, [])
        return render(request, "locations/form.html", status_code=422, location=loc, data=data, errors={}, items=items,
                      **_bin_context(db, loc, err), **_level_context(db, loc), **_lookups(db))
    db.commit()
    return redirect(back + "#conteudo", "Conteúdo das bins salvo.")


@router.post("/{location_id}/edit")
def update_location(location_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    loc = db.get(StorageLocation, location_id)
    if not loc:
        return redirect("/locations", "Endereço não encontrado.", "danger")
    errors, data = _validate(db, form, location_id)
    physical_change = any(data[k] != getattr(loc, k) for k in ("branch_id", "warehouse_id", "aisle", "bay", "level", "position", "compartment"))
    if not errors and physical_change and _has_positive_stock(db, location_id):
        errors.add("__all__", "Endereço com saldo não pode ter filial/armazém/coordenadas alterados. Transfira o saldo antes.")
    if errors:
        items = location_contents(db, [location_id]).get(location_id, [])
        return render(request, "locations/form.html", status_code=422, location=loc, data=data, errors=errors, items=items,
                      **_bin_context(db, loc), **_level_context(db, loc), **_lookups(db))
    bin_items = data.pop("bins")
    for k, v in data.items():
        setattr(loc, k, v)
    loc.address = build_address(loc.aisle, loc.bay, loc.level, loc.position, loc.compartment)
    set_location_bins(db, loc.id, bin_items)
    refresh_occupancy(db, [loc.id])
    db.commit()
    msg, lvl = _with_capacity_warnings(db, f"Endereço {loc.address} atualizado.", _affected_levels(db, loc, bin_items))
    return redirect("/locations", msg, lvl)


@router.post("/{location_id}/delete")
def delete_location(location_id: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    loc = db.get(StorageLocation, location_id)
    if not loc:
        return redirect("/locations", "Endereço não encontrado.", "danger")
    if loc.is_occupied or location_has_items(db, location_id):
        return redirect("/locations", f"Endereço {loc.address} está ocupado (tem item atrelado) e não pode ser excluído.", "danger")
    address, key = loc.address, level_key_of(loc)
    db.execute(delete(StorageLocation).where(StorageLocation.id == location_id))
    lv_msg = _levels_msg(*drop_unused_levels(db, [key])) if f_str(form, "drop_levels") else ""
    db.commit()
    back = f_str(form, "back") or "/locations"
    return redirect(back if back.startswith("/locations") else "/locations", f"Endereço {address} excluído." + lv_msg)
