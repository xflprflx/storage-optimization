"""CRUD de Armazéns (Warehouses) com seleção de Filiais (Many-to-Many)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from database import get_db
from models import Branch, StorageLocation, Warehouse, branch_warehouse
from routers.common import (
    FormErrors,
    apply_sort,
    f_str,
    form_data,
    paginate,
    redirect,
    render,
    text_search,
    to_int,
)
from services.addressing import normalize_code
from services.i18n import N_, _

router = APIRouter(prefix="/warehouses", tags=[N_("Armazéns")])


def _all_branches(db: Session) -> list[Branch]:
    return list(db.scalars(select(Branch).order_by(Branch.code)))


def _validate(db: Session, form: FormData, warehouse_id: int | None):
    errors = FormErrors()
    code = normalize_code(f_str(form, "code"))
    name = f_str(form, "name", 150)
    if not code:
        errors.add("code", _("Código é obrigatório."))
    elif len(code) > 30:
        errors.add("code", _("Código deve ter no máximo 30 caracteres."))
    else:
        stmt = select(Warehouse.id).where(Warehouse.code == code)
        if warehouse_id:
            stmt = stmt.where(Warehouse.id != warehouse_id)
        if db.scalar(stmt):
            errors.add("code", _("Já existe um armazém com o código {code}.", code=code))
    if not name:
        errors.add("name", _("Nome é obrigatório."))
    branch_ids = [i for i in (to_int(v) for v in form.getlist("branch_ids")) if i]
    return errors, {"code": code, "name": name, "branch_ids": branch_ids}


@router.get("")
def list_warehouses(request: Request, q: str = "", branch_id: str = "", sort: str = "", dir: str = "",
                    page: int = 1, db: Session = Depends(get_db)):
    br_count = (select(func.count()).select_from(branch_warehouse)
                .where(branch_warehouse.c.warehouse_id == Warehouse.id).correlate(Warehouse).scalar_subquery())
    first_branch = (select(func.min(Branch.code)).join(branch_warehouse, branch_warehouse.c.branch_id == Branch.id)
                    .where(branch_warehouse.c.warehouse_id == Warehouse.id).correlate(Warehouse).scalar_subquery())
    loc_count = (select(func.count()).select_from(StorageLocation)
                 .where(StorageLocation.warehouse_id == Warehouse.id).correlate(Warehouse).scalar_subquery())
    stmt = select(Warehouse).options(selectinload(Warehouse.branches))
    stmt = text_search(stmt, q, Warehouse.code, Warehouse.name)
    bid = to_int(branch_id)
    if bid:
        stmt = stmt.where(Warehouse.id.in_(select(branch_warehouse.c.warehouse_id).where(branch_warehouse.c.branch_id == bid)))
    stmt, srt = apply_sort(stmt, sort, dir, {
        "code": Warehouse.code, "name": Warehouse.name, "branches": (br_count, first_branch), "locations": loc_count,
    }, default="code", tiebreak=Warehouse.id)
    pg = paginate(db, stmt, page)
    ids = [w.id for w in pg.items]
    loc_counts = dict(
        db.execute(
            select(StorageLocation.warehouse_id, func.count()).where(StorageLocation.warehouse_id.in_(ids))
            .group_by(StorageLocation.warehouse_id)
        ).all()
    ) if ids else {}
    return render(request, "warehouses/list.html", page=pg, q=q, branch_id=bid, sort=srt, branches=_all_branches(db),
                  loc_counts=loc_counts)


@router.get("/new")
def new_warehouse(request: Request, db: Session = Depends(get_db)):
    return render(request, "warehouses/form.html", warehouse=None, data={"branch_ids": []}, errors={},
                  branches=_all_branches(db))


@router.post("/new")
def create_warehouse(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "warehouses/form.html", status_code=422, warehouse=None, data=data, errors=errors,
                      branches=_all_branches(db))
    wh = Warehouse(code=data["code"], name=data["name"])
    if data["branch_ids"]:
        wh.branches = list(db.scalars(select(Branch).where(Branch.id.in_(data["branch_ids"]))))
    db.add(wh)
    db.commit()
    return redirect("/warehouses", _("Armazém {code} criado com sucesso.", code=wh.code))


@router.get("/{warehouse_id}/edit")
def edit_warehouse(warehouse_id: int, request: Request, db: Session = Depends(get_db)):
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        return redirect("/warehouses", _("Armazém não encontrado."), "danger")
    data = {"code": wh.code, "name": wh.name, "branch_ids": [b.id for b in wh.branches]}
    return render(request, "warehouses/form.html", warehouse=wh, data=data, errors={}, branches=_all_branches(db))


@router.post("/{warehouse_id}/edit")
def update_warehouse(warehouse_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        return redirect("/warehouses", _("Armazém não encontrado."), "danger")
    errors, data = _validate(db, form, warehouse_id)
    current = {b.id for b in wh.branches}
    removed = current - set(data["branch_ids"])
    if removed:
        blocked = db.scalars(
            select(Branch.code).join(StorageLocation, StorageLocation.branch_id == Branch.id)
            .where(StorageLocation.warehouse_id == warehouse_id, Branch.id.in_(removed)).distinct()
        ).all()
        if blocked:
            errors.add("branch_ids", _("Não é possível desvincular filiais que possuem endereços neste armazém: {v}.", v=', '.join(blocked)))
    if errors:
        return render(request, "warehouses/form.html", status_code=422, warehouse=wh, data=data, errors=errors,
                      branches=_all_branches(db))
    wh.code, wh.name = data["code"], data["name"]
    wh.branches = list(db.scalars(select(Branch).where(Branch.id.in_(data["branch_ids"])))) if data["branch_ids"] else []
    db.commit()
    return redirect("/warehouses", _("Armazém {code} atualizado.", code=wh.code))


@router.post("/{warehouse_id}/delete")
def delete_warehouse(warehouse_id: int, db: Session = Depends(get_db)):
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        return redirect("/warehouses", _("Armazém não encontrado."), "danger")
    n_locs = db.scalar(select(func.count()).where(StorageLocation.warehouse_id == warehouse_id))
    if n_locs:
        return redirect("/warehouses", _("Armazém {code} possui {n_locs} endereço(s). Exclua os endereços antes de excluir o armazém.", code=wh.code, n_locs=n_locs), "danger")
    code = wh.code
    db.delete(wh)
    db.commit()
    return redirect("/warehouses", _("Armazém {code} excluído.", code=code))
