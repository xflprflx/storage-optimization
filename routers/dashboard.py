"""Painel inicial com indicadores gerais."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from database import get_db
from models import BinType, Branch, LocationBin, Product, StockItem, StorageLocation, Warehouse
from routers.common import render

router = APIRouter(tags=["Painel"])


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    count = lambda model: db.scalar(select(func.count()).select_from(model)) or 0  # noqa: E731
    total_locations = count(StorageLocation)
    occupied = db.scalar(select(func.count()).where(StorageLocation.is_occupied.is_(True))) or 0
    with_bins = db.scalar(select(func.count(func.distinct(LocationBin.location_id)))) or 0
    stats = {
        "branches": count(Branch),
        "warehouses": count(Warehouse),
        "locations": total_locations,
        "occupied": occupied,
        "free": total_locations - occupied,
        "occupancy_pct": round(occupied * 100 / total_locations, 1) if total_locations else 0,
        "products": count(Product),
        "bin_types": count(BinType),
        "with_bins": with_bins,
        "without_bins": total_locations - with_bins,
        "stock_qty": db.scalar(select(func.coalesce(func.sum(StockItem.quantity), Decimal("0")))) or Decimal("0"),
        "products_without_location": db.scalar(
            select(func.count()).where(Product.default_location_id.is_(None),
                                       ~Product.id.in_(select(StockItem.product_id)))) or 0,
    }
    # Ocupação por filial/armazém
    by_scope = db.execute(
        select(Branch.code, Warehouse.code, func.count(StorageLocation.id),
               func.sum(cast(StorageLocation.is_occupied, Integer)))
        .select_from(StorageLocation)
        .join(Branch, Branch.id == StorageLocation.branch_id)
        .outerjoin(Warehouse, Warehouse.id == StorageLocation.warehouse_id)
        .group_by(Branch.code, Warehouse.code).order_by(Branch.code, Warehouse.code)
    ).all()
    return render(request, "dashboard.html", stats=stats, by_scope=by_scope)
