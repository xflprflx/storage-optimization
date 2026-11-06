"""Tipos de bin que são fração (compartimento) de uma bin inteira: ex. "1/2 P" = metade da bin P."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BinType

MAX_CELLS = 48
Q = Decimal("0.0001")


def parse_grid(cols: str, rows: str, fraction: str = "", axis: str = "width") -> tuple[Optional[tuple[int, int]], str]:
    """Divisão da bin pai: colunas (na largura) × fileiras (no comprimento). Também aceita só a fração ("1/2"),
    dividida na direção `axis` (compatível com a versão anterior). Vazio -> (None, '')."""
    def num(v):
        v = (v or "").strip()
        if not v:
            return None
        if not v.isdigit() or int(v) < 1:
            raise ValueError
        return int(v)
    try:
        c, r = num(cols), num(rows)
    except ValueError:
        return None, "Divisões: use números inteiros a partir de 1."
    if c is None and r is None:
        f = (fraction or "").replace(" ", "")
        if not f:
            return None, ""
        m = re.fullmatch(r"1/(\d+)", f)
        if not m or int(m.group(1)) < 2:
            return None, "Fração inválida: use 1/2, 1/3, 1/4… (ou informe as divisões)."
        n = int(m.group(1))
        c, r = (1, n) if axis == "length" else (n, 1)
    c, r = c or 1, r or 1
    if c * r < 2:
        return None, "A divisão precisa ter ao menos 2 compartimentos (ex.: 2 × 1)."
    if c * r > MAX_CELLS:
        return None, f"No máximo {MAX_CELLS} compartimentos por bin."
    return (c, r), ""


def derived_dims(parent: BinType, cols: int, rows: int) -> dict:
    """Dimensões internas do compartimento a partir da bin pai (grade cols × rows)."""
    n = Decimal(cols * rows)
    q = lambda v, d: (v / Decimal(d)).quantize(Q) if v is not None else None  # noqa: E731
    return {"height": parent.height, "width": q(parent.width, cols), "length": q(parent.length, rows),
            "max_weight": q(parent.max_weight, n), "max_volume": q(parent.max_volume, n)}


def refresh_children(db: Session, parent: BinType) -> int:
    """Atualiza as frações com dimensões automáticas quando a bin pai muda. Retorna quantas mudaram."""
    n = 0
    for c in db.scalars(select(BinType).where(BinType.parent_id == parent.id)):
        if c.dims_auto and c.is_fraction:
            for k, v in derived_dims(parent, *c.grid).items():
                setattr(c, k, v)
            n += 1
    return n


def footprint_width(bt: BinType) -> Optional[float]:
    """Largura de frente "rateada" de um tipo: num compartimento, a fração da largura da bin pai."""
    if bt.is_fraction and bt.parent is not None and bt.parent.width is not None:
        return float(bt.parent.width) * bt.fraction
    return float(bt.width) if bt.width is not None else None


def physical_parents(qty: int, bt: BinType) -> int:
    """Quantas bins pai (inteiras) ocupam `qty` compartimentos deste tipo."""
    if not bt.is_fraction:
        return qty
    k = bt.per_parent
    return -(-qty // k)
