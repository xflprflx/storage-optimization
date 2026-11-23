"""Raio-X da filial: dados, estatísticas e insights do estado atual (services/branch_insights.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Branch, StorageLocation, Warehouse
from routers.common import render, to_int
from services.i18n import N_

router = APIRouter(prefix="/insights", tags=[N_("Raio-X da filial")])


@router.get("")
def branch_insights(request: Request, branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from services.branch_insights import build, unit_label

    branches = list(db.scalars(select(Branch).order_by(Branch.code)))
    bid = to_int(branch_id)
    if bid is None and branches:  # sem filial escolhida: a primeira que tem endereços
        bid = db.scalar(select(StorageLocation.branch_id).order_by(StorageLocation.branch_id).limit(1)) or branches[0].id
    wid = to_int(warehouse_id)
    br = db.get(Branch, bid) if bid else None
    warehouses = list(br.warehouses) if br is not None else []
    if wid and wid not in {w.id for w in warehouses}:
        wid = None
    data = build(db, br.id, wid) if br is not None else {}
    return render(request, "insights/branch.html", branches=branches, warehouses=warehouses, bid=bid, wid=wid,
                  d=data, unit_label=unit_label, all_warehouses=list(db.scalars(select(Warehouse))))
