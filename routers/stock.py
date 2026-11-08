"""Consulta e ajuste manual de saldos físicos (StockItem)."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import FormData

from database import get_db
from models import Branch, Product, StockItem, StorageLocation, Warehouse
from routers.common import (
    FormErrors,
    apply_sort,
    f_dec,
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
from services.addressing import normalize_code
from services.material_service import material_condition
from services.bin_content_service import refresh_contents
from services.stock_service import refresh_occupancy, sync_default_locations

router = APIRouter(prefix="/stock", tags=["Estoque"])


def _lookups(db: Session) -> dict:
    return {
        "branches": list(db.scalars(select(Branch).order_by(Branch.code))),
        "warehouses": list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
    }


def filtered_stock(p):
    """select(StockItem) com os filtros da lista de saldos (também usado na exclusão de "todos do filtro")."""
    L = StorageLocation
    base = (
        select(StockItem)
        .join(Product, Product.id == StockItem.product_id)
        .join(L, L.id == StockItem.location_id)
        .join(Branch, Branch.id == L.branch_id)
        .outerjoin(Warehouse, Warehouse.id == L.warehouse_id)
    )
    bid, wid = to_int(p.get("branch_id")), to_int(p.get("warehouse_id"))
    if bid:
        base = base.where(L.branch_id == bid)
    if wid:
        base = base.where(L.warehouse_id == wid)
    base = text_search(base, p.get("sku") or "", Product.sku, Product.name, fields=sku_name_fields(Product))
    cond = material_condition(p.get("material") or "")
    if cond is not None:
        base = base.where(cond)
    base = text_search(base, p.get("address") or "", L.address, address_columns=(L.address,))
    if (p.get("only_positive") if p.get("only_positive") is not None else "1") == "1":
        base = base.where(StockItem.quantity != Decimal("0"))
    return base


@router.get("")
def list_stock(
    request: Request,
    branch_id: str = "",
    warehouse_id: str = "",
    sku: str = "",
    material: str = "",
    address: str = "",
    only_positive: str = "1",
    sort: str = "",
    dir: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    L = StorageLocation
    base = filtered_stock({"branch_id": branch_id, "warehouse_id": warehouse_id, "sku": sku, "material": material,
                           "address": address, "only_positive": only_positive})
    physical = (L.aisle, L.bay, L.level, L.position, L.compartment)
    stmt = base.options(
        joinedload(StockItem.product),
        joinedload(StockItem.location).joinedload(L.branch),
        joinedload(StockItem.location).joinedload(L.warehouse),
    )
    stmt, srt = apply_sort(stmt, sort, dir, {
        "sku": (Product.sku, *physical), "name": (Product.name, Product.sku),
        "material": (func.nullif(Product.material_type, ""), Product.sku, *physical),
        "branch": (Branch.code, Warehouse.code, *physical), "warehouse": (Warehouse.code, Branch.code, *physical),
        "address": (*physical, Product.sku),
        "quantity": StockItem.quantity, "updated": StockItem.updated_at,
    }, default="sku", tiebreak=StockItem.id)
    pg = paginate(db, stmt, page)
    total_qty = db.scalar(base.with_only_columns(func.coalesce(func.sum(StockItem.quantity), Decimal("0"))))
    return render(request, "stock/list.html", page=pg, branch_id=to_int(branch_id), warehouse_id=to_int(warehouse_id), sku=sku, material=material, address=address,
                  only_positive=only_positive, total_qty=total_qty, sort=srt, **_lookups(db))


@router.get("/new")
def new_stock(request: Request):
    return render(request, "stock/form.html", item=None, data={}, errors={})


@router.post("/new")
def create_stock(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors = FormErrors()
    sku = normalize_code(f_str(form, "sku"))
    data = {"sku": sku, "location_id": f_int(form, "location_id"), "location_label": f_str(form, "location_label")}
    product = db.scalar(select(Product).where(Product.sku == sku)) if sku else None
    if not product:
        errors.add("sku", "SKU não encontrado.")
    loc = db.get(StorageLocation, data["location_id"]) if data["location_id"] else None
    if not loc:
        errors.add("location_id", "Selecione um endereço válido.")
    qty = f_dec(form, "quantity", errors, "Quantidade")
    data["quantity"] = qty
    if qty is None and "quantity" not in errors:
        errors.add("quantity", "Quantidade é obrigatória.")
    if not errors and db.scalar(select(StockItem.id).where(StockItem.product_id == product.id, StockItem.location_id == loc.id)):
        errors.add("__all__", "Já existe saldo deste produto neste endereço. Edite o registro existente.")
    if errors:
        return render(request, "stock/form.html", status_code=422, item=None, data=data, errors=errors)
    db.add(StockItem(product_id=product.id, location_id=loc.id, quantity=qty))
    db.flush()
    refresh_occupancy(db, [loc.id])
    sync_default_locations(db, [product.id])
    refresh_contents(db, [loc.id])
    db.commit()
    return redirect("/stock", f"Saldo de {sku} em {loc.address} registrado.")


@router.get("/{item_id}/edit")
def edit_stock(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = db.get(StockItem, item_id)
    if not item:
        return redirect("/stock", "Saldo não encontrado.", "danger")
    return render(request, "stock/form.html", item=item, data={"quantity": item.quantity}, errors={})


@router.post("/{item_id}/edit")
def update_stock(item_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    item = db.get(StockItem, item_id)
    if not item:
        return redirect("/stock", "Saldo não encontrado.", "danger")
    errors = FormErrors()
    qty = f_dec(form, "quantity", errors, "Quantidade")
    if qty is None and "quantity" not in errors:
        errors.add("quantity", "Quantidade é obrigatória.")
    if errors:
        return render(request, "stock/form.html", status_code=422, item=item, data={"quantity": form.get("quantity")}, errors=errors)
    item.quantity = qty
    db.flush()
    refresh_occupancy(db, [item.location_id])
    sync_default_locations(db, [item.product_id])
    refresh_contents(db, [item.location_id])
    db.commit()
    return redirect("/stock", f"Saldo de {item.product.sku} em {item.location.address} atualizado.")


@router.post("/{item_id}/delete")
def delete_stock(item_id: int, db: Session = Depends(get_db)):
    item = db.get(StockItem, item_id)
    if not item:
        return redirect("/stock", "Saldo não encontrado.", "danger")
    loc_id, pid = item.location_id, item.product_id
    db.delete(item)
    db.flush()
    refresh_occupancy(db, [loc_id])
    sync_default_locations(db, [pid])
    refresh_contents(db, [loc_id])
    db.commit()
    return redirect("/stock", "Registro de saldo excluído.")
