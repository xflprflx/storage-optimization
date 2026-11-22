"""Cadastro dos modelos de embalagem (caixa padrão usada por vários produtos: medidas e tara)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import PackagingModel, ProductPackaging
from routers.common import FormErrors, apply_sort, f_dec, f_str, form_data, paginate, redirect, render, text_search
from services.addressing import normalize_code
from services.i18n import N_, _

router = APIRouter(prefix="/packaging-models", tags=[N_("Modelos de embalagem")])
FIELDS = (("height", N_("Altura")), ("width", N_("Largura")), ("length", N_("Comprimento")), ("tare", N_("Tara")))


def _usage(db: Session) -> dict[int, int]:
    return dict(db.execute(select(ProductPackaging.model_id, func.count(func.distinct(ProductPackaging.product_id)))
                           .where(ProductPackaging.model_id.is_not(None)).group_by(ProductPackaging.model_id)).all())


def _validate(db: Session, form: FormData, mid: int | None):
    errors = FormErrors()
    data = {"code": normalize_code(f_str(form, "code")), "name": f_str(form, "name", 150), "notes": f_str(form, "notes", 200)}
    if not data["code"]:
        errors.add("code", _("Código é obrigatório."))
    elif len(data["code"]) > 30:
        errors.add("code", _("Código deve ter no máximo 30 caracteres."))
    else:
        stmt = select(PackagingModel.id).where(PackagingModel.code == data["code"])
        if mid:
            stmt = stmt.where(PackagingModel.id != mid)
        if db.scalar(stmt):
            errors.add("code", _("Já existe um modelo de embalagem com o código {code}.", code=data["code"]))
    if not data["name"]:
        data["name"] = data["code"]
    for k, label in FIELDS:
        data[k] = f_dec(form, k, errors, _(label))
        if k != "tare" and data[k] is not None and data[k] <= 0:
            errors.add(k, _("{label}: deve ser maior que zero.", label=_(label)))
    if not errors and not all(data[k] for k in ("height", "width", "length")):
        errors.add("height", _("Informe altura, largura e comprimento da caixa."))
    return errors, data


@router.get("")
def list_models(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1, db: Session = Depends(get_db)):
    stmt = text_search(select(PackagingModel), q, PackagingModel.code, PackagingModel.name)
    stmt, srt = apply_sort(stmt, sort, dir, {"code": PackagingModel.code, "name": PackagingModel.name},
                           default="code", tiebreak=PackagingModel.id)
    return render(request, "packaging_models/list.html", page=paginate(db, stmt, page), q=q, sort=srt, usage=_usage(db))


@router.get("/new")
def new_model(request: Request):
    return render(request, "packaging_models/form.html", item=None, data={}, errors={}, fields=FIELDS)


@router.post("/new")
def create_model(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "packaging_models/form.html", status_code=422, item=None, data=data, errors=errors, fields=FIELDS)
    m = PackagingModel(**data)
    db.add(m)
    db.commit()
    back = str(form.get("back") or "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/packaging-models",
                    _("Modelo de embalagem {code} criado.", code=m.code))


@router.get("/{mid}/edit")
def edit_model(mid: int, request: Request, db: Session = Depends(get_db)):
    m = db.get(PackagingModel, mid)
    if not m:
        return redirect("/packaging-models", _("Modelo de embalagem não encontrado."), "danger")
    data = {k: getattr(m, k) for k in ("code", "name", "notes", "height", "width", "length", "tare")}
    return render(request, "packaging_models/form.html", item=m, data=data, errors={}, fields=FIELDS)


@router.post("/{mid}/edit")
def update_model(mid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    m = db.get(PackagingModel, mid)
    if not m:
        return redirect("/packaging-models", _("Modelo de embalagem não encontrado."), "danger")
    errors, data = _validate(db, form, mid)
    if errors:
        return render(request, "packaging_models/form.html", status_code=422, item=m, data=data, errors=errors, fields=FIELDS)
    for k, v in data.items():
        setattr(m, k, v)
    db.commit()
    return redirect("/packaging-models", _("Modelo de embalagem {code} atualizado.", code=m.code))


@router.post("/{mid}/delete")
def delete_model(mid: int, db: Session = Depends(get_db)):
    m = db.get(PackagingModel, mid)
    if not m:
        return redirect("/packaging-models", _("Modelo de embalagem não encontrado."), "danger")
    n = _usage(db).get(m.id, 0)
    if n:
        return redirect("/packaging-models", _("O modelo {code} é usado por {n} produto(s): troque a embalagem deles antes de excluir.", code=m.code, n=n), "danger")
    code = m.code
    db.delete(m)
    db.commit()
    return redirect("/packaging-models", _("Modelo de embalagem {code} excluído.", code=code))
