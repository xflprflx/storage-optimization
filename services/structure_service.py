"""Estruturas de armazenagem aplicadas às estantes de depósitos existentes.

Aplicar uma estrutura a uma estante (Corredor + Estante):
  - grava o vínculo (BayStructure);
  - opcionalmente cria os níveis que faltam (1..N, no mesmo padrão de numeração dos níveis da estante);
  - define o vão de cada nível = vão útil da estrutura;
  - calcula "cabem X bins do tipo Y" em cada nível pela frente × profundidade do módulo e pela altura das bins
    (mesma regra do projeto de filial nova), para os tipos de bin inteiros com dimensões.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import BayStructure, BinType, StructureType
from services.level_service import ensure_level, fit_structure, matching_levels, set_capacity, spill_overflow
from services.site_planner import SiteParams, StructureSpec, level_capacity
from services.i18n import _

BayKey = tuple  # (branch_id, warehouse_id|None, aisle, bay)


def spec_of(st: StructureType) -> StructureSpec:
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    return StructureSpec(st.id, st.code, st.name, float(st.width), float(st.depth), float(st.height), int(st.levels),
                         float(st.level_height), f(st.max_load), f(st.cost), golden_from=f(st.golden_from),
                         golden_to=f(st.golden_to), base=float(st.first_level_cm or 0), kind=st.kind or "",
                         level_hs=st.vaos() if st.level_heights else None)


def _scope(stmt, model, branch_id, warehouse_id):
    stmt = stmt.where(model.branch_id == branch_id)
    return stmt.where(model.warehouse_id.is_(None) if warehouse_id is None else model.warehouse_id == warehouse_id)


def bay_structures(db: Session, branch_id: int, warehouse_id: Optional[int]) -> dict[tuple, StructureType]:
    """(corredor, estante) -> estrutura, no depósito."""
    rows = db.execute(_scope(select(BayStructure.aisle, BayStructure.bay, StructureType)
                             .join(StructureType, StructureType.id == BayStructure.structure_type_id),
                             BayStructure, branch_id, warehouse_id)).all()
    return {(a, b): st for a, b, st in rows}


def usage_counts(db: Session) -> dict[int, int]:
    """estrutura -> nº de estantes que a usam."""
    return dict(db.execute(select(BayStructure.structure_type_id, func.count()).group_by(BayStructure.structure_type_id)).all())


def matching_bays(db: Session, branch_id: int, warehouse_id: Optional[int], aisle_expr: str, bay_expr: str) -> list[BayKey]:
    keys = matching_levels(db, branch_id, warehouse_id, aisle_expr, bay_expr, "")
    return sorted({k[:4] for k in keys})


def capacities_for(st: StructureType, bin_types: Iterable[BinType], top_gap_cm: float = 5.0,
                   level_fill: float = 1.0, n: Optional[int] = None) -> dict[int, int]:
    """tipo de bin -> quantas cabem num nível da estrutura (só tipos inteiros com dimensões que cabem). `n` = o nível
    (vão próprio, quando a estrutura tem vão por nível)."""
    spec = spec_of(st)
    prm = SiteParams(top_gap_cm=top_gap_cm, level_fill=level_fill)
    from services.bin_optimizer import BinSpec

    out = {}
    for bt in bin_types:
        if bt.is_fraction or bt.height is None or bt.width is None or bt.length is None:
            continue
        k = level_capacity(BinSpec(bt.id, bt.code, float(bt.height), float(bt.width), float(bt.length)), spec, prm, None, n)
        if k > 0:
            out[bt.id] = k
    return out


def _level_codes(existing: list[str], n: int) -> list[str]:
    """Códigos 1..n no padrão da estante (ex.: '01'..'05' se os níveis atuais têm 2 dígitos)."""
    digits = [c for c in existing if c.isdigit()]
    pad = max((len(c) for c in digits), default=1)
    return [str(i).zfill(pad) for i in range(1, n + 1)]


def apply_structure(db: Session, bays: list[BayKey], st: StructureType, *, create_levels: bool = True,
                    set_heights: bool = True, set_caps: bool = True, replace_caps: bool = True,
                    top_gap_cm: float = 5.0, type_ids: Optional[set] = None, first_level_cm="keep") -> dict:
    """Aplica a estrutura às estantes. Com vão por nível na estrutura, cada nível (de baixo para cima) recebe o seu
    vão e a capacidade que cabe nele. `first_level_cm` = altura do 1º nível nessas estantes, quando foram montadas
    diferente da estrutura (None = a da estrutura; "keep" = não mexe). Retorna estatísticas."""
    from services.layout_service import level_order_key as natural_key  # níveis: ordem física

    stats = {"bays": 0, "levels_created": 0, "levels": 0, "caps": 0, "extra_levels": 0}
    bin_types = [bt for bt in db.scalars(select(BinType)) if type_ids is None or bt.id in type_ids]
    vaos = st.vaos()
    per_level = bool(st.level_heights)
    caps_n = {n: capacities_for(st, bin_types, top_gap_cm, n=n) for n in range(1, st.levels + 1)} \
        if set_caps and per_level else {}
    caps = capacities_for(st, bin_types, top_gap_cm) if set_caps and not per_level else {}
    idx_of: dict[tuple, int] = {}  # nível -> posição de baixo para cima na estante
    all_keys = []
    by_bay = defaultdict(list)
    for b, w, a, bay in bays:
        for k in matching_levels(db, b, w, a, bay, ""):
            if k[2] == a and k[3] == bay:
                by_bay[(b, w, a, bay)].append(k)
    for bk in bays:
        b, w, a, bay = bk
        link = db.scalar(_scope(select(BayStructure), BayStructure, b, w).where(BayStructure.aisle == a,
                                                                                BayStructure.bay == bay))
        if link is None:
            db.add(BayStructure(branch_id=b, warehouse_id=w, aisle=a, bay=bay, structure_type_id=st.id,
                                first_level_cm=None if first_level_cm == "keep" else first_level_cm))
        else:
            link.structure_type_id = st.id
            if first_level_cm != "keep":
                link.first_level_cm = first_level_cm
        stats["bays"] += 1
        keys = list(by_bay.get(bk, []))
        if create_levels:
            have = {k[4] for k in keys}
            for code in _level_codes(sorted(have), st.levels):
                if code not in have and not (code.lstrip("0") or "0") in {h.lstrip("0") or "0" for h in have}:
                    ensure_level(db, (b, w, a, bay, code))
                    keys.append((b, w, a, bay, code))
                    stats["levels_created"] += 1
        if len(keys) > st.levels:
            stats["extra_levels"] += len(keys) - st.levels
        stats["levels"] += len(keys)
        all_keys += keys
        keys.sort(key=lambda k: natural_key(k[4]))
        for i, k in enumerate(keys):
            idx_of[k] = min(i + 1, st.levels)
            sl = ensure_level(db, k)
            if set_heights:
                sl.height_cm = Decimal(str(vaos[min(i, len(vaos) - 1)]))
            if not sl.notes:
                sl.notes = _("Estrutura {code}", code=st.code)
    db.flush()
    if set_caps and all_keys:
        groups = {}
        for k in all_keys:  # níveis com a mesma capacidade (com vão por nível, por posição na estante)
            groups.setdefault(idx_of.get(k, 1) if per_level else 0, []).append(k)
        for g_n, g_keys in groups.items():
            cp = caps_n.get(g_n, {}) if per_level else caps
            for bt in bin_types:
                if bt.is_fraction:
                    continue
                n = cp.get(bt.id)
                if n or replace_caps:
                    set_capacity(db, g_keys, bt.id, n or None)
                    stats["caps"] += len(g_keys) if n else 0
    db.flush()
    stats["fit"] = fit_structure(db, all_keys) if all_keys else []
    stats["spill"] = spill_overflow(db, {k[:4] for k in all_keys}) if set_caps and all_keys else []
    return stats


def remove_structure(db: Session, bays: list[BayKey]) -> int:
    n = 0
    for b, w, a, bay in bays:
        link = db.scalar(_scope(select(BayStructure), BayStructure, b, w).where(BayStructure.aisle == a,
                                                                                BayStructure.bay == bay))
        if link is not None:
            db.delete(link)
            n += 1
    db.flush()
    return n
