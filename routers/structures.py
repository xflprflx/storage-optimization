"""CRUD de Estruturas de armazenagem (módulos de estante/porta-palete usados no projeto de filial nova)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import StructureType
from routers.common import FormErrors, apply_sort, f_dec, f_int, f_str, form_data, paginate, redirect, render, text_search
from services.addressing import normalize_code

router = APIRouter(prefix="/structures", tags=["Estruturas de armazenagem"])

KINDS = ["Estante", "Porta-palete", "Flow rack", "Cantilever", "Mezanino", "Outro"]
NUM_FIELDS = (  # chave, rótulo, obrigatório
    ("width", "Frente do módulo", True),
    ("depth", "Profundidade", True),
    ("height", "Altura total", True),
    ("level_height", "Vão útil por nível", True),
    ("max_load", "Carga máxima por nível", False),
    ("cost", "Custo por módulo", False),
)


def _validate(db: Session, form: FormData, sid: int | None):
    errors = FormErrors()
    data = {"code": normalize_code(f_str(form, "code")), "name": f_str(form, "name", 150),
            "kind": f_str(form, "kind", 40), "notes": f_str(form, "notes", 200)}
    if not data["code"]:
        errors.add("code", "Código é obrigatório.")
    elif len(data["code"]) > 30:
        errors.add("code", "Código deve ter no máximo 30 caracteres.")
    else:
        stmt = select(StructureType.id).where(StructureType.code == data["code"])
        if sid:
            stmt = stmt.where(StructureType.id != sid)
        if db.scalar(stmt):
            errors.add("code", f"Já existe uma estrutura com o código {data['code']}.")
    if not data["name"]:
        errors.add("name", "Nome é obrigatório.")
    for key, label, req in NUM_FIELDS:
        val = f_dec(form, key, errors, label)
        if val is None and req and not errors.get(key):
            errors.add(key, f"{label}: obrigatório.")
        elif val is not None and val == 0 and key != "cost":
            errors.add(key, f"{label}: deve ser maior que zero.")
        data[key] = val
    data["levels"] = f_int(form, "levels")
    if not data["levels"] or data["levels"] < 1:
        errors.add("levels", "Nº de níveis: inteiro maior que zero.")
    if not errors and data["levels"] * data["level_height"] > data["height"]:
        errors.add("level_height", f"{data['levels']} níveis × vão {data['level_height']} cm passam da altura total "
                                   f"({data['height']} cm).")
    return errors, data


@router.get("")
def list_structures(request: Request, q: str = "", sort: str = "", dir: str = "", page: int = 1,
                    db: Session = Depends(get_db)):
    stmt = text_search(select(StructureType), q, StructureType.code, StructureType.name, StructureType.kind)
    stmt, srt = apply_sort(stmt, sort, dir, {
        "code": StructureType.code, "name": StructureType.name, "kind": StructureType.kind,
        "width": StructureType.width, "depth": StructureType.depth, "height": StructureType.height,
        "levels": StructureType.levels, "level_height": StructureType.level_height, "cost": StructureType.cost,
    }, default="code", tiebreak=StructureType.id)
    return render(request, "structures/list.html", page=paginate(db, stmt, page), q=q, sort=srt)


@router.get("/new")
def new_structure(request: Request):
    return render(request, "structures/form.html", item=None, data={"kind": "Estante"}, errors={}, kinds=KINDS)


@router.post("/new")
def create_structure(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    errors, data = _validate(db, form, None)
    if errors:
        return render(request, "structures/form.html", status_code=422, item=None, data=data, errors=errors,
                      kinds=KINDS)
    st = StructureType(**data)
    db.add(st)
    db.commit()
    back = form.get("back") or "/structures"
    return redirect(back if back.startswith("/") else "/structures", f"Estrutura {st.code} criada.")


@router.get("/{sid}/edit")
def edit_structure(sid: int, request: Request, db: Session = Depends(get_db)):
    st = db.get(StructureType, sid)
    if not st:
        return redirect("/structures", "Estrutura não encontrada.", "danger")
    data = {k: getattr(st, k) for k in ("code", "name", "kind", "notes", "levels") + tuple(f[0] for f in NUM_FIELDS)}
    return render(request, "structures/form.html", item=st, data=data, errors={}, kinds=KINDS)


@router.post("/{sid}/edit")
def update_structure(sid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    st = db.get(StructureType, sid)
    if not st:
        return redirect("/structures", "Estrutura não encontrada.", "danger")
    errors, data = _validate(db, form, sid)
    if errors:
        return render(request, "structures/form.html", status_code=422, item=st, data=data, errors=errors, kinds=KINDS)
    for k, v in data.items():
        setattr(st, k, v)
    db.commit()
    return redirect("/structures", f"Estrutura {st.code} atualizada.")


@router.post("/{sid}/delete")
def delete_structure(sid: int, db: Session = Depends(get_db)):
    st = db.get(StructureType, sid)
    if not st:
        return redirect("/structures", "Estrutura não encontrada.", "danger")
    code = st.code
    db.delete(st)
    db.commit()
    return redirect("/structures", f"Estrutura {code} excluída.")
