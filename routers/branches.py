"""CRUD de Filiais (Branches)."""
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

router = APIRouter(prefix="/branches", tags=[N_("Filiais")])


def _all_warehouses(db: Session) -> list[Warehouse]:
    return list(db.scalars(select(Warehouse).order_by(Warehouse.code)))


def _validate(db: Session, form: FormData, branch_id: int | None):
    errors = FormErrors()
    code = normalize_code(f_str(form, "code"))
    name = f_str(form, "name", 150)
    if not code:
        errors.add("code", _("Código é obrigatório."))
    elif len(code) > 30:
        errors.add("code", _("Código deve ter no máximo 30 caracteres."))
    else:
        stmt = select(Branch.id).where(Branch.code == code)
        if branch_id:
            stmt = stmt.where(Branch.id != branch_id)
        if db.scalar(stmt):
            errors.add("code", _("Já existe uma filial com o código {code}.", code=code))
    if not name:
        errors.add("name", _("Nome é obrigatório."))
    wh_ids = [i for i in (to_int(v) for v in form.getlist("warehouse_ids")) if i]
    return errors, {"code": code, "name": name, "warehouse_ids": wh_ids}


@router.get("")
def list_branches(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1,
                  db: Session = Depends(get_db)):
    wh_count = (select(func.count()).select_from(branch_warehouse)
                .where(branch_warehouse.c.branch_id == Branch.id).correlate(Branch).scalar_subquery())
    loc_count = (select(func.count()).select_from(StorageLocation)
                 .where(StorageLocation.branch_id == Branch.id).correlate(Branch).scalar_subquery())
    stmt = select(Branch).options(selectinload(Branch.warehouses))
    stmt = text_search(stmt, q, Branch.code, Branch.name)
    stmt, srt = apply_sort(stmt, sort, dir, {
        "code": Branch.code, "name": Branch.name, "warehouses": wh_count, "locations": loc_count,
    }, default="code", tiebreak=Branch.id)
    pg = paginate(db, stmt, page)
    ids = [b.id for b in pg.items]
    loc_counts = dict(
        db.execute(
            select(StorageLocation.branch_id, func.count()).where(StorageLocation.branch_id.in_(ids)).group_by(StorageLocation.branch_id)
        ).all()
    ) if ids else {}
    return render(request, "branches/list.html", page=pg, q=q, sort=srt, loc_counts=loc_counts)


@router.get("/new")
def new_branch(request: Request, db: Session = Depends(get_db)):
    return render(request, "branches/form.html", branch=None, data={"warehouse_ids": []}, errors={},
                  warehouses=_all_warehouses(db))


@router.post("/new")
def create_branch(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "branches/form.html", status_code=422, branch=None, data=data, errors=errors,
                      warehouses=_all_warehouses(db))
    branch = Branch(code=data["code"], name=data["name"])
    if data["warehouse_ids"]:
        branch.warehouses = list(db.scalars(select(Warehouse).where(Warehouse.id.in_(data["warehouse_ids"]))))
    db.add(branch)
    db.commit()
    return redirect("/branches", _("Filial {code} criada com sucesso.", code=branch.code))


@router.get("/{branch_id}/edit")
def edit_branch(branch_id: int, request: Request, db: Session = Depends(get_db)):
    branch = db.get(Branch, branch_id)
    if not branch:
        return redirect("/branches", _("Filial não encontrada."), "danger")
    data = {"code": branch.code, "name": branch.name, "warehouse_ids": [w.id for w in branch.warehouses]}
    return render(request, "branches/form.html", branch=branch, data=data, errors={}, warehouses=_all_warehouses(db))


@router.post("/{branch_id}/edit")
def update_branch(branch_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    branch = db.get(Branch, branch_id)
    if not branch:
        return redirect("/branches", _("Filial não encontrada."), "danger")
    errors, data = _validate(db, form, branch_id)
    # Não permite desvincular armazém que possui endereços nesta filial.
    current = {w.id for w in branch.warehouses}
    removed = current - set(data["warehouse_ids"])
    if removed:
        blocked = db.scalars(
            select(Warehouse.code).join(StorageLocation, StorageLocation.warehouse_id == Warehouse.id)
            .where(StorageLocation.branch_id == branch_id, Warehouse.id.in_(removed)).distinct()
        ).all()
        if blocked:
            errors.add("warehouse_ids", _("Não é possível desvincular armazéns com endereços nesta filial: {v}.", v=', '.join(blocked)))
    if errors:
        return render(request, "branches/form.html", status_code=422, branch=branch, data=data, errors=errors,
                      warehouses=_all_warehouses(db))
    branch.code, branch.name = data["code"], data["name"]
    branch.warehouses = list(db.scalars(select(Warehouse).where(Warehouse.id.in_(data["warehouse_ids"])))) if data["warehouse_ids"] else []
    db.commit()
    return redirect("/branches", _("Filial {code} atualizada.", code=branch.code))


@router.post("/{branch_id}/delete")
def delete_branch(branch_id: int, db: Session = Depends(get_db)):
    branch = db.get(Branch, branch_id)
    if not branch:
        return redirect("/branches", _("Filial não encontrada."), "danger")
    n_locs = db.scalar(select(func.count()).where(StorageLocation.branch_id == branch_id))
    if n_locs:
        return redirect("/branches", _("Filial {code} possui {n_locs} endereço(s) cadastrados. Exclua os endereços antes de excluir a filial.", code=branch.code, n_locs=n_locs), "danger")
    code = branch.code
    db.delete(branch)
    db.commit()
    return redirect("/branches", _("Filial {code} excluída.", code=code))
