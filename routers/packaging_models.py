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
    # hierarquia (independe dos produtos): a lista do que o modelo pode conter, na ordem de preferência
    rows = []
    for i in form.getlist("cont_idx"):
        iid = to_int(form.get(f"cont_model_{i}"))
        if not iid or form.get(f"cont_del_{i}"):
            continue
        raw = f_str(form, f"cont_qty_{i}", 12)
        q = to_int(raw)
        row = {"idx": i, "inner_model_id": iid, "qty": q if q and q > 0 else None}
        im = db.get(PackagingModel, iid)
        if im is None or iid == mid:
            errors.add("contents", _("Modelo de dentro inválido."))
        elif mid and _contains(db, iid, mid):
            errors.add("contents", _("Hierarquia circular: o modelo {code} já contém este.", code=im.code))
        elif any(r["inner_model_id"] == iid for r in rows):
            errors.add("contents", _("O modelo {code} está repetido na lista.", code=im.code))
        elif raw and not row["qty"]:
            errors.add("contents", _("{label}: número inteiro maior que zero.", label=_("Quantidade")))
        rows.append(row)
    data["contents"] = rows
    if not errors:  # quantidade vazia = quantas cabem; avisa o que não cabe
        dims = (float(data["height"]), float(data["width"]), float(data["length"]))
        for r in rows:
            im = db.get(PackagingModel, r["inner_model_id"])
            fit = _fit(im, dims)
            if not fit:
                errors.add("contents", _("O modelo {code} não cabe neste pelas medidas.", code=im.code))
            elif not r["qty"]:
                r["qty"] = fit
            elif r["qty"] > fit:
                errors.add("contents", _("{qty} × {code} não cabem neste pelas medidas: cabem até {fit}.",
                                         qty=r["qty"], code=im.code, fit=fit))
    return errors, data


def _fit(im, dims: tuple) -> int:
    from services.packaging import fit_in_case

    if im is None or not all(v is not None for v in (im.height, im.width, im.length)) or not all(dims):
        return 0
    return fit_in_case((float(im.height), float(im.width), float(im.length)), None, dims)


def _contains(db: Session, mid: int, target: int, _depth: int = 0) -> bool:
    """O modelo `mid` contém (direta ou indiretamente) o `target`?"""
    if mid == target:
        return True
    m = db.get(PackagingModel, mid)
    if m is None or _depth > 10:
        return False
    return any(_contains(db, c.inner_model_id, target, _depth + 1) for c in m.contents)


def _save(db: Session, m: PackagingModel, data: dict) -> None:
    from models import PackagingModelContent

    for k in ("code", "name", "notes", "height", "width", "length", "tare"):
        setattr(m, k, data[k])
    m.contents[:] = [PackagingModelContent(inner_model_id=r["inner_model_id"], qty=r["qty"], sort=n)
                     for n, r in enumerate(data["contents"], start=1)]


@router.post("/calc")
def calc_model(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Botão Calcular: quantas embalagens de cada modelo da lista cabem neste (sem salvar) e o que não cabe."""
    from services.file_reader import parse_decimal

    def num(k):
        try:
            v = parse_decimal(str(form.get(k) or ""))
            return float(v) if v is not None and v > 0 else None
        except ValueError:
            return None
    dims = (num("height"), num("width"), num("length"))
    out = {"rows": {}, "info": [], "warnings": []}
    if not all(dims):
        out["warnings"].append(_("Informe altura, largura e comprimento da caixa."))
        return out
    for i in form.getlist("cont_idx"):
        im = db.get(PackagingModel, to_int(form.get(f"cont_model_{i}")) or 0)
        if im is None or form.get(f"cont_del_{i}"):
            continue
        fit = _fit(im, dims)
        q = to_int(form.get(f"cont_qty_{i}"))
        out["rows"][i] = fit
        if not fit:
            out["warnings"].append(_("O modelo {code} não cabe neste pelas medidas.", code=im.code))
        elif q and q > fit:
            out["warnings"].append(_("{qty} × {code} não cabem neste pelas medidas: cabem até {fit}.", qty=q,
                                     code=im.code, fit=fit))
        else:
            out["info"].append(_("Cabem até {fit} × {code}.", fit=fit, code=im.code))
    return out


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
    m = PackagingModel(code=data["code"], name=data["name"])
    _save(db, m, data)
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
    data["contents"] = [{"idx": f"e{n}", "inner_model_id": c.inner_model_id, "qty": c.qty}
                        for n, c in enumerate(m.contents)]
    return _form(request, db, m, data, {})


@router.post("/{mid}/edit")
def update_model(mid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    m = db.get(PackagingModel, mid)
    if not m:
        return redirect("/packaging-models", _("Modelo de embalagem não encontrado."), "danger")
    errors, data = _validate(db, form, mid)
    if errors:
        return _form(request, db, m, data, errors, 422)
    _save(db, m, data)
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
