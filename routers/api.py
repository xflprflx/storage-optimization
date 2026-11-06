"""Endpoints JSON de apoio às telas (autocomplete de produtos e endereços)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import Product, StorageLocation
from routers.common import text_search

router = APIRouter(prefix="/api", tags=["API"])


@router.get("/products/search")
def search_products(q: str = Query("", max_length=100), limit: int = 20, db: Session = Depends(get_db)):
    stmt = select(Product).order_by(Product.sku).limit(min(limit, 50))
    stmt = text_search(stmt, q, Product.sku, Product.name)
    return [{"id": p.id, "sku": p.sku, "name": p.name, "label": f"{p.sku} - {p.name}"} for p in db.scalars(stmt)]


@router.get("/locations/search")
def search_locations(
    q: str = Query("", max_length=100),
    branch_id: int | None = None,
    warehouse_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    stmt = (
        select(StorageLocation)
        .options(joinedload(StorageLocation.branch), joinedload(StorageLocation.warehouse))
        .order_by(StorageLocation.address)
        .limit(min(limit, 50))
    )
    if branch_id:
        stmt = stmt.where(StorageLocation.branch_id == branch_id)
    if warehouse_id:
        stmt = stmt.where(StorageLocation.warehouse_id == warehouse_id)
    stmt = text_search(stmt, q, StorageLocation.address, address_columns=(StorageLocation.address,))
    return [
        {"id": l.id, "address": l.address, "label": l.full_label, "occupied": l.is_occupied}
        for l in db.scalars(stmt)
    ]
