"""CRUD de Tipos de Bin (nome, dimensões e capacidade)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from services.level_service import fit_structure, structure_message
from models import BinType, LocationBin
from routers.common import (
    FormErrors,
    apply_sort,
    f_dec,
    f_str,
    form_data,
    paginate,
    redirect,
    render,
    text_search,
)
from services.addressing import normalize_code
from services.bin_type_service import derived_dims, parse_grid, refresh_children

router = APIRouter(prefix="/bin-types", tags=["Tipos de Bin"])

NUM_FIELDS = (
    ("height", "Altura"),
    ("width", "Largura"),
    ("length", "Comprimento"),
    ("max_weight", "Capacidade de peso"),
    ("max_volume", "Capacidade de volume"),
)


def _validate(db: Session, form: FormData, bin_type_id: int | None):
    errors = FormErrors()
    data = {"code": normalize_code(f_str(form, "code")), "name": f_str(form, "name", 150)}
    if not data["code"]:
        errors.add("code", "Código é obrigatório.")
    elif len(data["code"]) > 30:
        errors.add("code", "Código deve ter no máximo 30 caracteres.")
    else:
        stmt = select(BinType.id).where(BinType.code == data["code"])
        if bin_type_id:
            stmt = stmt.where(BinType.id != bin_type_id)
        if db.scalar(stmt):
            errors.add("code", f"Já existe um tipo de bin com o código {data['code']}.")
    if not data["name"]:
        errors.add("name", "Nome é obrigatório.")
    for key, label in NUM_FIELDS:
        val = f_dec(form, key, errors, label)
        if val is not None and val == 0:
            errors.add(key, f"{label}: deve ser maior que zero (ou deixe vazio).")
        data[key] = val
    # Fração de uma bin inteira (compartimento): grade colunas (largura) × fileiras (comprimento)
    data["div_w_txt"], data["div_l_txt"] = f_str(form, "div_w", 3), f_str(form, "div_l", 3)
    grid, msg = parse_grid(data["div_w_txt"], data["div_l_txt"], f_str(form, "fraction", 10), form.get("split_axis") or "width")
    if msg:
        errors.add("div_w", msg)
    pid = form.get("parent_id") or ""
    parent = db.get(BinType, int(pid)) if str(pid).isdigit() else None
    data.update(parent_id=parent.id if parent else None, split_axis="grid",
                div_w=grid[0] if grid else None, div_l=grid[1] if grid else None,
                fraction_num=1 if grid else None, fraction_den=grid[0] * grid[1] if grid else None)
    if grid and parent is None and not msg:
        errors.add("parent_id", "Informe a bin pai do compartimento.")
    if parent is not None and not grid and not msg:
        errors.add("div_w", "Informe a divisão (ex.: 2 na largura × 3 no comprimento).")
    if parent is not None:
        if bin_type_id and parent.id == bin_type_id:
            errors.add("parent_id", "A bin pai não pode ser o próprio tipo.")
        elif parent.is_fraction:
            errors.add("parent_id", "A bin pai precisa ser uma bin inteira (não um compartimento).")
        elif bin_type_id and db.scalar(select(BinType.id).where(BinType.parent_id == bin_type_id)):
            errors.add("parent_id", "Este tipo é bin pai de compartimentos; não pode ser compartimento.")
    data["dims_auto"] = False
    if parent is not None and grid and not errors.get("parent_id"):
        dims = derived_dims(parent, *grid)
        if all(data.get(k) is None for k in ("height", "width", "length")):
            data["dims_auto"] = True
            data.update({k: dims[k] for k in ("height", "width", "length")})
        for k in ("max_weight", "max_volume"):
            if data.get(k) is None and data["dims_auto"]:
                data[k] = dims[k]
    return errors, data


def _parents(db: Session, exclude: int | None = None) -> list[BinType]:
    stmt = select(BinType).where(BinType.parent_id.is_(None)).order_by(BinType.code)
    return [b for b in db.scalars(stmt) if b.id != exclude]


def _usage(db: Session, ids: list[int]) -> dict[int, tuple[int, int]]:
    """tipo -> (nº de endereços, nº total de bins)."""
    if not ids:
        return {}
    rows = db.execute(
        select(LocationBin.bin_type_id, func.count(LocationBin.location_id), func.sum(LocationBin.quantity))
        .where(LocationBin.bin_type_id.in_(ids)).group_by(LocationBin.bin_type_id)
    ).all()
    return {r[0]: (r[1], r[2] or 0) for r in rows}


@router.get("")
def list_bin_types(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1,
                   db: Session = Depends(get_db)):
    loc_count = (select(func.count()).select_from(LocationBin).where(LocationBin.bin_type_id == BinType.id)
                 .correlate(BinType).scalar_subquery())
    bin_count = (select(func.coalesce(func.sum(LocationBin.quantity), 0)).where(LocationBin.bin_type_id == BinType.id)
                 .correlate(BinType).scalar_subquery())
    # Decimais são guardados como inteiros escalados (×10.000): converte para REAL antes da conta.
    real = lambda col: cast(col, Float) / 10_000  # noqa: E731
    volume = func.coalesce(real(BinType.max_volume), real(BinType.height) * real(BinType.width) * real(BinType.length) / 1000)
    stmt = text_search(select(BinType), q, BinType.code, BinType.name)
    stmt, srt = apply_sort(stmt, sort, dir, {
        "code": BinType.code, "name": BinType.name,
        "height": BinType.height, "width": BinType.width, "length": BinType.length,
        "max_weight": BinType.max_weight, "volume": volume,
        "locations": loc_count, "bins": bin_count,
    }, default="code", tiebreak=BinType.id)
    pg = paginate(db, stmt, page)
    return render(request, "bin_types/list.html", page=pg, q=q, sort=srt, usage=_usage(db, [b.id for b in pg.items]))


@router.get("/new")
def new_bin_type(request: Request, db: Session = Depends(get_db)):
    return render(request, "bin_types/form.html", bin_type=None, data={}, errors={}, parents=_parents(db))


@router.post("/new")
def create_bin_type(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "bin_types/form.html", status_code=422, bin_type=None, data=data, errors=errors,
                      parents=_parents(db))
    for k in ("div_w_txt", "div_l_txt"):
        data.pop(k, None)
    bt = BinType(**data)
    db.add(bt)
    db.commit()
    return redirect("/bin-types", f"Tipo de bin {bt.code} criado.")


@router.get("/{bin_type_id}/edit")
def edit_bin_type(bin_type_id: int, request: Request, db: Session = Depends(get_db)):
    bt = db.get(BinType, bin_type_id)
    if not bt:
        return redirect("/bin-types", "Tipo de bin não encontrado.", "danger")
    data = {k: getattr(bt, k) for k in ("code", "name", "height", "width", "length", "max_weight", "max_volume",
                                        "parent_id", "dims_auto")}
    data["div_w_txt"], data["div_l_txt"] = (str(bt.grid[0]), str(bt.grid[1])) if bt.is_fraction else ("", "")
    if bt.dims_auto:  # dimensões vindas da bin pai: o formulário mostra vazio (continua automático)
        for k in ("height", "width", "length", "max_weight", "max_volume"):
            data[k] = None
    return render(request, "bin_types/form.html", bin_type=bt, data=data, errors={},
                  usage=_usage(db, [bt.id]).get(bt.id, (0, 0)), parents=_parents(db, bt.id))


@router.post("/{bin_type_id}/edit")
def update_bin_type(bin_type_id: int, request: Request, form: FormData = Depends(form_data),
                    db: Session = Depends(get_db)):
    bt = db.get(BinType, bin_type_id)
    if not bt:
        return redirect("/bin-types", "Tipo de bin não encontrado.", "danger")
    errors, data = _validate(db, form, bin_type_id)
    if errors:
        return render(request, "bin_types/form.html", status_code=422, bin_type=bt, data=data, errors=errors,
                      usage=_usage(db, [bt.id]).get(bt.id, (0, 0)), parents=_parents(db, bt.id))
    for k in ("div_w_txt", "div_l_txt"):
        data.pop(k, None)
    for k, v in data.items():
        setattr(bt, k, v)
    db.flush()
    refresh_children(db, bt)
    db.flush()
    adj = structure_message(fit_structure(db))
    db.commit()
    return redirect("/bin-types", f"Tipo de bin {bt.code} atualizado. {adj}".strip())


@router.post("/{bin_type_id}/delete")
def delete_bin_type(bin_type_id: int, db: Session = Depends(get_db)):
    bt = db.get(BinType, bin_type_id)
    if not bt:
        return redirect("/bin-types", "Tipo de bin não encontrado.", "danger")
    n_locs, _ = _usage(db, [bt.id]).get(bt.id, (0, 0))
    if n_locs:
        return redirect("/bin-types", f"O tipo {bt.code} está em uso em {n_locs} endereço(s). Remova-o dos endereços "
                                      "(Endereços → selecionar → Atribuir bins → Remover este tipo) antes de excluir.",
                        "danger")
    kids = list(db.scalars(select(BinType.code).where(BinType.parent_id == bt.id)))
    if kids:
        return redirect("/bin-types", f"O tipo {bt.code} é bin pai das frações {', '.join(kids)}. Exclua ou altere "
                                      "essas frações antes.", "danger")
    code = bt.code
    db.delete(bt)
    db.commit()
    return redirect("/bin-types", f"Tipo de bin {code} excluído.")

