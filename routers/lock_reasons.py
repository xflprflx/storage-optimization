"""Cadastro dos motivos de estante travada (posição fixa / não recompor)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import BayLock, LockReason
from routers.common import FormErrors, apply_sort, f_str, form_data, paginate, redirect, render, text_search
from services.i18n import N_, _

router = APIRouter(prefix="/lock-reasons", tags=[N_("Motivos de estante travada")])


def _usage(db: Session) -> dict[int, int]:
    return dict(db.execute(select(BayLock.reason_id, func.count(BayLock.id)).where(BayLock.reason_id.is_not(None))
                           .group_by(BayLock.reason_id)).all())


def _validate(db: Session, form: FormData, rid: int | None):
    errors = FormErrors()
    data = {"name": f_str(form, "name", 100), "notes": f_str(form, "notes", 200)}
    if not data["name"]:
        errors.add("name", _("Nome é obrigatório."))
    else:
        stmt = select(LockReason.id).where(func.lower(LockReason.name) == data["name"].lower())
        if rid:
            stmt = stmt.where(LockReason.id != rid)
        if db.scalar(stmt):
            errors.add("name", _("Já existe um motivo com o nome {name}.", name=data["name"]))
    return errors, data


@router.get("")
def list_reasons(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1, db: Session = Depends(get_db)):
    stmt = text_search(select(LockReason), q, LockReason.name, LockReason.notes)
    stmt, srt = apply_sort(stmt, sort, dir, {"name": LockReason.name}, default="name", tiebreak=LockReason.id)
    return render(request, "lock_reasons/list.html", page=paginate(db, stmt, page), q=q, sort=srt, usage=_usage(db))


@router.get("/new")
def new_reason(request: Request):
    return render(request, "lock_reasons/form.html", item=None, data={}, errors={})


@router.post("/new")
def create_reason(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "lock_reasons/form.html", status_code=422, item=None, data=data, errors=errors)
    r = LockReason(**data)
    db.add(r)
    db.commit()
    back = str(form.get("back") or "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/lock-reasons",
                    _("Motivo {name} criado.", name=r.name))


@router.get("/{rid}/edit")
def edit_reason(rid: int, request: Request, db: Session = Depends(get_db)):
    r = db.get(LockReason, rid)
    if not r:
        return redirect("/lock-reasons", _("Motivo não encontrado."), "danger")
    return render(request, "lock_reasons/form.html", item=r, data={"name": r.name, "notes": r.notes}, errors={})


@router.post("/{rid}/edit")
def update_reason(rid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    r = db.get(LockReason, rid)
    if not r:
        return redirect("/lock-reasons", _("Motivo não encontrado."), "danger")
    errors, data = _validate(db, form, rid)
    if errors:
        return render(request, "lock_reasons/form.html", status_code=422, item=r, data=data, errors=errors)
    for k, v in data.items():
        setattr(r, k, v)
    db.commit()
    return redirect("/lock-reasons", _("Motivo {name} atualizado.", name=r.name))


@router.post("/{rid}/delete")
def delete_reason(rid: int, db: Session = Depends(get_db)):
    r = db.get(LockReason, rid)
    if not r:
        return redirect("/lock-reasons", _("Motivo não encontrado."), "danger")
    n = _usage(db).get(r.id, 0)
    if n:
        return redirect("/lock-reasons", _("O motivo {name} está em {n} estante(s) travada(s): troque o motivo delas antes de excluir.", name=r.name, n=n), "danger")
    name = r.name
    db.delete(r)
    db.commit()
    return redirect("/lock-reasons", _("Motivo {name} excluído.", name=name))
