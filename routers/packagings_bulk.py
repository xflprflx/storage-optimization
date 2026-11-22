"""Embalagens dos produtos em lote: a lista de todas as embalagens (SKU × modelo) e o aplicar em massa — um modelo de
embalagem em vários SKUs (conteúdo = unidades) ou um nível acima de várias embalagens (conteúdo = a embalagem
marcada: caixa → master)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import FormData

from database import get_db
from models import BinType, Branch, PackagingModel, Product, ProductPackaging
from routers.common import FormErrors, f_dec, f_int, f_str, form_data, paginate, redirect, render, to_int
from services.i18n import N_, _

router = APIRouter(prefix="/packagings", tags=[N_("Embalagens dos produtos")])


def _ids(form: FormData, key: str) -> list[int]:
    out = []
    for v in form.getlist(key):
        out += [i for i in (to_int(x) for x in str(v).split(",")) if i]
    return list(dict.fromkeys(out))


def _list_stmt(q: str, model: int | None, branch: str):
    stmt = select(ProductPackaging).join(Product, Product.id == ProductPackaging.product_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.sku.ilike(like), Product.name.ilike(like)))
    if model:
        stmt = stmt.where(ProductPackaging.model_id == model)
    if branch == "none":
        stmt = stmt.where(~ProductPackaging.branches.any())
    elif str(branch).isdigit():
        stmt = stmt.where(ProductPackaging.branches.any(Branch.id == int(branch)))
    return stmt.order_by(Product.sku, ProductPackaging.sort, ProductPackaging.id)


@router.get("")
def list_packagings(request: Request, q: str = "", model: str = "", branch: str = "", page: int = 1,
                    db: Session = Depends(get_db)):
    """Todas as embalagens dos produtos: marcar e aplicar um nível acima, ou remover."""
    mid = to_int(model)
    stmt = _list_stmt(q, mid, branch).options(
        selectinload(ProductPackaging.product), selectinload(ProductPackaging.model),
        selectinload(ProductPackaging.inner).selectinload(ProductPackaging.model),
        selectinload(ProductPackaging.branches), selectinload(ProductPackaging.pallets))
    return render(request, "packagings/list.html", page=paginate(db, stmt, page), q=q, model=mid, branch=branch,
                  models=list(db.scalars(select(PackagingModel).order_by(PackagingModel.code))),
                  branches=list(db.scalars(select(Branch).order_by(Branch.code))))


def _targets(db: Session, form: FormData):
    """(modo, alvos, ids, descrição): modo "skus" (produtos marcados ou filtrados) ou "level" (embalagens marcadas
    ou filtradas: o nível acima)."""
    mode = "level" if f_str(form, "mode") == "level" else "skus"
    if mode == "level":
        if f_str(form, "scope") == "filtered":
            ids = list(db.scalars(_list_stmt(f_str(form, "f_q"), to_int(form.get("f_model")), f_str(form, "f_branch"))
                                  .with_only_columns(ProductPackaging.id)))
        else:
            ids = _ids(form, "ids")
        ks = []
        for i in range(0, len(ids), 900):
            ks += list(db.scalars(select(ProductPackaging).where(ProductPackaging.id.in_(ids[i:i + 900]))))
        return mode, [(k.product, k) for k in ks], ids
    if f_str(form, "scope") == "filtered":
        from routers.products import _filtered

        ids = list(db.scalars(_filtered(f_str(form, "f_q"), f_str(form, "f_material"), f_str(form, "f_field"),
                                        f_str(form, "f_mode") or "contains")))
    else:
        ids = _ids(form, "ids")
    prods = []
    for i in range(0, len(ids), 900):
        prods += list(db.scalars(select(Product).where(Product.id.in_(ids[i:i + 900]))))
    return mode, [(p, None) for p in prods], ids


def _form(request: Request, db: Session, mode: str, targets: list, ids: list, data: dict, errors, back: str,
          status_code: int = 200):
    from collections import Counter

    inner_models = Counter(k.model.code for _p, k in targets if k is not None and k.model is not None)
    return render(request, "packagings/apply.html", status_code=status_code, mode=mode, n=len(targets),
                  ids=",".join(str(i) for i in ids), data=data, errors=errors, back=back,
                  sample=[p.sku for p, _k in targets[:12]], inner_models=inner_models.most_common(),
                  models=list(db.scalars(select(PackagingModel).order_by(PackagingModel.code))),
                  branches=list(db.scalars(select(Branch).order_by(Branch.code))),
                  pallet_types=list(db.scalars(select(BinType).where(BinType.store_as == "pallet").order_by(BinType.code))))


@router.post("/apply")
def apply_form(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """1ª etapa: os alvos (SKUs ou embalagens marcadas) e o formulário do que aplicar."""
    back = f_str(form, "back") or "/packagings"
    if not back.startswith(("/products", "/packagings")):
        back = "/packagings"
    mode, targets, ids = _targets(db, form)
    if not targets:
        return redirect(back, _("Nenhum produto ou embalagem selecionado."), "warning")
    data = {"branch_ids": [b.id for b in db.scalars(select(Branch).where(Branch.unitization != "off"))],
            "on_existing": "update", "pallets": []}
    return _form(request, db, mode, targets, [k.id if k is not None else p.id for p, k in targets], data, {}, back)


@router.post("/apply/confirm")
def apply_confirm(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """2ª etapa: aplica o modelo de embalagem em todos os alvos."""
    from services.packaging import apply_bulk

    back = f_str(form, "back") or "/packagings"
    if not back.startswith(("/products", "/packagings")):
        back = "/packagings"
    mode = "level" if f_str(form, "mode") == "level" else "skus"
    ids = _ids(form, "ids")
    if mode == "level":
        targets = [(k.product, k) for k in db.scalars(select(ProductPackaging).where(ProductPackaging.id.in_(ids)))]
    else:
        targets = [(p, None) for p in db.scalars(select(Product).where(Product.id.in_(ids)))]
    errors = FormErrors()
    model = db.get(PackagingModel, f_int(form, "model_id") or 0)
    if model is None:
        errors.add("model_id", _("Escolha o modelo de embalagem (Cadastros → Modelos de embalagem)."))
    raw = f_str(form, "inner_qty", 12)
    qty = to_int(raw)
    if raw and (qty is None or qty <= 0):
        errors.add("inner_qty", _("{label}: número inteiro maior que zero.", label=_("Quantidade")))
    weight = f_dec(form, "weight", errors, _("Peso cheia"))
    valid = {b.id: b for b in db.scalars(select(Branch))}
    branches = [valid[i] for i in _ids(form, "branch_ids") if i in valid]
    pallets = {b.id: b for b in db.scalars(select(BinType).where(BinType.store_as == "pallet"))}
    rows = []
    for i in form.getlist("pal_idx"):
        tid = f_int(form, f"pal_type_{i}")
        if not tid or form.get(f"pal_del_{i}") or tid not in pallets:
            continue
        a, b = to_int(form.get(f"pal_layer_{i}")), to_int(form.get(f"pal_layers_{i}"))
        rows.append((pallets[tid], a if a and a > 0 else None, b if b and b > 0 else None))
    on_existing = "skip" if f_str(form, "on_existing") == "skip" else "update"
    data = {"model_id": model.id if model else None, "inner_qty": qty, "weight": weight,
            "branch_ids": [b.id for b in branches], "on_existing": on_existing,
            "pallets": [{"idx": f"e{n}", "pallet_type_id": p.id, "layer": a, "layers": b} for n, (p, a, b) in enumerate(rows)]}
    if errors or not targets:
        return _form(request, db, mode, targets, ids, data, errors, back, 422)
    res = apply_bulk(db, targets, model, qty, weight, branches, rows, on_existing)
    db.commit()
    msg = _("Embalagem {code}: {c} criada(s), {u} atualizada(s), {s} pulada(s); {n} valor(es) calculado(s) pelas medidas.",
            code=model.code, c=res["created"], u=res["updated"], s=res["skipped"], n=res["calculated"])
    warns = res["warnings"]
    if warns:
        msg += " " + _("Avisos:") + " " + " · ".join(warns[:5]) + (" …" if len(warns) > 5 else "")
    return redirect(f"/packagings?model={model.id}", msg, "warning" if warns else "success")


@router.post("/delete")
def delete_selected(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Remove as embalagens marcadas (as que são usadas dentro de outra ficam)."""
    ids = _ids(form, "ids")
    kept = removed = 0
    for k in list(db.scalars(select(ProductPackaging).where(ProductPackaging.id.in_(ids)))):
        if any(x.inner_id == k.id for x in k.product.packagings if x.id not in ids):
            kept += 1
            continue
        for x in k.product.packagings:  # quem a usa dentro também está saindo: solta a referência
            if x.inner_id == k.id:
                x.inner = None
        k.product.packagings.remove(k)
        removed += 1
    db.commit()
    back = f_str(form, "back") or "/packagings"
    return redirect(back if back.startswith("/packagings") else "/packagings",
                    _("{n} embalagem(ns) removida(s).", n=removed)
                    + (" " + _("{n} mantida(s): usadas dentro de outra embalagem.", n=kept) if kept else ""),
                    "warning" if kept else "success")
