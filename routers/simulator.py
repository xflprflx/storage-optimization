"""Simulador: a porta de entrada do simulador de trajeto e da caminhada em 3D, para as filiais existentes (histórico
de pedidos + planta desenhada) e para os projetos de filial nova (aba Pedidos)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Branch, BranchLayout, PickHistory, SiteProject
from routers.common import render
from services.i18n import N_

router = APIRouter(prefix="/simulator", tags=[N_("Simulador")])


def _summary(data: dict | None) -> dict:
    """Resumo da amostra de pedidos: pedidos no histórico, na amostra, dias e se tem o horário de liberação."""
    data = data or {}
    sample = data.get("sample") or []
    return {"orders": int(data.get("orders") or 0), "sample": len(sample),
            "days": len({e[3] for e in sample if len(e) > 3 and e[3]}),
            "release": any(len(e) > 4 for e in sample)}


@router.get("")
def simulator_home(request: Request, db: Session = Depends(get_db)):
    hist = {h.branch_id: h for h in db.scalars(select(PickHistory))}
    plans = {b for (b,) in db.execute(select(BranchLayout.branch_id).distinct())}
    branches = [{"b": b, "hist": _summary(hist[b.id].data) if b.id in hist else None, "plan": b.id in plans}
                for b in db.scalars(select(Branch).order_by(Branch.code))]
    projects = [{"p": p, "orders": _summary((p.config or {}).get("orders"))}
                for p in db.scalars(select(SiteProject).order_by(SiteProject.id.desc()))]
    return render(request, "simulator/index.html", branches=branches, projects=projects)
