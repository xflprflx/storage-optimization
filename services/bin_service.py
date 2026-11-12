"""
Bins dos endereços: atribuição individual, atribuição em massa e cálculo de capacidade.

Um endereço pode ter várias bins, inclusive de tipos diferentes. Cada linha de
LocationBin guarda (endereço, tipo de bin, quantidade de bins daquele tipo).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from models import BinType, LocationBin
from services.i18n import N_, _

CHUNK = 900
MAX_BINS_PER_TYPE = 100_000

BULK_MODES = {
    "add": N_("Adicionar no próprio nível (soma à quantidade do mesmo tipo)"),
    "set": N_("Definir quantidade deste tipo (mantém os outros tipos)"),
    "replace": N_("Substituir todas as bins do endereço por este tipo"),
    "remove": N_("Remover este tipo dos endereços"),
    "clear": N_("Remover TODAS as bins dos endereços"),
}


@dataclass
class Capacity:
    bins: int = 0
    weight: Optional[Decimal] = None  # kg
    volume: Optional[Decimal] = None  # litros

    def add(self, bin_type: BinType, qty: int) -> None:
        self.bins += qty
        if bin_type.max_weight is not None:
            self.weight = (self.weight or Decimal("0")) + bin_type.max_weight * qty
        vol = bin_type.volume
        if vol is not None:
            self.volume = (self.volume or Decimal("0")) + vol * qty


def capacity_of(location_bins: Iterable[LocationBin]) -> Capacity:
    cap = Capacity()
    for lb in location_bins:
        cap.add(lb.bin_type, lb.quantity)
    return cap


def parse_bin_rows(type_ids: list, quantities: list, levels: Optional[list] = None):
    """Converte as linhas do formulário em [(bin_type_id, qtd, shelf_level_id|None)].

    `levels` (opcional) traz, por linha, o nível onde as bins ficam ('' = próprio nível).
    Linhas repetidas (mesmo tipo e nível) são somadas.
    """
    levels = list(levels or [])
    totals: dict[tuple, int] = {}
    for i, (raw_type, raw_qty) in enumerate(zip(type_ids, quantities)):
        raw_type = str(raw_type or "").strip()
        raw_qty = str(raw_qty or "").strip()
        raw_lvl = str(levels[i] if i < len(levels) else "").strip()
        if not raw_type:
            continue
        try:
            type_id = int(raw_type)
        except ValueError:
            return [], _("Tipo de bin inválido.")
        try:
            qty = int(raw_qty or "1")
        except ValueError:
            return [], _("Quantidade de bins deve ser um número inteiro.")
        if qty < 1 or qty > MAX_BINS_PER_TYPE:
            return [], _("Quantidade de bins deve estar entre 1 e {MAX_BINS_PER_TYPE}.", MAX_BINS_PER_TYPE=f"{MAX_BINS_PER_TYPE:,}")
        lvl = None
        if raw_lvl:
            try:
                lvl = int(raw_lvl)
            except ValueError:
                return [], _("Nível inválido.")
        totals[(type_id, lvl)] = totals.get((type_id, lvl), 0) + qty
    return [(t, q, lvl) for (t, lvl), q in totals.items()], None


def validate_types(db: Session, type_ids: Iterable[int]) -> Optional[str]:
    ids = set(type_ids)
    if not ids:
        return None
    found = set(db.scalars(select(BinType.id).where(BinType.id.in_(ids))))
    if found != ids:
        return _("Um ou mais tipos de bin não existem mais.")
    return None


def set_location_bins(db: Session, location_id: int, items: list[tuple]) -> None:
    """Substitui todas as bins de um endereço pela lista informada.
    Itens: (tipo, qtd) -> no próprio nível; ou (tipo, qtd, shelf_level_id|None)."""
    db.execute(delete(LocationBin).where(LocationBin.location_id == location_id))
    rows: dict[tuple, int] = {}
    for it in items:
        t, q = it[0], it[1]
        lvl = it[2] if len(it) > 2 else None
        if q > 0:
            rows[(t, lvl)] = rows.get((t, lvl), 0) + q
    if rows:
        db.execute(insert(LocationBin), [
            {"location_id": location_id, "bin_type_id": t, "quantity": q, "shelf_level_id": lvl}
            for (t, lvl), q in rows.items()
        ])
    from services.bin_content_service import refresh_contents

    db.flush()
    refresh_contents(db, [location_id])  # conteúdo das bins acompanha as bins


@dataclass
class BulkResult:
    locations: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_removed: int = 0


def bulk_assign(db: Session, location_ids: list[int], mode: str, bin_type_id: Optional[int], qty: int) -> BulkResult:
    """Atribui bins a vários endereços de uma vez (sem commit)."""
    if mode not in BULK_MODES:
        raise ValueError(_("Modo de atribuição inválido."))
    if mode != "clear" and not bin_type_id:
        raise ValueError(_("Selecione o tipo de bin."))
    if mode in ("add", "set", "replace") and not (1 <= qty <= MAX_BINS_PER_TYPE):
        raise ValueError(_("Quantidade de bins deve estar entre 1 e {MAX_BINS_PER_TYPE}.", MAX_BINS_PER_TYPE=f"{MAX_BINS_PER_TYPE:,}"))
    res = BulkResult(locations=len(location_ids))
    ids = sorted(set(location_ids))
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        if mode == "clear":
            res.rows_removed += db.execute(delete(LocationBin).where(LocationBin.location_id.in_(chunk))).rowcount or 0
            continue
        if mode == "remove":
            res.rows_removed += db.execute(delete(LocationBin).where(
                LocationBin.location_id.in_(chunk), LocationBin.bin_type_id == bin_type_id)).rowcount or 0
            continue
        if mode == "replace":
            res.rows_removed += db.execute(delete(LocationBin).where(
                LocationBin.location_id.in_(chunk),
                (LocationBin.bin_type_id != bin_type_id) | LocationBin.shelf_level_id.is_not(None),
            )).rowcount or 0
        if mode == "set":
            # "Definir quantidade deste tipo": vale para o endereço todo -> remove o tipo dos níveis extras
            res.rows_removed += db.execute(delete(LocationBin).where(
                LocationBin.location_id.in_(chunk), LocationBin.bin_type_id == bin_type_id,
                LocationBin.shelf_level_id.is_not(None))).rowcount or 0
        existing = {
            lb.location_id: lb for lb in db.scalars(select(LocationBin).where(
                LocationBin.location_id.in_(chunk), LocationBin.bin_type_id == bin_type_id,
                LocationBin.shelf_level_id.is_(None)))
        }
        new_rows = []
        for loc_id in chunk:
            lb = existing.get(loc_id)
            if lb is None:
                new_rows.append({"location_id": loc_id, "bin_type_id": bin_type_id, "quantity": qty})
            else:
                lb.quantity = min(lb.quantity + qty, MAX_BINS_PER_TYPE) if mode == "add" else qty
                res.rows_updated += 1
        if new_rows:
            db.execute(insert(LocationBin), new_rows)
            res.rows_created += len(new_rows)
        db.flush()
    from services.bin_content_service import refresh_contents

    db.flush()
    refresh_contents(db, location_ids)  # conteúdo das bins acompanha as bins
    return res
