"""Sincronização nas importações: excluir o que NÃO está no arquivo.

Regras de segurança (todas as importações):
- só roda se o arquivo não teve nenhuma linha com erro e teve ao menos uma linha válida — uma linha recusada
  poderia ser justamente um cadastro que "não está no arquivo" e seria apagado por engano;
- nunca exclui o que está em uso (endereço ocupado/com saldo, filial/armazém com endereços, tipo de bin usado,
  produto com saldo); o que fica é contado e listado com o motivo.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from models import (BinContent, BinType, Branch, LevelCapacity, LocationBin, Product, ShelfLevel, StockItem,
                    StorageLocation, Warehouse)
from models.entities import branch_warehouse
from services.i18n import N_, _

CHUNK = 900


def blocked_reason(error_count: int, rows_ok: int) -> Optional[str]:
    if error_count:
        return (_("Sincronização NÃO executada: o arquivo teve {error_count} linha(s) com erro. Corrija e importe de novo — nada foi excluído.", error_count=error_count))
    if not rows_ok:
        return _("Sincronização NÃO executada: nenhuma linha válida no arquivo — nada foi excluído.")
    return None


def _few(xs: list[str], n: int = 10) -> str:
    xs = sorted(xs)
    return ", ".join(xs[:n]) + (f"… (+{len(xs) - n})" if len(xs) > n else "")


def _chunks(ids: list) -> Iterable[list]:
    for i in range(0, len(ids), CHUNK):
        yield ids[i:i + CHUNK]


def _kept_note(what: str, kept: dict[str, list[str]]) -> list[str]:
    return [_("{what} fora do arquivo mantido(s) — {reason}: {few}.", what=what, reason=reason, few=_few(codes)) for reason, codes in kept.items() if codes]


# --------------------------------------------------------------------------- #
def sync_branches(db: Session, seen: set[int]) -> tuple[int, list[str]]:
    used = set(db.scalars(select(StorageLocation.branch_id).distinct())) | set(db.scalars(select(ShelfLevel.branch_id).distinct()))
    gone, kept = [], {N_("tem endereços ou níveis"): []}
    for b in db.scalars(select(Branch).where(Branch.id.not_in(seen) if seen else True)):
        if b.id in used:
            kept["tem endereços ou níveis"].append(b.code)
        else:
            gone.append(b.id)
    for ch in _chunks(gone):
        db.execute(delete(Branch).where(Branch.id.in_(ch)))
    return len(gone), _kept_note(_("Filial(is)"), kept)


def sync_warehouses(db: Session, seen: set[int], links: Optional[dict[int, set[int]]] = None) -> tuple[dict, list[str]]:
    """`links` {armazém: filiais do arquivo}: com a coluna Filiais, as associações ficam iguais às do arquivo
    (associação com endereços ou níveis naquela filial × armazém é mantida)."""
    used_pairs = set(db.execute(select(StorageLocation.branch_id, StorageLocation.warehouse_id).distinct()).all()) | \
        set(db.execute(select(ShelfLevel.branch_id, ShelfLevel.warehouse_id).distinct()).all())
    used = {w for _b, w in used_pairs if w is not None}
    gone, kept = [], {N_("tem endereços ou níveis"): []}
    for w in db.scalars(select(Warehouse).where(Warehouse.id.not_in(seen) if seen else True)):
        if w.id in used:
            kept["tem endereços ou níveis"].append(w.code)
        else:
            gone.append(w.id)
    for ch in _chunks(gone):
        db.execute(delete(Warehouse).where(Warehouse.id.in_(ch)))
    n_links, kept_links = 0, []
    if links:
        codes_b = dict(db.execute(select(Branch.id, Branch.code)).all())
        codes_w = dict(db.execute(select(Warehouse.id, Warehouse.code)).all())
        for bid, wid in db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)
                                   .where(branch_warehouse.c.warehouse_id.in_(list(links)))).all():
            if bid in links[wid]:
                continue
            if (bid, wid) in used_pairs:
                kept_links.append(f"{codes_b.get(bid)}×{codes_w.get(wid)}")
                continue
            db.execute(delete(branch_warehouse).where(branch_warehouse.c.branch_id == bid,
                                                      branch_warehouse.c.warehouse_id == wid))
            n_links += 1
    notes = _kept_note(_("Armazém(ns)"), kept)
    if kept_links:
        notes.append(_("Associação(ões) filial×armazém fora do arquivo mantida(s) — têm endereços ou níveis: {few}.", few=_few(kept_links)))
    return {N_("Armazéns excluídos"): len(gone), N_("Filial×Armazém desassociados"): n_links}, notes


def sync_bin_types(db: Session, seen_codes: set[str]) -> tuple[int, list[str]]:
    used = set(db.scalars(select(LocationBin.bin_type_id).distinct())) | set(db.scalars(select(BinContent.bin_type_id).distinct()))
    types = list(db.scalars(select(BinType)))
    cand = {t.id: t for t in types if t.code not in seen_codes}
    kept = {N_("usado em endereços"): [t.code for t in cand.values() if t.id in used]}
    cand = {i: t for i, t in cand.items() if i not in used}
    # bin pai só sai se todos os compartimentos dela também saírem
    changed = True
    kept["é bin pai de compartimento mantido"] = []
    while changed:
        changed = False
        for t in types:
            if t.parent_id in cand and t.id not in cand:
                kept["é bin pai de compartimento mantido"].append(cand.pop(t.parent_id).code)
                changed = True
    children = [i for i, t in cand.items() if t.parent_id is not None]
    parents = [i for i in cand if i not in children]
    for group in (children, parents):
        for ch in _chunks(group):
            db.execute(delete(LevelCapacity).where(LevelCapacity.bin_type_id.in_(ch)))
            db.execute(delete(BinType).where(BinType.id.in_(ch)))
    return len(cand), _kept_note(_("Tipo(s) de bin"), kept)


def sync_locations(db: Session, scopes: set[tuple], seen: set[int], drop_levels: bool) -> tuple[dict, list[str]]:
    """Endereços das filiais/armazéns que aparecem no arquivo e não estão nele: exclui os livres."""
    from services.level_service import drop_unused_levels, level_key_of
    from services.stock_service import has_items_expr

    out = {N_("Endereços excluídos"): 0, N_("Posições físicas (níveis) excluídas"): 0}
    kept: list[str] = []
    gone: list[StorageLocation] = []
    for bid, wid in scopes:
        stmt = select(StorageLocation).where(StorageLocation.branch_id == bid,
                                             StorageLocation.warehouse_id.is_(None) if wid is None
                                             else StorageLocation.warehouse_id == wid)
        busy = set(db.scalars(select(StorageLocation.id).where(
            StorageLocation.branch_id == bid,
            StorageLocation.warehouse_id.is_(None) if wid is None else StorageLocation.warehouse_id == wid,
            (StorageLocation.is_occupied.is_(True)) | has_items_expr(StorageLocation.id, correlate=StorageLocation))))
        for loc in db.scalars(stmt):
            if loc.id in seen:
                continue
            (kept.append(loc.address) if loc.id in busy else gone.append(loc))
    keys = [level_key_of(l) for l in gone]
    ids = [l.id for l in gone]
    for ch in _chunks(ids):
        db.execute(delete(StorageLocation).where(StorageLocation.id.in_(ch)))
    out["Endereços excluídos"] = len(ids)
    notes = []
    if kept:
        notes.append(_("{len} endereço(s) fora do arquivo mantido(s) — ocupados ou com item atrelado: {few}.", len=len(kept), few=_few(kept)))
    if drop_levels and keys:
        dropped, lv_kept = drop_unused_levels(db, keys)
        out["Posições físicas (níveis) excluídas"] = len(dropped)
        if lv_kept:
            notes.append(_("Nível(is) mantido(s): {few}.", few=_few(lv_kept)))
    return out, notes


def sync_levels(db: Session, scopes: set[tuple], cap_seen: set[tuple], height_seen: Optional[set[int]]) -> tuple[dict, list[str]]:
    """Níveis das filiais/armazéns do arquivo: remove as capacidades (nível × tipo) que o arquivo não informou e,
    com a coluna Vão mapeada, o vão dos níveis que o arquivo não informou. Níveis dedicados (automáticos) ficam."""
    from services.level_service import DEDICATED_NOTE

    out = {N_("Capacidades removidas"): 0, N_("Vãos removidos"): 0}
    for bid, wid in scopes:
        lv = list(db.scalars(select(ShelfLevel).where(
            ShelfLevel.branch_id == bid, ShelfLevel.warehouse_id.is_(None) if wid is None else ShelfLevel.warehouse_id == wid)))
        lv = [s for s in lv if not (s.notes or "").startswith(DEDICATED_NOTE)]
        ids = [s.id for s in lv]
        for ch in _chunks(ids):
            for cid, sl, t in db.execute(select(LevelCapacity.id, LevelCapacity.shelf_level_id, LevelCapacity.bin_type_id)
                                         .where(LevelCapacity.shelf_level_id.in_(ch))).all():
                if (sl, t) not in cap_seen:
                    db.execute(delete(LevelCapacity).where(LevelCapacity.id == cid))
                    out["Capacidades removidas"] += 1
        if height_seen is not None:
            for s in lv:
                if s.id not in height_seen and s.height_cm is not None:
                    s.height_cm = None
                    out["Vãos removidos"] += 1
    db.flush()
    return out, []


def sync_products(db: Session, seen: set[int]) -> tuple[int, list[str]]:
    with_stock = set(db.scalars(select(StockItem.product_id).where(StockItem.quantity != 0).distinct()))
    gone, kept = [], []
    for pid, sku in db.execute(select(Product.id, Product.sku)).all():
        if pid in seen:
            continue
        (kept.append(sku) if pid in with_stock else gone.append(pid))
    for ch in _chunks(gone):
        db.execute(delete(Product).where(Product.id.in_(ch)))
    notes = [_("{len} produto(s) fora do arquivo mantido(s) — têm saldo: {few}.", len=len(kept), few=_few(kept))] if kept else []
    return len(gone), notes


def count_all(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0
