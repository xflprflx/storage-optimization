"""Tipo de material do produto: normalização, opções para filtros e filtro reutilizável."""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Product

NO_MATERIAL = "__none__"  # valor do filtro para "produtos sem tipo de material"
MAX_LEN = 60


def clean_material(raw) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()[:MAX_LEN]


class MaterialResolver:
    """Reaproveita a grafia já cadastrada (sem diferenciar maiúsculas/minúsculas):
    'eletronico' digitado vira 'Eletronico' se já existir assim."""

    def __init__(self, db: Session):
        self.known: dict[str, str] = {}
        for (v,) in db.execute(select(Product.material_type).distinct().where(Product.material_type != "")):
            self.known.setdefault(v.casefold(), v)

    def __call__(self, raw) -> str:
        v = clean_material(raw)
        if not v:
            return ""
        return self.known.setdefault(v.casefold(), v)


def material_types(db: Session) -> list[tuple[str, int]]:
    """[(tipo, nº de produtos)] em ordem alfabética (inclui '' para os sem tipo, se houver)."""
    rows = db.execute(select(Product.material_type, func.count()).group_by(Product.material_type)).all()
    return sorted(rows, key=lambda r: (r[0] == "", r[0].casefold()))


def material_condition(value: Optional[str]):
    """Condição SQL sobre Product.material_type para o valor do filtro (None = sem filtro)."""
    value = (value or "").strip()
    if not value:
        return None
    if value == NO_MATERIAL:
        return Product.material_type == ""
    return Product.material_type == value
