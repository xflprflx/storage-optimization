"""Estantes travadas: posição fixa (não sai do lugar no rearranjo, no editor da planta nem na planta gerada) e não
recompor (a recomposição das estruturas não mexe nela), com o motivo (cadastro de motivos)."""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session


def _scope(stmt, model, branch_id: int, wid: Optional[int]):
    stmt = stmt.where(model.branch_id == branch_id)
    return stmt.where(model.warehouse_id.is_(None) if wid is None else model.warehouse_id == wid)


def locks(db: Session, branch_id: int, wid: Optional[int]) -> dict[tuple, dict]:
    """{(corredor, estante): {"fixed", "no_recompose", "reason"}} das estantes travadas do depósito."""
    from models import BayLock

    out = {}
    for lk in db.scalars(_scope(select(BayLock), BayLock, branch_id, wid)):
        if lk.fixed_position or lk.no_recompose:
            out[(lk.aisle, lk.bay)] = {"fixed": lk.fixed_position, "no_recompose": lk.no_recompose,
                                       "reason": lk.reason.name if lk.reason is not None else ""}
    return out


def fixed_bays(db: Session, branch_id: int, wid: Optional[int]) -> dict[tuple, str]:
    """{(corredor, estante): motivo} das estantes com posição fixa."""
    return {k: v["reason"] for k, v in locks(db, branch_id, wid).items() if v["fixed"]}


def no_recompose_bays(db: Session, branch_id: int, wid: Optional[int]) -> dict[tuple, str]:
    return {k: v["reason"] for k, v in locks(db, branch_id, wid).items() if v["no_recompose"]}


def set_locks(db: Session, bays: Iterable[tuple], fixed: Optional[bool], no_recompose: Optional[bool],
              reason_id: Optional[int] = None, keep_reason: bool = False) -> int:
    """Marca (True), desmarca (False) ou mantém (None) cada trava nas estantes [(filial, armazém, corredor, estante)].
    Estante sem nenhuma trava sai do cadastro. Retorna quantas estantes mudaram."""
    from models import BayLock

    n = 0
    for bid, wid, aisle, bay in bays:
        lk = db.scalar(_scope(select(BayLock), BayLock, bid, wid).where(BayLock.aisle == aisle, BayLock.bay == bay))
        if lk is None:
            if not (fixed or no_recompose):
                continue
            lk = BayLock(branch_id=bid, warehouse_id=wid, aisle=aisle, bay=bay)
            db.add(lk)
        before = (lk.fixed_position, lk.no_recompose, lk.reason_id)
        if fixed is not None:
            lk.fixed_position = fixed
        if no_recompose is not None:
            lk.no_recompose = no_recompose
        if not keep_reason:
            lk.reason_id = reason_id
        if not (lk.fixed_position or lk.no_recompose):
            if lk.id is not None:
                db.delete(lk)
            else:
                db.expunge(lk)
            n += 1
            continue
        n += before != (lk.fixed_position, lk.no_recompose, lk.reason_id)
    db.flush()
    return n


def lock_rows(db: Session, branch_id: int, wid: Optional[int]) -> list:
    """Estantes travadas do depósito, para a tela (ordem natural)."""
    from models import BayLock
    from services.layout_service import natural_key

    rows = list(db.scalars(_scope(select(BayLock), BayLock, branch_id, wid)))
    return sorted(rows, key=lambda r: (natural_key(r.aisle), natural_key(r.bay)))


def _placed(g) -> dict:
    return {k: (round(p.x), round(p.y), round(p.w), round(p.d)) for k, p in g.placed.items()}


def moved_fixed(db: Session, branch_id: int, wid: Optional[int], new_data: dict) -> list[str]:
    """Estantes com posição fixa que a planta nova tira do lugar (comparada com a planta salva)."""
    from services.branch_layout import build, clean, get_layout, scope_bays

    fixed = fixed_bays(db, branch_id, wid)
    lay = get_layout(db, branch_id, wid)
    if not fixed or lay is None or not (lay.data or {}).get("rows"):
        return []
    bays = scope_bays(db, branch_id, wid)
    old, new = _placed(build(clean(lay.data), bays)), _placed(build(new_data, bays))
    return [f"{a}-{b}" for (a, b) in sorted(fixed) if (a, b) in old and new.get((a, b)) != old[(a, b)]]


def keep_fixed(db: Session, branch_id: int, wid: Optional[int], new_data: dict) -> tuple[dict, str]:
    """Planta gerada (automática, pela IA ou do projeto): as fileiras da planta salva que têm estante fixa ficam como
    estão, e as estantes delas saem das fileiras geradas. Retorna (planta, aviso)."""
    from services.branch_layout import build, clean, get_layout, row_bays, scope_bays

    fixed = fixed_bays(db, branch_id, wid)
    lay = get_layout(db, branch_id, wid)
    if not fixed or lay is None or not (lay.data or {}).get("rows"):
        return new_data, ""
    bays = scope_bays(db, branch_id, wid)
    old = clean(lay.data)
    g_old = build(old, bays)
    keep_idx = sorted({p.row for k, p in g_old.placed.items() if k in fixed})
    if not keep_idx:
        return new_data, ""
    kept = [old["rows"][i] for i in keep_idx]
    taken = {(p.bay.aisle, p.bay.bay) for p in g_old.placed.values() if p.row in keep_idx}
    rows = []
    for r in new_data.get("rows") or []:
        bs = row_bays(r, bays)
        if not any((b.aisle, b.bay) in taken for b in bs):
            rows.append(r)
            continue
        rest = [b.bay for b in bs if (b.aisle, b.bay) not in taken]
        if rest:
            rows.append({**r, "rev": False, "bays": rest})
    nxt = max([r.get("id") or 0 for r in rows] + [0]) + 1
    for r in kept:  # ids novos (o vizinho de frente escolhido à mão volta ao automático)
        rows.append({**r, "id": nxt, "pair": 0})
        nxt += 1
    data = clean({**new_data, "rows": rows,
                  "L": max(new_data.get("L") or 0, old["L"]), "W": max(new_data.get("W") or 0, old["W"])})
    from services.i18n import _

    return data, _("{n} fileira(s) com estante de posição fixa ficaram onde estavam; confira se a planta gerada não "
                   "passa por cima delas.", n=len(kept))
