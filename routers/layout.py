"""Mapa do estoque: desenho proporcional das estruturas a partir do cadastro de endereços."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Branch, StorageLocation, Warehouse, branch_warehouse
from routers.common import render, to_int
from services.layout_service import get_preview, load_layout

router = APIRouter(tags=["Mapa do estoque"])


def _lookups(db: Session) -> dict:
    branches = list(db.scalars(select(Branch).order_by(Branch.code)))
    warehouses = list(db.scalars(select(Warehouse).order_by(Warehouse.code)))
    wh_branches: dict[int, list[int]] = {}
    for b, w in db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)).all():
        wh_branches.setdefault(w, []).append(b)
    # Filiais com endereços "sem armazém"
    no_wh = set(db.scalars(select(StorageLocation.branch_id).where(StorageLocation.warehouse_id.is_(None)).distinct()))
    return {"branches": branches, "warehouses": warehouses, "wh_branches": wh_branches, "branches_no_wh": no_wh}


@router.get("/layout")
def layout_page(request: Request, branch_id: str = "", warehouse_id: str = "", aisle: str = "", preview: str = "",
                embed: str = "", db: Session = Depends(get_db)):
    lk = _lookups(db)
    bid = to_int(branch_id)
    # Sem filial escolhida: sugere o primeiro depósito que tem endereços
    if not bid:
        first = db.execute(select(StorageLocation.branch_id, StorageLocation.warehouse_id)
                           .order_by(StorageLocation.branch_id, StorageLocation.warehouse_id).limit(1)).first()
        if first:
            bid = first[0]
            warehouse_id = str(first[1]) if first[1] else "none"
    return render(request, "layout/index.html", branch_id=bid, warehouse_id=warehouse_id, aisle=aisle,
                  preview=preview, embed=bool(embed), **lk)


@router.get("/layout/data")
def layout_data(branch_id: int, warehouse_id: str = "", aisle: str = "", preview: str = "",
                db: Session = Depends(get_db)):
    if not db.get(Branch, branch_id):
        return JSONResponse({"detail": "Filial não encontrada."}, status_code=404)
    wid = None if warehouse_id in ("", "none") else to_int(warehouse_id)
    pv = get_preview(preview) if preview else None
    if preview and pv is None:
        return JSONResponse({"detail": "A pré-visualização expirou: clique de novo em Pré-visualizar."}, status_code=404)
    out = load_layout(db, branch_id, wid, aisle.strip().upper(), pv)
    if pv is not None:
        out["preview"] = {"new": len(pv["new"]), "removed": len(pv["removed"]), "conflicts": len(pv["conflicts"])}
    return out
