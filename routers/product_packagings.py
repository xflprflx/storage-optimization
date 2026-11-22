"""Embalagens do produto: a lista de modelos de embalagem do produto (quantidade em cada um, hierarquia opcional),
as paletizações de cada uma e as filiais em que cada uma está habilitada."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import BinType, Branch, PackagingModel, PackagingPallet, Product, ProductPackaging
from routers.common import FormErrors, f_dec, f_int, f_str, form_data, redirect, render
from services.i18n import N_, _

router = APIRouter(prefix="/products/{product_id}/packagings", tags=[N_("Embalagens do produto")])



def _descendants(prod: Product, kid: int) -> set[int]:
    """A embalagem e as que são feitas dela (direta ou indiretamente): não podem ser a de dentro dela."""
    out, grew = {kid}, True
    while grew:
        grew = False
        for k in prod.packagings:
            if k.inner_id in out and k.id not in out:
                out.add(k.id)
                grew = True
    return out


def _pallet_types(db: Session) -> list[BinType]:
    return list(db.scalars(select(BinType).where(BinType.store_as == "pallet").order_by(BinType.code)))


def _validate(db: Session, prod: Product, form: FormData, kid: int | None):
    errors = FormErrors()
    data = {}
    mid = f_int(form, "model_id")
    data["model_id"] = mid if mid and db.get(PackagingModel, mid) else None
    if not data["model_id"]:
        errors.add("model_id", _("Escolha o modelo de embalagem (Cadastros → Modelos de embalagem)."))
    elif any(k.model_id == data["model_id"] and k.id != kid for k in prod.packagings):
        errors.add("model_id", _("O produto já tem esse modelo de embalagem."))
    inner = f_int(form, "inner_id")
    valid_inner = {k.id for k in prod.packagings} - (_descendants(prod, kid) if kid else set())
    data["inner_id"] = inner if inner in valid_inner else None
    raw = f_str(form, "inner_qty", 12)
    data["inner_qty"] = None
    if raw:
        try:
            v = int(raw)
            if v <= 0:
                raise ValueError
            data["inner_qty"] = v
        except ValueError:
            errors.add("inner_qty", _("{label}: número inteiro maior que zero.", label=_("Quantidade")))
    data["weight"] = f_dec(form, "weight", errors, _("Peso cheia"))
    data["sort"] = f_int(form, "sort") or 0
    valid = {b.id for b in db.scalars(select(Branch))}
    data["branch_ids"] = sorted({int(v) for v in form.getlist("branch_ids") if str(v).isdigit() and int(v) in valid})
    pallets, types = [], {b.id for b in _pallet_types(db)}
    for i in form.getlist("pal_idx"):
        tid = f_int(form, f"pal_type_{i}")
        if not tid or form.get(f"pal_del_{i}"):
            continue
        if tid not in types:
            errors.add("pallets", _("Palete inválido."))
            continue
        row = {"idx": i, "id": f_int(form, f"pal_id_{i}"), "pallet_type_id": tid}
        for key, label in (("layer", _("Caixas por camada")), ("layers", _("Camadas"))):
            raw = f_str(form, f"pal_{key}_{i}", 12)
            row[key] = None
            if raw:
                try:
                    v = int(raw)
                    if v <= 0:
                        raise ValueError
                    row[key] = v
                except ValueError:
                    errors.add("pallets", _("{label}: número inteiro maior que zero.", label=label))
        pallets.append(row)
    data["pallets"] = pallets
    return errors, data


def _save(db: Session, prod: Product, k: ProductPackaging, data: dict) -> list[str]:
    from services.packaging import check, fill

    for key in ("inner_id", "inner_qty", "model_id", "weight", "sort"):
        setattr(k, key, data[key])
    k.inner = db.get(ProductPackaging, k.inner_id) if k.inner_id else None
    k.model = db.get(PackagingModel, k.model_id)
    k.name = k.model.code[:40]
    k.branches = list(db.scalars(select(Branch).where(Branch.id.in_(data["branch_ids"])))) if data["branch_ids"] else []
    keep = {r["id"] for r in data["pallets"] if r["id"]}
    for pp in list(k.pallets):
        if pp.id not in keep:
            k.pallets.remove(pp)
    by_id = {pp.id: pp for pp in k.pallets}
    for r in data["pallets"]:
        pp = by_id.get(r["id"]) if r["id"] else None
        if pp is None:
            pp = PackagingPallet()
            k.pallets.append(pp)
        pp.pallet_type_id, pp.layer, pp.layers = r["pallet_type_id"], r["layer"], r["layers"]
        pp.pallet_type = db.get(BinType, r["pallet_type_id"])
    notes = fill(k, prod)
    return notes + check(k, prod)


def _form(request: Request, db: Session, prod: Product, k, data: dict, errors, status_code: int = 200):
    inners = [x for x in prod.packagings if not k or x.id not in _descendants(prod, k.id)]
    return render(request, "product_packagings/form.html", status_code=status_code, product=prod, item=k, data=data,
                  errors=errors, inners=inners, pallet_types=_pallet_types(db),
                  branches=list(db.scalars(select(Branch).order_by(Branch.code))),
                  models=list(db.scalars(select(PackagingModel).order_by(PackagingModel.code))))


@router.get("")
def list_packagings(product_id: int, request: Request, db: Session = Depends(get_db)):
    from services.packaging import ALL, check, dims_of, ordered, weight_of

    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    pals = _pallet_types(db)
    rows = []
    for k in ordered(prod, ALL):
        rows.append({"k": k, "units": k.units(), "dims": dims_of(k), "weight": weight_of(k, prod),
                     "warnings": check(k, prod),
                     "missing": [b.code for b in pals if not any(pp.pallet_type_id == b.id for pp in k.pallets)]})
    return render(request, "product_packagings/list.html", product=prod, rows=rows, pallet_types=pals)


@router.get("/new")
def new_packaging(product_id: int, request: Request, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    # nova embalagem: já marcada nas filiais com a unitização ligada
    data = {"pallets": [], "sort": max((k.sort or 0 for k in prod.packagings), default=0) + 1,
            "branch_ids": [b.id for b in db.scalars(select(Branch).where(Branch.unitization != "off"))]}
    return _form(request, db, prod, None, data, {})


@router.post("/new")
def create_packaging(product_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    errors, data = _validate(db, prod, form, None)
    if errors:
        return _form(request, db, prod, None, data, errors, 422)
    k = ProductPackaging(product_id=prod.id, sort=len(prod.packagings))
    prod.packagings.append(k)
    notes = _save(db, prod, k, data)
    db.commit()
    return redirect(f"/products/{prod.id}/packagings", " ".join([_("Embalagem {name} criada.", name=k.name)] + notes),
                    "warning" if notes else "success")


@router.get("/{kid}/edit")
def edit_packaging(product_id: int, kid: int, request: Request, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    k = db.get(ProductPackaging, kid)
    if not prod or not k or k.product_id != prod.id:
        return redirect(f"/products/{product_id}/packagings", _("Embalagem não encontrada."), "danger")
    data = {key: getattr(k, key) for key in ("inner_id", "inner_qty", "model_id", "weight", "sort")}
    data["branch_ids"] = sorted(k.branch_ids)
    data["pallets"] = [{"idx": f"e{i}", "id": pp.id, "pallet_type_id": pp.pallet_type_id, "layer": pp.layer,
                        "layers": pp.layers} for i, pp in enumerate(k.pallets)]
    return _form(request, db, prod, k, data, {})


@router.post("/{kid}/edit")
def update_packaging(product_id: int, kid: int, request: Request, form: FormData = Depends(form_data),
                     db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    k = db.get(ProductPackaging, kid)
    if not prod or not k or k.product_id != prod.id:
        return redirect(f"/products/{product_id}/packagings", _("Embalagem não encontrada."), "danger")
    errors, data = _validate(db, prod, form, kid)
    if errors:
        return _form(request, db, prod, k, data, errors, 422)
    notes = _save(db, prod, k, data)
    db.commit()
    return redirect(f"/products/{prod.id}/packagings", " ".join([_("Embalagem {name} atualizada.", name=k.name)] + notes),
                    "warning" if notes else "success")


@router.post("/{kid}/delete")
def delete_packaging(product_id: int, kid: int, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    k = db.get(ProductPackaging, kid)
    if not prod or not k or k.product_id != prod.id:
        return redirect(f"/products/{product_id}/packagings", _("Embalagem não encontrada."), "danger")
    users = [x.name for x in prod.packagings if x.inner_id == k.id]
    if users:
        return redirect(f"/products/{prod.id}/packagings",
                        _("A embalagem {name} é usada dentro de {others}: altere-as antes de excluir.", name=k.name,
                          others=", ".join(users)), "danger")
    name = k.name
    prod.packagings.remove(k)
    db.commit()
    return redirect(f"/products/{prod.id}/packagings", _("Embalagem {name} excluída.", name=name))


@router.post("/calc")
def calc_packaging(product_id: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Botão Calcular: a quantidade de dentro e as paletizações pelas medidas (sem salvar) + avisos."""
    from types import SimpleNamespace

    from services.packaging import _K, check, fill

    prod = db.get(Product, product_id)
    if not prod:
        return {"info": [], "warnings": []}
    errs = FormErrors()
    num = lambda key: f_dec(form, key, errs, key)  # noqa: E731
    inner = db.get(ProductPackaging, f_int(form, "inner_id") or 0)
    model = db.get(PackagingModel, f_int(form, "model_id") or 0)
    k = _K(name=model.code if model else _("Embalagem"), inner=inner if inner and inner.product_id == prod.id else None,
           inner_qty=f_int(form, "inner_qty"), model=model, weight=num("weight"))
    rows = []
    for i in form.getlist("pal_idx"):
        tid = f_int(form, f"pal_type_{i}")
        if tid and not form.get(f"pal_del_{i}"):
            rows.append((i, SimpleNamespace(pallet_type_id=tid, pallet_type=db.get(BinType, tid),
                                            layer=f_int(form, f"pal_layer_{i}"), layers=f_int(form, f"pal_layers_{i}"))))
    k.pallets = [r for _i, r in rows]
    info = fill(k, prod)
    return {"inner_qty": k.inner_qty, "pallets": {i: [r.layer, r.layers] for i, r in rows}, "info": info,
            "warnings": check(k, prod), "units": k.units()}
