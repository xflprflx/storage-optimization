"""Visualização do arranjo das unidades dentro de uma bin (2D e 3D)."""
from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import BinType, Product
from routers.common import render, to_int
from services.packing import arrange, svg_mini

router = APIRouter(prefix="/packing", tags=["Arranjo nas bins"])


def _f(v):
    return float(v) if v is not None else None


def parent_info(bt, seq: Optional[int] = None) -> Optional[dict]:
    """Compartimento (fração): dados da bin pai e a posição deste compartimento nela (pela numeração da bin)."""
    if bt is None or not bt.is_fraction or bt.parent is None:
        return None
    p = bt.parent
    if None in (p.height, p.width, p.length) or None in (bt.width, bt.length):
        return None
    k = bt.per_parent
    nx, nz = bt.grid
    slot = ((seq or 1) - 1) % k  # ordem: da esquerda para a direita, da frente para o fundo
    cw, cl = float(p.width) / nx, float(p.length) / nz
    return {"h": float(p.height), "w": float(p.width), "l": float(p.length), "k": k, "nx": nx, "nz": nz,
            # fileira 1 = frente (a frente da bin fica em z = comprimento, como a seta no 3D)
            "grid": bt.grid_label, "x0": (slot % nx) * cw, "z0": float(p.length) - (slot // nx + 1) * cl,
            "slot": slot + 1, "n": ((seq or 1) - 1) // k + 1, "label": bt.fraction_label, "code": p.code}


def _arrange_bt(bt, data, rotation, seq=None):
    bdims = (_f(bt.height), _f(bt.width), _f(bt.length)) if bt and None not in (bt.height, bt.width, bt.length) else None
    arr = arrange(bt.code if bt else "?", bdims, data, rotation)
    arr.parent = parent_info(bt, seq)
    return arr


def _tok(item) -> str:
    """(SKU, qtd[, reserva]) -> 'SKU:qtd' ou 'SKU:qtd+reserva' (reserva = espaço planejado além do saldo)."""
    sku, q = item[0], item[1]
    r = item[2] if len(item) > 2 else 0
    return f"{sku}:{float(q):g}" + (f"+{float(r):g}" if r else "")


def _parse_tok(tok: str):
    sku, _, q = tok.rpartition(":")
    if not sku:
        return None
    q, _, r = q.replace(",", ".").partition("+")
    try:
        return (sku, float(q), float(r or 0))
    except ValueError:
        return None


def bin_arrangement(db: Session, bin_type_id: int, items: list[tuple[str, float]], rotation: str = "upright",
                    seq: Optional[int] = None):
    """items = [(SKU, qtd)] -> BinArrangement (dimensões vindas do cadastro)."""
    bt = db.get(BinType, bin_type_id)
    if bt is None:
        return None
    skus = [i[0] for i in items]
    prods = {p.sku: p for p in db.scalars(select(Product).where(Product.sku.in_(skus)))} if skus else {}
    data = []
    for sku, q, *res in items:
        p = prods.get(sku)
        d = (_f(p.height), _f(p.width), _f(p.length)) if p is not None and None not in (p.height, p.width, p.length) else None
        data.append((sku, q, d, res[0] if res else 0))
    return _arrange_bt(bt, data, rotation, seq)


@router.get("/3d")
def view_3d(request: Request, bt: int = 0, rot: str = "upright", title: str = "", db: Session = Depends(get_db)):
    items = []
    for raw in request.query_params.getlist("i"):
        t = _parse_tok(raw)
        if t:
            items.append(t)
    m = re.search(r"#(\d+)", title or "")
    arr = bin_arrangement(db, to_int(bt) or 0, items, "any" if rot == "any" else "upright", int(m.group(1)) if m else None)
    if arr is None:
        return render(request, "packing/view3d.html", status_code=404, arr=None, data="null", title=title, svg="")
    return render(request, "packing/view3d.html", arr=arr, data=json.dumps(arr.as_json()), title=title,
                  svg=svg_mini(arr, 160), rot=rot)


class Arranger:
    """Calcula arranjos em lote para uma tela (carrega tipos e produtos uma vez)."""

    def __init__(self, db: Session, rotation: str = "upright"):
        from urllib.parse import urlencode

        self._urlencode = urlencode
        self.db = db
        self.rotation = "any" if rotation == "any" else "upright"
        self.types = {t.id: t for t in db.scalars(select(BinType))}
        self.products: dict[str, Product] = {}

    def _load(self, skus):
        missing = [s for s in skus if s not in self.products]
        for i in range(0, len(missing), 900):
            for p in self.db.scalars(select(Product).where(Product.sku.in_(missing[i:i + 900]))):
                self.products[p.sku] = p

    def view(self, type_id: int, items: list[tuple[str, float]], title: str = "", size: int = 46,
             seq: Optional[int] = None) -> dict:
        """{'svg', 'url', 'texts': {sku: texto}, 'overflow': bool}"""
        self._load([i[0] for i in items])
        bt = self.types.get(type_id)
        data = []
        for sku, q, *res in items:
            p = self.products.get(sku)
            d = (_f(p.height), _f(p.width), _f(p.length)) if p is not None and None not in (p.height, p.width, p.length) else None
            data.append((sku, q, d, res[0] if res else 0))
        arr = _arrange_bt(bt, data, self.rotation, seq)
        q = [("bt", type_id), ("rot", self.rotation), ("title", title)] + [("i", _tok(i)) for i in items]
        return {"svg": svg_mini(arr, size) if arr.ok else "", "url": "/packing/3d?" + self._urlencode(q),
                "texts": {i.sku: i.text for i in arr.items}, "overflow": any(i.overflow for i in arr.items), "ok": arr.ok}


def case_url(title: str, rotation: str, bins: list[tuple]) -> str:
    """bins = [(lado 'before'|'after', tipo, rótulo, [(SKU, qtd)])] -> URL da tela de detalhe do caso."""
    from urllib.parse import urlencode

    q = [("title", title), ("rot", "any" if rotation == "any" else "upright")]
    for side, t, label, items in bins:
        q.append(("b", f"{side}|{t}|{label}|" + ";".join(_tok(i) for i in items)))
    return "/packing/case?" + urlencode(q)


def bin_card(arranger: "Arranger", type_id: int, label: str, items: list, seq: Optional[int] = None) -> dict:
    """Uma bin da tela do caso: arranjo, desenho 2D e dados do 3D. items = [(SKU, qtd[, reserva])]."""
    items = [i for i in items if (i[1] or 0) > 0 or (len(i) > 2 and i[2])]
    bt = arranger.types.get(type_id)
    arranger._load([i[0] for i in items])
    data = []
    for sku, q, *res in items:
        p = arranger.products.get(sku)
        d = (_f(p.height), _f(p.width), _f(p.length)) if p is not None and None not in (p.height, p.width, p.length) else None
        data.append((sku, q, d, res[0] if res else 0))
    arr = _arrange_bt(bt, data, arranger.rotation, seq)
    return {"label": label, "arr": arr, "svg": svg_mini(arr, 150) if arr.ok and (items or arr.parent) else "",
            "json": json.dumps(arr.as_json()) if arr.dims else "null", "empty": not items}


def section(sid: str, label: str, icon: str, bins: Optional[list], note: str = "", empty: str = "Nenhuma bin.") -> dict:
    """Bloco do acordeão da tela do caso. bins None = sem mudança."""
    return {"id": sid, "label": label, "icon": icon, "bins": bins or [], "unchanged": bins is None, "note": note,
            "empty": empty}


@router.get("/case")
def view_case(request: Request, title: str = "", rot: str = "upright", db: Session = Depends(get_db)):
    """Detalhe de um caso a partir das bins na URL (antes e depois), cada uma em 2D e 3D."""
    arranger = Arranger(db, rot)
    sides = {"before": [], "after": []}
    for raw in request.query_params.getlist("b"):
        parts = raw.split("|", 3)
        if len(parts) != 4 or parts[0] not in sides:
            continue
        side, t, label, items_raw = parts
        items = [x for x in map(_parse_tok, filter(None, items_raw.split(";"))) if x]
        m = re.search(r"#(\d+)", label)
        sides[side].append(bin_card(arranger, to_int(t) or 0, label, items, int(m.group(1)) if m else None))
    sections = [section("before", "Antes (hoje)", "bi-clock-history", sides["before"])]
    if "after" in {r.split("|", 1)[0] for r in request.query_params.getlist("b")}:
        sections.append(section("after", "Depois (sugestão)", "bi-arrow-right-circle", sides["after"]))
    return render(request, "packing/case.html", title=title, rot=arranger.rotation, sections=sections, scenarios=None)
