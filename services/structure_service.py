"""Modelos de estrutura aplicados às estantes de depósitos existentes.

Aplicar uma estrutura a uma estante (Corredor + Estante):
  - grava o vínculo (BayStructure);
  - opcionalmente cria os níveis que faltam (1..N, no mesmo padrão de numeração dos níveis da estante);
  - define o vão de cada nível = vão útil da estrutura;
  - calcula "cabem X bins do tipo Y" em cada nível pela frente × profundidade do módulo e pela altura das bins
    (mesma regra do projeto de filial nova), para os tipos de bin inteiros com dimensões.

A capacidade calculada não é uma foto: `recalc_capacities` refaz a conta sempre que a estrutura, o vão de um nível ou
um tipo de bin mudam (e na inicialização). O limite manual (LevelCapacity.manual_bins) fica e vale quando é menor.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import BayStructure, BinType, LevelCapacity, StructureType
from services.level_service import (bay_levels, effective_capacity, ensure_level, fit_structure, matching_levels,
                                    spill_overflow)
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


CAP_DEFAULTS = {"top_gap": 5.0, "side_gap": 0.0, "fill": 1.0}


def cap_params_of(link: BayStructure) -> SiteParams:
    """Folgas do cálculo da capacidade na estante (as gravadas ao aplicar a estrutura ou ao gerar a filial)."""
    p = {**CAP_DEFAULTS, **{k: v for k, v in (link.cap_params or {}).items() if v is not None}}
    return SiteParams(top_gap_cm=float(p["top_gap"]), side_gap_cm=float(p["side_gap"]), level_fill=float(p["fill"]))


def recalc_capacities(db: Session, bays: Optional[Iterable[BayKey]] = None, *, structure_ids: Optional[Iterable[int]] = None,
                      clear_manual: bool = False, drop_redundant: bool = False) -> int:
    """Recalcula a capacidade dos níveis das estantes com estrutura (`bays` e/ou `structure_ids`; nenhum = todas).
    Cada nível usa o próprio vão (sem vão gravado, o da estrutura naquela posição) e as folgas da estante. Para cada
    tipo de bin inteiro com medidas: calculada = quantas cabem; vale o menor entre ela e o limite manual. Tipo com
    medidas que não cabe no nível perde a capacidade (e o limite); tipo sem medidas e compartimentos ficam como estão.
    `clear_manual` apaga os limites manuais; `drop_redundant` descarta os iguais ou acima do calculado (migração).
    Retorna quantas capacidades mudaram."""
    from dataclasses import replace

    from services.bin_optimizer import BinSpec
    from services.layout_service import level_order_key as natural_key

    only = {tuple(b) for b in bays} if bays is not None else None
    if only is not None and not only:
        return 0
    q = select(BayStructure, StructureType).join(StructureType, StructureType.id == BayStructure.structure_type_id)
    if structure_ids is not None:
        ids = list(structure_ids)
        if not ids:
            return 0
        q = q.where(BayStructure.structure_type_id.in_(ids))
    links = [(ln, st) for ln, st in db.execute(q).all()
             if only is None or (ln.branch_id, ln.warehouse_id, ln.aisle, ln.bay) in only]
    if not links:
        return 0
    specs = [BinSpec(bt.id, bt.code, float(bt.height), float(bt.width), float(bt.length))
             for bt in db.scalars(select(BinType))
             if not bt.is_fraction and bt.height is not None and bt.width is not None and bt.length is not None]
    cache: dict[tuple, dict[int, int]] = {}
    changed = 0
    for link, st in links:
        prm = cap_params_of(link)
        spec = spec_of(st)
        levels = sorted(bay_levels(db, link.branch_id, link.warehouse_id, link.aisle, link.bay),
                        key=lambda lv: natural_key(lv["level"]))
        for i, lv in enumerate(levels):
            sl = ensure_level(db, lv["key"])
            vao = float(sl.height_cm) if sl.height_cm is not None else spec.vao(min(i + 1, spec.levels))
            ck = (st.id, round(vao, 2), prm.top_gap_cm, prm.side_gap_cm, prm.level_fill)
            if ck not in cache:
                one = replace(spec, level_hs=[vao])
                cache[ck] = {t.id: n for t in specs if (n := level_capacity(t, one, prm, None, 1)) > 0}
            calc = cache[ck]
            if sl.id is None:
                db.flush()
            rows = {c.bin_type_id: c for c in db.scalars(select(LevelCapacity).where(LevelCapacity.shelf_level_id == sl.id))}
            for t in specs:
                row, n = rows.get(t.id), calc.get(t.id)
                if n is None:
                    if row is not None:  # não cabe no nível
                        db.delete(row)
                        changed += 1
                    continue
                manual = None if row is None or clear_manual else row.manual_bins
                if manual and drop_redundant and manual >= n:
                    manual = None
                eff = effective_capacity(n, manual)
                if row is None:
                    db.add(LevelCapacity(shelf_level_id=sl.id, bin_type_id=t.id, max_bins=eff, calc_bins=n))
                    changed += 1
                elif (row.calc_bins, row.manual_bins, row.max_bins) != (n, manual, eff):
                    row.calc_bins, row.manual_bins, row.max_bins = n, manual, eff
                    changed += 1
    db.flush()
    return changed


def bay_neighbors(db: Session, branch_id: int, warehouse_id: Optional[int], geo=None) -> tuple[list[tuple], str]:
    """Pares de estantes vizinhas (encostadas lado a lado), [((corredor, estante), (corredor, estante))], e a origem:
    "plan" = pela planta desenhada (mesma fileira, encostadas); "numbering" = estimado pela numeração do corredor (sem
    planta, uma letra por corredor: ímpares de um lado e pares do outro, a vizinha é o nº + 2); "" = nenhum."""
    import re

    if geo is None:
        from services.branch_layout import geometry

        try:
            geo = geometry(db, branch_id, warehouse_id)
        except Exception:  # noqa: BLE001  (planta inválida: estima pela numeração)
            geo = None
    if geo is not None and geo.placed:
        rows = geo.data.get("rows") or []
        by_row: dict[int, list] = defaultdict(list)
        for key, pb in geo.placed.items():
            by_row[pb.row].append((key, pb))
        out = []
        for ri, lst in by_row.items():
            vert = ri < len(rows) and rows[ri].get("orient") == "v"
            ln = (lambda pb: pb.d) if vert else (lambda pb: pb.w)  # comprimento ao longo da fileira
            lst.sort(key=lambda kp: kp[1].along)
            for (k1, p1), (k2, p2) in zip(lst, lst[1:]):
                if abs((p2.along - p1.along) - (ln(p1) + ln(p2)) / 2) < 1.0:  # encostadas (sem vão entre elas)
                    out.append((k1, k2))
        return out, "plan"
    keys = sorted({(a, b) for a, b in db.execute(_scope(select(BayStructure.aisle, BayStructure.bay), BayStructure,
                                                        branch_id, warehouse_id))})
    by_aisle: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for a, b in keys:
        if re.fullmatch(r"\d+", b or ""):
            by_aisle[a].append((int(b), b))
    out = []
    for a, lst in by_aisle.items():  # ímpares de um lado e pares do outro (uma letra por corredor): vizinha = nº + 2
        num = {n: b for n, b in lst}
        for n, b in sorted(lst):
            if n + 2 in num:
                out.append(((a, b), (a, num[n + 2])))
    return out, ("numbering" if out else "")


def _level_codes(existing: list[str], n: int) -> list[str]:
    """Códigos 1..n no padrão da estante (ex.: '01'..'05' se os níveis atuais têm 2 dígitos)."""
    digits = [c for c in existing if c.isdigit()]
    pad = max((len(c) for c in digits), default=1)
    return [str(i).zfill(pad) for i in range(1, n + 1)]


def apply_structure(db: Session, bays: list[BayKey], st: StructureType, *, create_levels: bool = True,
                    set_heights: bool = True, clear_manual: bool = False,
                    top_gap_cm: float = 5.0, first_level_cm="keep") -> dict:
    """Aplica a estrutura às estantes. Com vão por nível na estrutura, cada nível (de baixo para cima) recebe o seu
    vão, e a capacidade de cada nível é calculada (recalc_capacities, com a folga `top_gap_cm` gravada na estante).
    `clear_manual` apaga os limites manuais dessas estantes. `first_level_cm` = altura do 1º nível nessas estantes,
    quando foram montadas diferente da estrutura (None = a da estrutura; "keep" = não mexe). Retorna estatísticas."""
    from services.layout_service import level_order_key as natural_key  # níveis: ordem física

    stats = {"bays": 0, "levels_created": 0, "levels": 0, "caps": 0, "extra_levels": 0}
    vaos = st.vaos()
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
            link = BayStructure(branch_id=b, warehouse_id=w, aisle=a, bay=bay, structure_type_id=st.id,
                                first_level_cm=None if first_level_cm == "keep" else first_level_cm)
            db.add(link)
        else:
            link.structure_type_id = st.id
            if first_level_cm != "keep":
                link.first_level_cm = first_level_cm
        link.cap_params = {**(link.cap_params or {}), "top_gap": float(top_gap_cm)}
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
            sl = ensure_level(db, k)
            if set_heights:
                sl.height_cm = Decimal(str(vaos[min(i, len(vaos) - 1)]))
            if not sl.notes:
                sl.notes = _("Estrutura {code}", code=st.code)
    db.flush()
    stats["fit"] = fit_structure(db, all_keys) if all_keys else []
    recalc_capacities(db, bays, clear_manual=clear_manual)
    stats["caps"] = db.scalar(select(func.count()).select_from(LevelCapacity).where(
        LevelCapacity.shelf_level_id.in_([ensure_level(db, k).id for k in all_keys]),
        LevelCapacity.calc_bins.is_not(None))) if all_keys else 0
    stats["spill"] = spill_overflow(db, {k[:4] for k in all_keys}) if all_keys else []
    return stats


def remove_structure(db: Session, bays: list[BayKey]) -> int:
    """Tira a estrutura das estantes. A capacidade que valia nos níveis fica, agora como limite manual (sem a
    estrutura não há como recalculá-la)."""
    n = 0
    for b, w, a, bay in bays:
        link = db.scalar(_scope(select(BayStructure), BayStructure, b, w).where(BayStructure.aisle == a,
                                                                                BayStructure.bay == bay))
        if link is not None:
            ids = [lv["shelf_level_id"] for lv in bay_levels(db, b, w, a, bay) if lv["shelf_level_id"]]
            for c in db.scalars(select(LevelCapacity).where(LevelCapacity.shelf_level_id.in_(ids),
                                                            LevelCapacity.calc_bins.is_not(None))):
                c.manual_bins, c.calc_bins = c.max_bins, None
            db.delete(link)
            n += 1
    db.flush()
    return n
