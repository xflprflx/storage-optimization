"""Cadastros de Tipos de estrutura (composição de peças, furação, espessura do nível e aparência no 3D) e de Peças de
estrutura (código, nome e custo)."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import StructureKind, StructureKindPiece, StructurePiece, StructureType
from routers.common import (FormErrors, apply_sort, f_dec, f_str, form_data, paginate, redirect, render,
                            text_search)
from services.addressing import normalize_code
from services.i18n import N_, _
from services.structure_kinds import PER, refresh_floor

router = APIRouter(tags=[N_("Tipos e peças de estrutura")])


# --------------------------------------------------------------------------- #
# Peças
# --------------------------------------------------------------------------- #
def _piece_usage(db: Session) -> dict[int, int]:
    return dict(db.execute(select(StructureKindPiece.piece_id, func.count(func.distinct(StructureKindPiece.kind_id)))
                           .group_by(StructureKindPiece.piece_id)).all())


def _piece_validate(db: Session, form: FormData, pid: int | None):
    errors = FormErrors()
    data = {"code": normalize_code(f_str(form, "code")), "name": f_str(form, "name", 150), "notes": f_str(form, "notes", 200)}
    if not data["code"]:
        errors.add("code", _("Código é obrigatório."))
    elif len(data["code"]) > 30:
        errors.add("code", _("Código deve ter no máximo 30 caracteres."))
    else:
        stmt = select(StructurePiece.id).where(StructurePiece.code == data["code"])
        if pid:
            stmt = stmt.where(StructurePiece.id != pid)
        if db.scalar(stmt):
            errors.add("code", _("Já existe uma peça com o código {code}.", code=data["code"]))
    if not data["name"]:
        errors.add("name", _("Nome é obrigatório."))
    data["cost"] = f_dec(form, "cost", errors, _("Custo por peça"))
    return errors, data


@router.get("/structure-pieces")
def list_pieces(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1, db: Session = Depends(get_db)):
    stmt = text_search(select(StructurePiece), q, StructurePiece.code, StructurePiece.name)
    stmt, srt = apply_sort(stmt, sort, dir, {"code": StructurePiece.code, "name": StructurePiece.name,
                                             "cost": StructurePiece.cost}, default="code", tiebreak=StructurePiece.id)
    return render(request, "structure_kinds/pieces.html", page=paginate(db, stmt, page), q=q, sort=srt, usage=_piece_usage(db))


@router.get("/structure-pieces/new")
def new_piece(request: Request):
    return render(request, "structure_kinds/piece_form.html", item=None, data={}, errors={})


@router.post("/structure-pieces/new")
def create_piece(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _piece_validate(db, form, None)
    if errors:
        return render(request, "structure_kinds/piece_form.html", status_code=422, item=None, data=data, errors=errors)
    pc = StructurePiece(**data)
    db.add(pc)
    db.commit()
    back = str(form.get("back") or "")
    return redirect(back if back.startswith("/structure-") else "/structure-pieces", _("Peça {code} criada.", code=pc.code))


@router.get("/structure-pieces/{pid}/edit")
def edit_piece(pid: int, request: Request, db: Session = Depends(get_db)):
    pc = db.get(StructurePiece, pid)
    if not pc:
        return redirect("/structure-pieces", _("Peça não encontrada."), "danger")
    return render(request, "structure_kinds/piece_form.html", item=pc,
                  data={k: getattr(pc, k) for k in ("code", "name", "cost", "notes")}, errors={})


@router.post("/structure-pieces/{pid}/edit")
def update_piece(pid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    pc = db.get(StructurePiece, pid)
    if not pc:
        return redirect("/structure-pieces", _("Peça não encontrada."), "danger")
    errors, data = _piece_validate(db, form, pid)
    if errors:
        return render(request, "structure_kinds/piece_form.html", status_code=422, item=pc, data=data, errors=errors)
    for k, v in data.items():
        setattr(pc, k, v)
    db.commit()
    return redirect("/structure-pieces", _("Peça {code} atualizada.", code=pc.code))


@router.post("/structure-pieces/{pid}/delete")
def delete_piece(pid: int, db: Session = Depends(get_db)):
    pc = db.get(StructurePiece, pid)
    if not pc:
        return redirect("/structure-pieces", _("Peça não encontrada."), "danger")
    n = _piece_usage(db).get(pc.id, 0)
    if n:
        return redirect("/structure-pieces", _("A peça {code} está na composição de {n} tipo(s) de estrutura: tire-a da composição antes de excluir.", code=pc.code, n=n), "danger")
    code = pc.code
    db.delete(pc)
    db.commit()
    return redirect("/structure-pieces", _("Peça {code} excluída.", code=code))


# --------------------------------------------------------------------------- #
# Tipos de estrutura
# --------------------------------------------------------------------------- #
def _kind_usage(db: Session) -> dict[int, int]:
    return dict(db.execute(select(StructureType.kind_id, func.count()).where(StructureType.kind_id.is_not(None))
                           .group_by(StructureType.kind_id)).all())


def _kind_validate(db: Session, form: FormData, kid: int | None):
    from services.appearance import code_text, form_values

    errors = FormErrors()
    data = {"name": f_str(form, "name", 40), "notes": f_str(form, "notes", 200),
            "floor": str(form.get("floor") or "") in ("1", "on", "true")}
    if not data["name"]:
        errors.add("name", _("Nome é obrigatório."))
    else:
        stmt = select(StructureKind.id).where(func.lower(StructureKind.name) == data["name"].lower())
        if kid:
            stmt = stmt.where(StructureKind.id != kid)
        if db.scalar(stmt):
            errors.add("name", _("Já existe um tipo de estrutura chamado {name}.", name=data["name"]))
    data["pitch_cm"] = f_dec(form, "pitch_cm", errors, _("Passo da furação"))
    data["beam_cm"] = f_dec(form, "beam_cm", errors, _("Espessura do nível"))
    for k, lbl in (("pitch_cm", _("Passo da furação")), ("beam_cm", _("Espessura do nível"))):
        if data[k] is not None and data[k] <= 0 and not errors.get(k):
            errors.add(k, _("{label}: deve ser maior que zero (ou deixe vazio).", label=lbl))
    rows = []
    pieces = form.getlist("cp_piece") if hasattr(form, "getlist") else []
    qtys = form.getlist("cp_qty") if hasattr(form, "getlist") else []
    pers = form.getlist("cp_per") if hasattr(form, "getlist") else []
    valid = {p.id for p in db.scalars(select(StructurePiece))}
    for i, (pid, q, per) in enumerate(zip(pieces, qtys, pers), 1):
        if not str(pid).strip() and not str(q).strip():
            continue  # linha vazia
        try:
            qv = Decimal(str(q).replace(",", ".").strip())
        except Exception:  # noqa: BLE001
            qv = None
        if not str(pid).isdigit() or int(pid) not in valid:
            errors.add("composition", _("Linha {i} da composição: escolha a peça.", i=i))
        elif qv is None or qv <= 0:
            errors.add("composition", _("Linha {i} da composição: a quantidade deve ser maior que zero.", i=i))
        else:
            rows.append((int(pid), qv, per if per in PER else "module"))
    data["composition"] = rows
    data.update(form_values(form, "struct", errors))
    data["look_code"] = f_str(form, "look_code", 20000) or code_text(data.get("appearance"))
    return errors, data


def _kind_ctx(db: Session) -> dict:
    return {"pieces": list(db.scalars(select(StructurePiece).order_by(StructurePiece.code))), "PER": PER}


def _save_kind(db: Session, k: StructureKind, data: dict) -> None:
    comp = data.pop("composition")
    data.pop("look_code", None)
    for key, v in data.items():
        setattr(k, key, v)
    k.pieces = [StructureKindPiece(piece_id=pid, qty=q, per=per) for pid, q, per in comp]
    db.flush()
    # estruturas do tipo: o nome do tipo acompanha (o cálculo e o 3D leem o nome)
    for st in db.scalars(select(StructureType).where(StructureType.kind_id == k.id)):
        st.kind = k.name
    db.commit()
    refresh_floor(db)


@router.get("/structure-kinds")
def list_kinds(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1, db: Session = Depends(get_db)):
    stmt = text_search(select(StructureKind), q, StructureKind.name)
    stmt, srt = apply_sort(stmt, sort, dir, {"name": StructureKind.name}, default="name", tiebreak=StructureKind.id)
    from services.appearance import model_choices

    return render(request, "structure_kinds/list.html", page=paginate(db, stmt, page), q=q, sort=srt,
                  usage=_kind_usage(db), models=dict(model_choices("struct")), PER=PER)


@router.get("/structure-kinds/new")
def new_kind(request: Request, db: Session = Depends(get_db)):
    return render(request, "structure_kinds/form.html", item=None, data={"composition": []}, errors={}, **_kind_ctx(db))


@router.post("/structure-kinds/new")
def create_kind(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _kind_validate(db, form, None)
    if errors:
        return render(request, "structure_kinds/form.html", status_code=422, item=None, data=data, errors=errors, **_kind_ctx(db))
    k = StructureKind(name=data["name"])
    db.add(k)
    _save_kind(db, k, data)
    back = str(form.get("back") or "")
    return redirect(back if back.startswith("/structure") else "/structure-kinds", _("Tipo de estrutura {name} criado.", name=k.name))


@router.get("/structure-kinds/{kid}/edit")
def edit_kind(kid: int, request: Request, db: Session = Depends(get_db)):
    from services.appearance import code_text

    k = db.get(StructureKind, kid)
    if not k:
        return redirect("/structure-kinds", _("Tipo de estrutura não encontrado."), "danger")
    data = {key: getattr(k, key) for key in ("name", "floor", "pitch_cm", "beam_cm", "notes", "look_model")}
    data["composition"] = [(p.piece_id, p.qty, p.per) for p in k.pieces]
    data["look_code"] = code_text(k.appearance)
    return render(request, "structure_kinds/form.html", item=k, data=data, errors={}, usage=_kind_usage(db).get(k.id, 0),
                  **_kind_ctx(db))


@router.post("/structure-kinds/{kid}/edit")
def update_kind(kid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    k = db.get(StructureKind, kid)
    if not k:
        return redirect("/structure-kinds", _("Tipo de estrutura não encontrado."), "danger")
    errors, data = _kind_validate(db, form, kid)
    if errors:
        return render(request, "structure_kinds/form.html", status_code=422, item=k, data=data, errors=errors,
                      usage=_kind_usage(db).get(k.id, 0), **_kind_ctx(db))
    _save_kind(db, k, data)
    return redirect("/structure-kinds", _("Tipo de estrutura {name} atualizado.", name=k.name))


@router.post("/structure-kinds/{kid}/delete")
def delete_kind(kid: int, db: Session = Depends(get_db)):
    k = db.get(StructureKind, kid)
    if not k:
        return redirect("/structure-kinds", _("Tipo de estrutura não encontrado."), "danger")
    n = _kind_usage(db).get(k.id, 0)
    if n:
        return redirect("/structure-kinds", _("{n} estrutura(s) são do tipo {name}: troque o tipo delas antes de excluir.", n=n, name=k.name), "danger")
    name = k.name
    db.delete(k)
    db.commit()
    refresh_floor(db)
    return redirect("/structure-kinds", _("Tipo de estrutura {name} excluído.", name=name))
