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
from services.i18n import N_, _

router = APIRouter(prefix="/structures", tags=[N_("Estruturas de armazenagem")])

KINDS = [N_("Estante"), "Porta-palete", N_("Palete no chão"), N_("Flow rack"), N_("Cantilever"), N_("Mezanino"), N_("Outro")]
FLOOR_KIND = N_("Palete no chão")  # paletes direto no piso (empilhados por nível), sem montantes nem longarinas
NUM_FIELDS = (  # chave, rótulo, obrigatório
    ("width", N_("Frente do módulo"), True),
    ("depth", N_("Profundidade"), True),
    ("height", N_("Altura total"), True),
    ("level_height", N_("Vão útil por nível"), True),
    ("max_load", N_("Carga máxima por nível"), False),
    ("cost", N_("Custo por módulo"), False),
    ("golden_from", N_("Zona de ouro — início"), False),
    ("golden_to", N_("Zona de ouro — fim"), False),
    ("first_level_cm", N_("Altura do 1º nível"), False),
)


def _validate(db: Session, form: FormData, sid: int | None):
    errors = FormErrors()
    data = {"code": normalize_code(f_str(form, "code")), "name": f_str(form, "name", 150),
            "kind": f_str(form, "kind", 40), "notes": f_str(form, "notes", 200)}
    if not data["code"]:
        errors.add("code", _("Código é obrigatório."))
    elif len(data["code"]) > 30:
        errors.add("code", _("Código deve ter no máximo 30 caracteres."))
    else:
        stmt = select(StructureType.id).where(StructureType.code == data["code"])
        if sid:
            stmt = stmt.where(StructureType.id != sid)
        if db.scalar(stmt):
            errors.add("code", _("Já existe uma estrutura com o código {code}.", code=data['code']))
    if not data["name"]:
        errors.add("name", _("Nome é obrigatório."))
    for key, label, req in NUM_FIELDS:
        val = f_dec(form, key, errors, label)
        if val is None and req and not errors.get(key):
            errors.add(key, _("{label}: obrigatório.", label=label))
        elif val is not None and val == 0 and key not in ("cost", "golden_from", "first_level_cm"):
            errors.add(key, _("{label}: deve ser maior que zero.", label=label))
        data[key] = val
    data["levels"] = f_int(form, "levels")
    if not data["levels"] or data["levels"] < 1:
        errors.add("levels", _("Nº de níveis: inteiro maior que zero."))
    gf, gt = data.get("golden_from"), data.get("golden_to")
    if (gf is None) != (gt is None):
        errors.add("golden_to", _("Zona de ouro: informe início e fim (ou deixe os dois vazios)."))
    elif gf is not None and gt <= gf:
        errors.add("golden_to", _("Zona de ouro: o fim deve ser maior que o início."))
    elif gf is not None and data.get("height") and gt > data["height"]:
        errors.add("golden_to", _("Zona de ouro: o fim passa da altura total ({height} cm).", height=data['height']))
    # vão de cada nível (opcional): "40; 40; 35" de baixo para cima; faltando, o vão útil vale para os de cima
    raw = f_str(form, "level_heights", 400)
    hs = []
    for part in [p_ for p_ in raw.replace(",", ".").replace("/", ";").replace(" ", ";").split(";") if p_.strip()]:
        try:
            v = float(part)
        except ValueError:
            errors.add("level_heights", _("Vão por nível inválido: {part}.", part=f"{part!r}"))
            break
        if v <= 0:
            errors.add("level_heights", _("Vão por nível: os valores devem ser maiores que zero."))
            break
        hs.append(round(v, 2))
    if data["levels"] and len(hs) > data["levels"]:
        errors.add("level_heights", _("Foram informados {len} vãos para {levels} níveis.", len=len(hs), levels=data['levels']))
    data["level_heights"] = hs or None
    base = float(data.get("first_level_cm") or 0)
    if not errors and data["levels"] and data.get("level_height") and data.get("height"):
        vs = [hs[i] if i < len(hs) else float(data["level_height"]) for i in range(data["levels"])]
        if base + sum(vs) > float(data["height"]) + 1e-6:
            what = _("os vãos por nível") if hs else _("{levels} níveis × vão {level_height} cm", levels=data['levels'], level_height=data['level_height'])
            errors.add("level_heights" if hs else "level_height",
                       _("1º nível a {base} cm + {what} (= {v} cm) passam da altura total ({height} cm).", base=f"{base:g}", what=what, v=f"{base + sum(vs):g}", height=data['height']))
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
    from services.structure_service import usage_counts

    return render(request, "structures/list.html", page=paginate(db, stmt, page), q=q, sort=srt, usage=usage_counts(db))


@router.get("/new")
def new_structure(request: Request):
    return render(request, "structures/form.html", item=None, data={"kind": _("Estante")}, errors={}, kinds=KINDS)


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
    return redirect(back if back.startswith("/") else "/structures", _("Estrutura {code} criada.", code=st.code))


@router.get("/{sid}/edit")
def edit_structure(sid: int, request: Request, db: Session = Depends(get_db)):
    st = db.get(StructureType, sid)
    if not st:
        return redirect("/structures", _("Estrutura não encontrada."), "danger")
    data = {k: getattr(st, k) for k in ("code", "name", "kind", "notes", "levels", "level_heights")
            + tuple(f[0] for f in NUM_FIELDS)}
    return render(request, "structures/form.html", item=st, data=data, errors={}, kinds=KINDS)


@router.post("/{sid}/edit")
def update_structure(sid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    st = db.get(StructureType, sid)
    if not st:
        return redirect("/structures", _("Estrutura não encontrada."), "danger")
    errors, data = _validate(db, form, sid)
    if errors:
        return render(request, "structures/form.html", status_code=422, item=st, data=data, errors=errors, kinds=KINDS)
    for k, v in data.items():
        setattr(st, k, v)
    db.commit()
    return redirect("/structures", _("Estrutura {code} atualizada.", code=st.code))


@router.post("/{sid}/delete")
def delete_structure(sid: int, db: Session = Depends(get_db)):
    st = db.get(StructureType, sid)
    if not st:
        return redirect("/structures", _("Estrutura não encontrada."), "danger")
    from services.structure_service import usage_counts

    n = usage_counts(db).get(st.id, 0)
    if n:
        return redirect("/structures", _("A estrutura {code} está em {n} estante(s). Remova-a das estantes em Capacidade dos níveis → Aplicar estrutura → remover, antes de excluir.", code=st.code, n=n), "danger")
    code = st.code
    db.delete(st)
    db.commit()
    return redirect("/structures", _("Estrutura {code} excluída.", code=code))
