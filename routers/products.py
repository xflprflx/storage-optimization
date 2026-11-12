"""CRUD de Produtos (com dimensões físicas embutidas)."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

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
from services.i18n import N_, _, seps

router = APIRouter(prefix="/products", tags=[N_("Produtos")])

DIM_FIELDS = (("height", N_("Altura")), ("width", N_("Largura")), ("length", N_("Comprimento")), ("weight", N_("Peso")))


def _validate(db: Session, form: FormData, product_id: int | None):
    """Cadastro do produto (compartilhado por todas as filiais) + políticas de estoque por filial (linhas pol_*)."""
    errors = FormErrors()
    data = {
        "sku": normalize_code(f_str(form, "sku")),
        "name": f_str(form, "name", 255),
        "material_type": MaterialResolver(db)(f_str(form, "material_type")),
    }
    if not data["sku"]:
        errors.add("sku", _("SKU é obrigatório."))
    elif len(data["sku"]) > 60:
        errors.add("sku", _("SKU deve ter no máximo 60 caracteres."))
    else:
        stmt = select(Product.id).where(Product.sku == data["sku"])
        if product_id:
            stmt = stmt.where(Product.id != product_id)
        if db.scalar(stmt):
            errors.add("sku", _("SKU {sku} já cadastrado.", sku=data['sku']))
    if not data["name"]:
        errors.add("name", _("Nome é obrigatório."))
    for key, label in DIM_FIELDS:
        data[key] = f_dec(form, key, errors, label)
    data["policies"] = _policy_rows(db, form, errors)
    return errors, data


def _policy_rows(db: Session, form: FormData, errors: FormErrors) -> list[dict]:
    """Linhas da tabela "Política de estoque por filial": filial, endereço padrão, min e max level."""
    from models import Branch

    rows, seen = [], set()
    for i in form.getlist("pol_idx"):
        bid = f_int(form, f"pol_branch_{i}")
        row = {"idx": i, "branch_id": bid, "location_id": f_int(form, f"pol_loc_{i}"),
               "location_label": f_str(form, f"pol_loc_label_{i}"), "delete": bool(form.get(f"pol_del_{i}"))}
        row["min_level"] = f_dec(form, f"pol_min_{i}", errors, _("Nível mínimo"))
        row["max_level"] = f_dec(form, f"pol_max_{i}", errors, _("Nível máximo"))
        empty = not row["location_id"] and row["min_level"] is None and row["max_level"] is None
        if not bid:
            if not empty:
                errors.add("policies", _("Escolha a filial de cada política de estoque."))
            continue
        br = db.get(Branch, bid)
        if br is None:
            errors.add("policies", _("Filial inválida."))
            continue
        if bid in seen:
            errors.add("policies", _("Filial {code} aparece mais de uma vez: uma política por filial.", code=br.code))
            continue
        seen.add(bid)
        if row["location_label"] and not row["location_id"]:
            errors.add("policies", _("{code}: escolha o endereço padrão na lista (ou deixe vazio).", code=br.code))
        if row["location_id"]:
            loc = db.get(StorageLocation, row["location_id"])
            if loc is None or loc.branch_id != bid:
                errors.add("policies", _("{code}: o endereço padrão precisa ser da própria filial.", code=br.code))
        if row["min_level"] is not None and row["max_level"] is not None and row["min_level"] > row["max_level"]:
            errors.add("policies", _("{code}: nível máximo deve ser maior ou igual ao mínimo.", code=br.code))
        row["empty"] = empty
        rows.append(row)
    return rows


def _save_policies(db: Session, prod: Product, rows: list[dict]) -> set:
    """Grava as políticas (linha vazia ou marcada para remover = remove a da filial). Retorna os endereços
    padrão antigos e novos (para recalcular a ocupação)."""
    from models import StockPolicy

    current = {p.branch_id: p for p in db.scalars(select(StockPolicy).where(StockPolicy.product_id == prod.id))}
    touched = {p.default_location_id for p in current.values()}
    keep = set()
    for r in rows:
        pol = current.get(r["branch_id"])
        if r["delete"] or r["empty"]:
            continue
        if pol is None:
            pol = StockPolicy(branch_id=r["branch_id"], product_id=prod.id)
            db.add(pol)
        pol.default_location_id = r["location_id"] or None
        pol.min_level, pol.max_level = r["min_level"], r["max_level"]
        touched.add(pol.default_location_id)
        keep.add(r["branch_id"])
    for bid, pol in current.items():
        if bid not in keep:
            db.delete(pol)
    db.flush()
    return touched


def _policy_form_rows(db: Session, prod: Optional[Product]) -> list[dict]:
    """Políticas atuais do produto para o formulário (+ uma linha vazia para outra filial)."""
    from models import StockPolicy

    rows = []
    if prod is not None:
        for i, pol in enumerate(db.scalars(select(StockPolicy).where(StockPolicy.product_id == prod.id)
                                           .options(joinedload(StockPolicy.default_location),
                                                    joinedload(StockPolicy.branch)))):
            rows.append({"idx": str(i), "branch_id": pol.branch_id, "location_id": pol.default_location_id,
                         "location_label": pol.default_location.full_label if pol.default_location else "",
                         "min_level": pol.min_level, "max_level": pol.max_level})
    rows.sort(key=lambda r: r["branch_id"])
    return rows


SEARCH_FIELDS = {"": N_("Tudo"), "sku": "SKU", "name": N_("Nome"), "material": N_("Tipo de material"), "address": N_("Endereço")}


def _search_fields(default_addresses, stock_addresses) -> dict:
    return {"sku": ((Product.sku,), ()), "name": ((Product.name,), ()), "material": ((Product.material_type,), ()),
            "address": ((default_addresses, stock_addresses), (default_addresses, stock_addresses))}


def _default_addresses(first: bool = False):
    """Endereços padrão do produto nas filiais (políticas de estoque), para busca (todos) ou ordenação (o 1º)."""
    from models import StockPolicy

    DL = aliased(StorageLocation)
    col = func.min(DL.address) if first else func.group_concat(DL.address, " ")
    return (select(col).select_from(StockPolicy).join(DL, DL.id == StockPolicy.default_location_id)
            .where(StockPolicy.product_id == Product.id).correlate(Product).scalar_subquery())


def _filtered(q: str, material: str, field: str = "", mode: str = "contains"):
    """Produtos que a busca da lista mostra (mesma regra da tela)."""
    DefLoc = aliased(StorageLocation)
    SL = aliased(StorageLocation)
    stock_addresses = (select(func.group_concat(SL.address, " ")).select_from(StockItem)
                       .join(SL, SL.id == StockItem.location_id)
                       .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    default_addresses = _default_addresses()
    stmt = select(Product.id)
    stmt = text_search(stmt, q, Product.sku, Product.name, Product.material_type, default_addresses, stock_addresses,
                       address_columns=(default_addresses, stock_addresses), mode=mode, only=field or None,
                       fields=_search_fields(default_addresses, stock_addresses))
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
            return redirect(back, _("Produto modelo {model_sku} não encontrado.", model_sku=model_sku), "danger")
        for k, _l in DIM_FIELDS:
            if vals[k] is None:
                vals[k] = getattr(src, k)
    if errors:
        return redirect(back, " ".join(m for ms in errors.values() for m in (ms if isinstance(ms, list) else [ms])), "danger")
    for k, label in DIM_FIELDS:
        if vals[k] is not None and vals[k] == 0:
            return redirect(back, _("{label}: informe um valor maior que zero (vazio = não muda).", label=label), "danger")
    vals = {k: v for k, v in vals.items() if v is not None}
    if not vals:
        return redirect(back, _("Informe ao menos uma medida (altura, largura, comprimento ou peso) ou um produto modelo."), "warning")
    if f_str(form, "scope") == "filtered":
        ids = list(db.scalars(_filtered(f_str(form, "f_q"), f_str(form, "f_material"), f_str(form, "f_field"),
                                        f_str(form, "f_mode") or "contains")))
    else:
        ids = [i for i in (to_int(v) for v in form.getlist("ids")) if i]
    if not ids:
        return redirect(back, _("Nenhum produto selecionado."), "warning")
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
    what = ", ".join(f"{labels[k].lower()} {float(v):g}{unit.get(k, ' cm')}".replace(".", seps()[0]) for k, v in vals.items())
    return redirect(back, f"{n} produto(s) atualizado(s) ({what})." + (_(" {v} já tinham essas medidas.", v=len(ids) - n) if len(ids) > n else ""))


def filtered_products(q: str = "", material: str = "", field: str = "", mode: str = "contains"):
    """select(Product) com a busca e o filtro de material da lista (também usado na exclusão de "todos do filtro")."""
    field = field if field in SEARCH_FIELDS else ""
    mode = mode if mode in SEARCH_MODES else "contains"
    default_addresses = _default_addresses()
    # Endereços onde o produto está (saldo) — usados na busca
    SL = aliased(StorageLocation)
    stock_addresses = (select(func.group_concat(SL.address, " ")).select_from(StockItem)
                       .join(SL, SL.id == StockItem.location_id)
                       .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    stmt = text_search(select(Product), q, Product.sku, Product.name, Product.material_type, default_addresses,
                       stock_addresses, address_columns=(default_addresses, stock_addresses), mode=mode, only=field or None,
                       fields=_search_fields(default_addresses, stock_addresses))
    cond = material_condition(material)
    return stmt.where(cond) if cond is not None else stmt


@router.get("")
def list_products(request: Request, q: str = "", material: str = "", sort: str = "", dir: str = "", page: int = 1,
                  field: str = "", mode: str = "contains", db: Session = Depends(get_db)):
    field = field if field in SEARCH_FIELDS else ""
    mode = mode if mode in SEARCH_MODES else "contains"
    total_stock = (select(func.coalesce(func.sum(StockItem.quantity), Decimal("0")))
                   .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    first_default = _default_addresses(first=True)
    SL = aliased(StorageLocation)
    first_stock_address = (select(func.min(SL.address)).select_from(StockItem)
                           .join(SL, SL.id == StockItem.location_id)
                           .where(StockItem.product_id == Product.id).correlate(Product).scalar_subquery())
    stmt = filtered_products(q, material, field, mode)
    stmt, srt = apply_sort(stmt, sort, dir, {
        "sku": Product.sku, "name": Product.name, "material": (func.nullif(Product.material_type, ""), Product.sku),
        "location": (func.coalesce(first_default, first_stock_address), Product.sku),
        "height": Product.height, "width": Product.width, "length": Product.length, "weight": Product.weight,
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
                  places=product_places(db, pg.items), policies=_policies_summary(db, ids))


def _policies_summary(db: Session, ids: list[int]) -> dict[int, list[dict]]:
    """Políticas de estoque (filial, min, max) dos produtos da página."""
    from models import Branch, StockPolicy

    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    for pid, code, mn, mx in db.execute(select(StockPolicy.product_id, Branch.code, StockPolicy.min_level,
                                               StockPolicy.max_level)
                                        .join(Branch, Branch.id == StockPolicy.branch_id)
                                        .where(StockPolicy.product_id.in_(ids)).order_by(Branch.code)):
        out.setdefault(pid, []).append({"branch": code, "min": mn, "max": mx})
    return out


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
    from models import StockPolicy

    for pol in db.scalars(select(StockPolicy).where(StockPolicy.product_id.in_(ids),
                                                    StockPolicy.default_location_id.is_not(None))
                          .options(joinedload(StockPolicy.default_location).joinedload(StorageLocation.branch),
                                   joinedload(StockPolicy.default_location).joinedload(StorageLocation.warehouse))):
        loc = pol.default_location  # endereço padrão na filial (um por filial)
        hit = next((x for x in out[pol.product_id] if x["id"] == loc.id), None)
        if hit:
            hit["default"] = True
            out[pol.product_id].remove(hit)
            out[pol.product_id].insert(0, hit)
        else:
            out[pol.product_id].insert(0, {"id": loc.id, "address": loc.address, "scope": loc.scope_label, "qty": None,
                                           "default": True})
    return out


def _form(request: Request, db: Session, product, data, errors, status_code: int = 200):
    from models import Branch

    return render(request, "products/form.html", status_code=status_code, product=product, data=data, errors=errors,
                  dim_fields=DIM_FIELDS, branches=list(db.scalars(select(Branch).order_by(Branch.code))))


@router.get("/new")
def new_product(request: Request, db: Session = Depends(get_db)):
    return _form(request, db, None, {"policies": []}, {})


@router.post("/new")
def create_product(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return _form(request, db, None, data, errors, 422)
    rows = data.pop("policies")
    prod = Product(**data)
    db.add(prod)
    db.flush()
    refresh_occupancy(db, _save_policies(db, prod, rows))
    sync_default_locations(db, [prod.id])
    db.commit()
    return redirect(f"/products/{prod.id}", _("Produto {sku} criado.", sku=prod.sku))


@router.get("/{product_id}")
def product_detail(product_id: int, request: Request, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    stock = list(db.scalars(
        select(StockItem).join(StorageLocation)
        .options(joinedload(StockItem.location).joinedload(StorageLocation.branch),
                 joinedload(StockItem.location).joinedload(StorageLocation.warehouse))
        .where(StockItem.product_id == product_id)
        .order_by(StorageLocation.branch_id, StorageLocation.warehouse_id, StorageLocation.address)
    ))
    total = sum((s.quantity for s in stock), Decimal("0"))
    from models import StockPolicy

    policies = list(db.scalars(select(StockPolicy).where(StockPolicy.product_id == product_id)
                               .options(joinedload(StockPolicy.branch),
                                        joinedload(StockPolicy.default_location).joinedload(StorageLocation.warehouse))))
    policies.sort(key=lambda p: p.branch.code)
    return render(request, "products/detail.html", product=prod, stock=stock, total=total, policies=policies)


@router.get("/{product_id}/edit")
def edit_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    data = {k: getattr(prod, k) for k in ("sku", "name", "material_type", "height", "width", "length", "weight")}
    data["policies"] = _policy_form_rows(db, prod)
    return _form(request, db, prod, data, {})


@router.post("/{product_id}/edit")
def update_product(product_id: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    errors, data = _validate(db, form, product_id)
    if errors:
        return _form(request, db, prod, data, errors, 422)
    rows = data.pop("policies")
    for k, v in data.items():
        setattr(prod, k, v)
    db.flush()
    refresh_occupancy(db, _save_policies(db, prod, rows))
    if sync_default_locations(db, [prod.id]):
        db.commit()
        return redirect(f"/products/{prod.id}", _("Produto {sku} atualizado. Em filial(is) onde ele tem saldo, o endereço padrão é o endereço onde o produto está (o de maior saldo, se o escolhido não tinha saldo).", sku=prod.sku), "warning")
    db.commit()
    return redirect(f"/products/{prod.id}", _("Produto {sku} atualizado.", sku=prod.sku))


@router.post("/{product_id}/delete")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    prod = db.get(Product, product_id)
    if not prod:
        return redirect("/products", _("Produto não encontrado."), "danger")
    from models import StockPolicy

    location_ids = list(db.scalars(select(StockItem.location_id).where(StockItem.product_id == product_id)))
    location_ids += list(db.scalars(select(StockPolicy.default_location_id).where(StockPolicy.product_id == product_id)))
    sku = prod.sku
    db.delete(prod)  # cascata: StockItems
    db.flush()
    refresh_occupancy(db, location_ids)
    db.commit()
    return redirect("/products", _("Produto {sku} excluído (saldos removidos).", sku=sku))
