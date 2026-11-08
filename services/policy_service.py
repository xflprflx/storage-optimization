"""Política de estoque (produto × filial): endereço padrão, min e max level.

O cadastro do produto é único (compartilhado por todas as filiais); o que é de cada filial fica aqui. Funções de
apoio para quem lê/grava esses dados sem repetir as consultas.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import StockPolicy, StorageLocation


def get_policy(db: Session, branch_id: int, product_id: int, create: bool = False) -> Optional[StockPolicy]:
    """Política da filial para o produto (criada se `create`). A sessão não descarrega sozinha (autoflush=False):
    a política criada antes no mesmo lote ainda não está no banco, então procura também nas pendentes."""
    for obj in db.new:
        if isinstance(obj, StockPolicy) and obj.branch_id == branch_id and obj.product_id == product_id:
            return obj
    pol = db.scalar(select(StockPolicy).where(StockPolicy.branch_id == branch_id, StockPolicy.product_id == product_id))
    if pol is None and create:
        pol = StockPolicy(branch_id=branch_id, product_id=product_id)
        db.add(pol)
    return pol


def set_default_location(db: Session, product_id: int, location_id: Optional[int],
                         branch_id: Optional[int] = None) -> Optional[StockPolicy]:
    """Endereço padrão do produto na filial do endereço (ou na filial informada, para limpar)."""
    if location_id:
        loc = db.get(StorageLocation, location_id)
        if loc is None:
            return None
        branch_id = loc.branch_id
    if not branch_id:
        return None
    pol = get_policy(db, branch_id, product_id, create=bool(location_id))
    if pol is not None:
        pol.default_location_id = location_id or None
    return pol


def policies_by_product(db: Session, product_ids: Iterable[int], branch_id: int) -> dict[int, StockPolicy]:
    ids = list({i for i in product_ids if i})
    out: dict[int, StockPolicy] = {}
    for i in range(0, len(ids), 900):
        for pol in db.scalars(select(StockPolicy).where(StockPolicy.branch_id == branch_id,
                                                        StockPolicy.product_id.in_(ids[i:i + 900]))):
            out[pol.product_id] = pol
    return out
