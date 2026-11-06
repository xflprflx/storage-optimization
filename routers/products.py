"""CRUD de Produtos (com dimensões físicas embutidas)."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, joinedload
from starlette.datastructures import FormData

from database import get_db
from models import Product, StockItem, StorageLocation
from routers.common import (
    SEARCH_MODES,
    FormErrors,
    apply_sort,
    f_dec,
    f_int,
    f_str,
    form_data,
    paginate,
    redirect,
    render,
    text_search,
)
from services.addressing import normalize_code
from services.material_service import MaterialResolver, material_condition
from services.stock_service import refresh_occupancy, sync_default_locations

router = APIRouter(prefix="/products", tags=["Produtos"])

DIM_FIELDS = (("height", "Altura"), ("width", "Largura"), ("length", "Comprimento"), ("weight", "Peso"))
LEVEL_FIELDS = (("min_level", "Nível mínimo"), ("max_level", "Nível máximo"))


def _validate(db: Session, form: FormData, product_id: int | None):
    errors = FormErrors()
    data = {
        "sku": normalize_code(f_str(form, "sku")),
        "name": f_str(form, "name", 255),
        "material_type": MaterialResolver(db)(f_str(form, "material_type")),
        "default_location_id": f_int(form, "default_location_id"),
    }
    if not data["sku"]:
        errors.add("sku", "SKU é obrigatório.")
    elif len(data["sku"]) > 60:
        errors.add("sku", "SKU deve ter no máximo 60 caracteres.")
    else:
        stmt = select(Product.id).where(Product.sku == data["sku"])
        if product_id:
            stmt = stmt.where(Product.id != product_id)
        if db.scalar(stmt):
            errors.add("sku", f"SKU {data['sku']} já cadastrado.")
    if not data["name"]:
        errors.add("name", "Nome é obrigatório.")
    for key, label in DIM_FIELDS + LEVEL_FIELDS:
        data[key] = f_dec(form, key, errors, label)
    if (data.get("min_level") is not None and data.get("max_level") is not None
            and data["min_level"] > data["max_level"]):
        errors.add("max_level", "Nível máximo deve ser maior ou igual ao mínimo.")
    if data["default_location_id"] and not db.get(StorageLocation, data["default_location_id"]):
        errors.add("default_location_id", "Endereço padrão inválido.")
    data["default_location_label"] = f_str(form, "default_location_label")
    return errors, data


SEARCH_FIELDS = {"": "Tudo", "sku": "SKU", "name": "Nome", "material": "Tipo de material", "address": "Endereço"}


def _search_fields(DefLoc, stock_addresses) -> dict:
    return {"sku": ((Product.sku,), ()), "name": ((Product.name,), ()), "material": ((Product.material_type,), ()),
            "address": ((DefLoc.address, stock_addresses), (DefLoc.address, stock_addresses))}


def _filtered(q: str, material: str, field: str = "", mode: str = "contains"):
    """Produtos que a busca da lista mostra (mesma regra da tela)."""
    DefLoc = aliased(StorageLocation)
    SL = aliased(StorageLocation)
    stock_addresses = (select(func.group_concat(SL.address, " ")).select_from(StockItem)
                       .join(SL, SL.id == StockItem.location_id)
                       .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    stmt = select(Product.id).outerjoin(DefLoc, DefLoc.id == Product.default_location_id)
    stmt = text_search(stmt, q, Product.sku, Product.name, Product.material_type, DefLoc.address, stock_addresses,
                       address_columns=(DefLoc.address, stock_addresses), mode=mode, only=field or None,
                       fields=_search_fields(DefLoc, stock_addresses))
    cond = material_condition(material)
    return stmt.where(cond) if cond is not None else stmt


@router.post("/bulk-dims")
def bulk_dims(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Mesmas dimensões e peso para vários produtos (campo vazio = não muda)."""
    from routers.common import to_int

    back = f_str(form, "back") or "/products"
    if not back.startswith("/products"):
        back = "/products"
    errors = FormErrors()
    vals = {k: f_dec(form, k, errors, label) for k, label in DIM_FIELDS}
    model_sku = normalize_code(f_str(form, "copy_sku"))
    if model_sku:
        src = db.scalar(select(Product).where(Product.sku == model_sku))
        if src is None:
            return redirect(back, f"Produto modelo {model_sku} não encontrado.", "danger")
        for k, _l in DIM_FIELDS:
            if vals[k] is None:
                vals[k] = getattr(src, k)
    if errors:
        return redirect(back, " ".join(m for ms in errors.values() for m in (ms if isinstance(ms, list) else [ms])), "danger")
    for k, label in DIM_FIELDS:
        if vals[k] is not None and vals[k] == 0:
            return redirect(back, f"{label}: informe um valor maior que zero (vazio = não muda).", "danger")
    vals = {k: v for k, v in vals.items() if v is not None}
    if not vals:
        return redirect(back, "Informe ao menos uma medida (altura, largura, comprimento ou peso) ou um produto modelo.", "warning")
    if f_str(form, "scope") == "filtered":
        ids = list(db.scalars(_filtered(f_str(form, "f_q"), f_str(form, "f_material"), f_str(form, "f_field"),
                                        f_str(form, "f_mode") or "contains")))
    else:
        ids = [i for i in (to_int(v) for v in form.getlist("ids")) if i]
    if not ids:
        return redirect(back, "Nenhum produto selecionado.", "warning")
    n = 0
    for i in range(0, len(ids), 900):
        for prod in db.scalars(select(Product).where(Product.id.in_(ids[i:i + 900]))):
            if any(getattr(prod, k) != v for k, v in vals.items()):
                for k, v in vals.items():
                    setattr(prod, k, v)
                n += 1
    db.flush()
    # a distribuição presumida nas bins depende das medidas: recalcula onde esses produtos estão
    from services.bin_content_service import refresh_contents

    locs = set()
    for i in range(0, len(ids), 900):
        locs |= set(db.scalars(select(StockItem.location_id).where(StockItem.product_id.in_(ids[i:i + 900]),
                                                                  StockItem.quantity > 0)))
    if locs:
        refresh_contents(db, sorted(locs))
    db.commit()
    labels = dict(DIM_FIELDS)
    unit = {"weight": " kg"}
    what = ", ".join(f"{labels[k].lower()} {float(v):g}{unit.get(k, ' cm')}".replace(".", ",") for k, v in vals.items())
    return redirect(back, f"{n} produto(s) atualizado(s) ({what})." + (f" {len(ids) - n} já tinham essas medidas." if len(ids) > n else ""))


@router.get("")
def list_products(request: Request, q: str = "", material: str = "", sort: str = "", dir: str = "", page: int = 1,
                  field: str = "", mode: str = "contains", db: Session = Depends(get_db)):
    field = field if field in SEARCH_FIELDS else ""
    mode = mode if mode in SEARCH_MODES else "contains"
    DefLoc = aliased(StorageLocation)
    total_stock = (select(func.coalesce(func.sum(StockItem.quantity), Decimal("0")))
                   .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    stmt = (
        select(Product)
        .outerjoin(DefLoc, DefLoc.id == Product.default_location_id)
        .options(
            joinedload(Product.default_location).joinedload(StorageLocation.branch),
            joinedload(Product.default_location).joinedload(StorageLocation.warehouse),
        )
    )
    # Endereços onde o produto está (saldo) — usados na busca e na ordenação
    SL = aliased(StorageLocation)
    stock_addresses = (select(func.group_concat(SL.address, " ")).select_from(StockItem)
                       .join(SL, SL.id == StockItem.location_id)
                       .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    first_stock_address = (select(func.min(SL.address)).select_from(StockItem)
                           .join(SL, SL.id == StockItem.location_id)
                           .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    stmt = text_search(stmt, q, Product.sku, Product.name, Product.material_type, DefLoc.address, stock_addresses,
                       address_columns=(DefLoc.address, stock_addresses), mode=mode, only=field or None,
                       fields=_search_fields(DefLoc, stock_addresses))
    cond = material_condition(material)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt, srt = apply_sort(stmt, sort, dir, {
        "sku": Product.sku, "name": Product.name, "material": (func.nullif(Product.material_type, ""), Product.sku),
        "location": (func.coalesce(DefLoc.address, first_stock_address), Product.sku),
        "height": Product.height, "width": Product.width, "length": Product.length, "weight": Product.weight,
        "min_level": Product.min_level, "max_level": Product.max_level,
        "total": total_stock,
    }, default="sku", tiebreak=Product.id)
    pg = paginate(db, stmt, page)
    ids = [p.id for p in pg.items]
    totals = dict(
        db.execute(
            select(StockItem.product_id, func.sum(StockItem.quantity)).where(StockItem.product_id.in_(ids))
            .group_by(StockItem.product_id)
        ).all()
    ) if ids else {}
    return render(request, "products/list.html", page=pg, q=q, material=material, sort=srt, totals=totals,
                  field=field, mode=mode, search_fields=SEARCH_FIELDS, search_modes=SEARCH_MODES,
                  places=product_places(db, pg.items))


def product_places(db: Session, products) -> dict[int, list[dict]]:
    """Endereços de cada produto: onde há saldo lançado (com a quantidade) + o endereço padrão.
    O endereço padrão que coincide com um endereço de saldo é marcado nele (não repete)."""
    from models import Branch, Warehouse

    ids = [p.id for p in products]
    out: dict[int, list[dict]] = {i: [] for i in ids}
    if not ids:
        return out
    for pid, lid, addr, b, w, qty in db.execute(
        select(StockItem.product_id, StorageLocation.id, StorageLocation.address, Branch.code, Warehouse.code,
               StockItem.quantity)
        .join(StorageLocation, StorageLocation.id == StockItem.location_id)
        .join(Branch, Branch.id == StorageLocation.branch_id)
        .outerjoin(Warehouse, Warehouse.id == StorageLocation.warehouse_id)
        .where(StockItem.product_id.in_(ids))
        .order_by(StockItem.product_id, StockItem.quantity.desc(), StorageLocation.address)
    ):
        out[pid].append({"id": lid, "address": addr, "scope": f"{b} / {w}" if w else b, "qty": qty, "default": False})
    for p in products:
        loc = p.default_location
        if loc is None:
            continue
        hit = next((x for x in out[p.id] if x["id"] == loc.id), None)
        if hit:
            hit["default"] = True
            out[p.id].remove(hit)
            out[p.id].insert(0, hit)
        else:
            out[p.id].insert(0, {"id": loc.id, "address": loc.address, "scope": loc.scope_label, "qty": None,
                                 "default": True})
    return out


@router.get("/new")
def new_product(request: Request):
    return render(request, "products/form.html", product=None, data={}, errors={}, dim_fields=DIM_FIELDS)


@router.post("/new")
def create_product(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "products/form.html", status_code=422, product=None, data=data, errors=errors, dim_fields=DIM_FIELDS)
    data.pop("default_location_label", None)
    prod = Product(**data)
    db.add(prod)
    db.flush()
    refresh_occupancy(db, [prod.default_location_id])
    sync_default_locations(db, [prod.id])
    db.commit()
    return redirect(f"/products/{prod.id}", f"Produto {prod.sku} criado.")


@router.get("/{product_id}")
def product_detail(product_id: int, request: Request, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", "Produto não encontrado.", "danger")
    stock = list(db.scalars(
        select(StockItem).join(StorageLocation)
        .options(joinedload(StockItem.location).joinedload(StorageLocation.branch),
                 joinedload(StockItem.location).joinedload(StorageLocation.warehouse))
        .where(StockItem.product_id == product_id)
        .order_by(StorageLocation.branch_id, StorageLocation.warehouse_id, StorageLocation.address)
    ))
    total = sum((s.quantity for s in stock), Decimal("0"))
    return render(request, "products/detail.html", product=prod, stock=stock, total=total)


@router.get("/{product_id}/edit")
def edit_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", "Produto não encontrado.", "danger")
    data = {k: getattr(prod, k) for k in ("sku", "name", "material_type", "default_location_id", "height", "width", "length", "weight", "min_level", "max_level")}
    data["default_location_label"] = prod.default_location.full_label if prod.default_location else ""
    return render(request, "products/form.html", product=prod, data=data, errors={}, dim_fields=DIM_FIELDS)


@router.post("/{product_id}/edit")
def update_product(product_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", "Produto não encontrado.", "danger")
    errors, data = _validate(db, form, product_id)
    if errors:
        return render(request, "products/form.html", status_code=422, product=prod, data=data, errors=errors, dim_fields=DIM_FIELDS)
    data.pop("default_location_label", None)
    old_location = prod.default_location_id
    for k, v in data.items():
        setattr(prod, k, v)
    db.flush()
    refresh_occupancy(db, [old_location, prod.default_location_id])
    chosen = prod.default_location_id
    if sync_default_locations(db, [prod.id]):
        db.commit()
        db.refresh(prod)
        msg = (f"Produto {prod.sku} atualizado. O endereço padrão é o endereço onde o produto está: "
               f"ficou {prod.default_location.address}" + (" (o endereço escolhido não tem saldo deste produto)." if chosen else "."))
        return redirect(f"/products/{prod.id}", msg, "warning" if chosen else "success")
    db.commit()
    return redirect(f"/products/{prod.id}", f"Produto {prod.sku} atualizado.")


@router.post("/{product_id}/delete")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", "Produto não encontrado.", "danger")
    location_ids = list(db.scalars(select(StockItem.location_id).where(StockItem.product_id == product_id)))
    location_ids.append(prod.default_location_id)
    sku = prod.sku
    db.delete(prod)  # cascata: StockItems
    db.flush()
    refresh_occupancy(db, location_ids)
    db.commit()
    return redirect("/products", f"Produto {sku} excluído (saldos removidos).")
