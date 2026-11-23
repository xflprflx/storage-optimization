"""Plano de ocupação X%: max level de cada SKU para que as bins que ele ocupa fiquem com X% do volume
(services/occupancy_plan.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import Branch
from routers.common import form_data, redirect, render, to_int
from services.i18n import N_, _

router = APIRouter(prefix="/optimization/occupancy", tags=[N_("Plano de ocupação")])
KEYS = ("branch_id", "warehouse_id", "q", "sku", "material")
DEFAULTS = {"target": "85", "only_increase": "1", "round_packs": "1", "days": "30", "unitization": "branch"}


def _params(src) -> tuple[dict, dict, list[str]]:
    filters = {k: str(src.get(k) or "") for k in KEYS}
    raw = {k: str(src.get(k) if src.get(k) is not None else ("" if src.get("run") else v)) for k, v in DEFAULTS.items()}
    errors = []
    try:
        t = float(raw["target"].replace(",", "."))
        if not 1 <= t <= 100:
            raise ValueError
    except ValueError:
        errors.append(_("Ocupação desejada: informe um número de 1 a 100."))
    if raw["unitization"] not in ("branch", "off", "cases", "full"):
        raw["unitization"] = "branch"
    return filters, raw, errors


def _plan(db: Session, filters: dict, raw: dict):
    from routers.optimization import _scope_ids
    from services.occupancy_plan import build

    ids = _scope_ids(db, {**{k: "" for k in ("occupied", "bin_type_id")}, **filters})
    on = lambda k: raw.get(k) in ("1", "on", "true")  # noqa: E731
    return build(db, int(filters["branch_id"]), ids, float(raw["target"].replace(",", ".")),
                 only_increase=on("only_increase"), round_packs=on("round_packs"), days=to_int(raw["days"]) or 30,
                 unit_run=raw["unitization"])


@router.get("")
def occupancy_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    filters, raw, errors = _params(qp)
    branches = list(db.scalars(select(Branch).order_by(Branch.code)))
    if not filters["branch_id"] and branches:
        filters["branch_id"] = str(branches[0].id)
    plan = None
    if qp.get("run") and not errors and to_int(filters["branch_id"]):
        plan = _plan(db, filters, raw)
        db.rollback()  # o cálculo não grava nada (o conteúdo presumido é refeito quando preciso)
    br = db.get(Branch, to_int(filters["branch_id"])) if to_int(filters["branch_id"]) else None
    from services.branch_insights import unit_label

    return render(request, "occupancy/plan.html", filters=filters, raw=raw, errors=errors, plan=plan,
                  branches=branches, warehouses=list(br.warehouses) if br is not None else [], branch=br,
                  unit_label=unit_label)


@router.get("/plan.xlsx")
def occupancy_xlsx(request: Request, db: Session = Depends(get_db)):
    from services.occupancy_plan import to_xlsx

    filters, raw, errors = _params(request.query_params)
    if errors or not to_int(filters["branch_id"]):
        return redirect("/optimization/occupancy", " ".join(errors) or _("Escolha a filial."), "danger")
    plan = _plan(db, filters, raw)
    db.rollback()
    br = db.get(Branch, int(filters["branch_id"]))
    title = _("Plano de ocupação {p}% — {branch}", p=raw["target"], branch=br.code if br else "")
    return Response(to_xlsx(plan, title), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="plano_ocupacao_{raw["target"]}.xlsx"'})


@router.post("/apply")
def occupancy_apply(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Grava os max levels sugeridos (dos SKUs marcados, ou de todos) na política de estoque da filial."""
    from urllib.parse import urlencode

    from services.occupancy_plan import apply

    src = {k[2:]: v for k, v in form.items() if k.startswith("p_")}
    filters, raw, errors = _params({**src, "run": "1"})
    back = "/optimization/occupancy?" + urlencode({**filters, **raw, "run": "1"})
    if errors or not to_int(filters["branch_id"]):
        return redirect(back, " ".join(errors) or _("Escolha a filial."), "danger")
    plan = _plan(db, filters, raw)
    only = None if form.get("scope") == "all" else {i for i in (to_int(v) for v in form.getlist("ids")) if i}
    if only is not None and not only:
        db.rollback()
        return redirect(back, _("Marque ao menos um SKU (ou escolha aplicar em todos)."), "warning")
    n = apply(db, int(filters["branch_id"]), plan["rows"], only)
    db.commit()
    return redirect(back, _("Max level atualizado em {n} SKU(s) na política de estoque da filial.", n=n))
