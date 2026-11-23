"""Cadastro dos modelos de embalagem (caixa padrão usada por vários produtos: medidas e tara)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import PackagingModel, ProductPackaging
from routers.common import (FormErrors, apply_sort, f_dec, f_str, form_data, paginate, redirect, render, text_search,
                            to_int)
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
    # hierarquia: este modelo contém N de outro (independe dos produtos)
    inner = to_int(form.get("inner_model_id"))
    data["inner_model_id"] = inner if inner and inner != mid and db.get(PackagingModel, inner) else None
    if data["inner_model_id"] and mid and _contains(db, data["inner_model_id"], mid):
        errors.add("inner_model_id", _("Hierarquia circular: o modelo escolhido já contém este."))
    raw = f_str(form, "inner_qty", 12)
    data["inner_qty"] = to_int(raw) if data["inner_model_id"] else None
    if data["inner_model_id"] and raw and (not data["inner_qty"] or data["inner_qty"] <= 0):
        errors.add("inner_qty", _("{label}: número inteiro maior que zero.", label=_("Quantidade")))
    if data["inner_model_id"] and not data["inner_qty"] and not errors:
        from services.packaging import fit_in_case

        im = db.get(PackagingModel, data["inner_model_id"])
        n = fit_in_case((float(im.height), float(im.width), float(im.length)), None,
                        (float(data["height"]), float(data["width"]), float(data["length"])))
        data["inner_qty"] = n or None  # vazio: quantas cabem pelas medidas
        if not n:
            errors.add("inner_qty", _("O modelo {code} não cabe neste pelas medidas.", code=im.code))
    return errors, data


def _contains(db: Session, mid: int, target: int) -> bool:
    """O modelo `mid` contém (direta ou indiretamente) o `target`?"""
    seen, cur = set(), db.get(PackagingModel, mid)
    while cur is not None and cur.id not in seen:
        if cur.id == target:
            return True
        seen.add(cur.id)
        cur = cur.inner_model
    return False


@router.get("")
def list_models(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1, db: Session = Depends(get_db)):
    stmt = text_search(select(PackagingModel), q, PackagingModel.code, PackagingModel.name)
    stmt, srt = apply_sort(stmt, sort, dir, {"code": PackagingModel.code, "name": PackagingModel.name},
                           default="code", tiebreak=PackagingModel.id)
    return render(request, "packaging_models/list.html", page=paginate(db, stmt, page), q=q, sort=srt, usage=_usage(db))


def _form(request: Request, db: Session, m, data: dict, errors, status_code: int = 200):
    others = [x for x in db.scalars(select(PackagingModel).order_by(PackagingModel.code))
              if m is None or (x.id != m.id and not _contains(db, x.id, m.id))]
    return render(request, "packaging_models/form.html", status_code=status_code, item=m, data=data, errors=errors,
                  fields=FIELDS, others=others)


@router.get("/new")
def new_model(request: Request, db: Session = Depends(get_db)):
    return _form(request, db, None, {}, {})


@router.post("/new")
def create_model(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return _form(request, db, None, data, errors, 422)
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
    data = {k: getattr(m, k) for k in ("code", "name", "notes", "height", "width", "length", "tare", "inner_model_id",
                                       "inner_qty")}
    return _form(request, db, m, data, {})


@router.post("/{mid}/edit")
def update_model(mid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    m = db.get(PackagingModel, mid)
    if not m:
        return redirect("/packaging-models", _("Modelo de embalagem não encontrado."), "danger")
    errors, data = _validate(db, form, mid)
    if errors:
        return _form(request, db, m, data, errors, 422)
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
