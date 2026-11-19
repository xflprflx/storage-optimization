"""
Recomposição das estruturas de um depósito existente: reespaçar os níveis (menos vão, mais níveis — ou o contrário),
trocar a estrutura de uma estante por outra cadastrada, ou as duas, com as peças das estruturas.

Por estante (com estrutura cadastrada e níveis), as opções são:
  - manter;
  - reespaçar a própria estrutura: cada nível com bins fica com o menor vão que ainda as comporta (bin mais alta +
    folga); com a altura que sobra, níveis novos no vão da bin de referência (a mais comum na estante);
  - trocar por outra estrutura cadastrada de mesma frente, profundidade até a atual e altura até a maior do
    depósito: nos níveis da cadastrada ou reespaçada.
O que está guardado não muda de nível: o nível k continua com as bins dele (os endereços continuam válidos); níveis
de cima sem endereço nem bins podem sair. A base de cada nível respeita a espessura do nível e, se o tipo tiver,
o passo da furação dos montantes.

Ganho = bins a mais que cabem (do tipo de referência de cada estante), contando o espaço livre de cada nível.

Peças: a composição do tipo de estrutura (por módulo e por nível). Com `pieces="existing"`, só as peças das
estruturas do escopo (as liberadas por uma estante servem para outra); com "unlimited", o que faltar é comprado
(custo pelo custo da peça, quando informado).

Escolha (OR-Tools CP-SAT), lexicográfica: 1º maior ganho; 2º menor custo (ou menos peças compradas, sem custos);
3º menos estantes mexidas.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.i18n import _

TOP_GAP = 5.0  # folga acima da bin para pegar (cm), como no projeto de filial nova
EPS = 1e-6


def _f(v, d: float = 0.0) -> float:
    return float(v) if v is not None else d


def _step(v: float, beam: float, pitch: Optional[float]) -> float:
    """Vão ajustado para a base do nível seguinte cair num furo (múltiplo do passo)."""
    if not pitch:
        return round(v, 1)
    return round(math.ceil((v + beam) / pitch - EPS) * pitch - beam, 1)


class _Ctx:
    """Bins (medidas), estruturas e tipos de estrutura carregados uma vez."""

    def __init__(self, db: Session):
        from models import BinType, StructureType
        from services.bin_optimizer import BinSpec
        from services.site_planner import SiteParams
        from services.structure_kinds import composition, kind_of

        self.prm = SiteParams(top_gap_cm=TOP_GAP)
        self.types = {bt.id: bt for bt in db.scalars(select(BinType))}
        self.spec = {}
        for bt in self.types.values():
            if bt.height is not None and bt.width is not None and bt.length is not None:
                self.spec[bt.id] = BinSpec(bt.id, bt.code, float(bt.height), float(bt.width), float(bt.length))
        self.structs = list(db.scalars(select(StructureType)))
        self.kind = {st.id: kind_of(db, st) for st in self.structs}
        self.comp = {st.id: composition(self.kind[st.id]) for st in self.structs}

    def parent(self, tid: int):
        bt = self.types.get(tid)
        if bt is not None and bt.is_fraction and bt.parent_id in self.spec:
            return self.spec[bt.parent_id], bt.per_parent
        return None

    def height(self, tid: int) -> Optional[float]:
        s = self.spec.get(tid)
        if s is None and self.parent(tid):
            return self.parent(tid)[0].h
        return s.h if s is not None else None

    def beam(self, st, vaos: list[float], base: float) -> float:
        k = self.kind.get(st.id)
        if k is not None and k.beam_cm is not None:
            return float(k.beam_cm)
        return max(0.0, (float(st.height) - base - sum(vaos)) / max(1, len(vaos)))

    def pitch(self, st) -> Optional[float]:
        k = self.kind.get(st.id)
        return float(k.pitch_cm) if k is not None and k.pitch_cm else None

    def condition(self, st) -> str:
        k = self.kind.get(st.id)
        return (k.condition or "") if k is not None else ""

    def door(self, st):
        from services.doors import door_of

        return door_of(self.kind.get(st.id))

    def floor(self, st) -> bool:
        from services.site_planner import is_floor

        k = self.kind.get(st.id)
        return bool(k.floor) if k is not None else is_floor(st.kind)


def _spec(st, vaos: list[float], base: float):
    from services.site_planner import StructureSpec

    return StructureSpec(st.id, st.code, st.name, float(st.width), float(st.depth), float(st.height), len(vaos),
                         max(vaos), _f(st.max_load, None) if st.max_load is not None else None, None, base=base,
                         kind=st.kind or "", level_hs=list(vaos))


def _cap(ctx: _Ctx, spec, n: int, tid: int) -> int:
    from services.site_planner import level_capacity

    par = ctx.parent(tid)
    if par is None and tid not in ctx.spec:
        return 0
    return level_capacity(ctx.spec.get(tid) or par[0], spec, ctx.prm, par, n)


def _evaluate(ctx: _Ctx, st, vaos: list[float], base: float, content: list[dict], ref: Optional[int]) -> Optional[int]:
    """Bins livres (do tipo de referência) da configuração; None se o que está guardado não cabe nela."""
    spec = _spec(st, vaos, base)
    free = 0
    for i in range(len(vaos)):
        used = content[i] if i < len(content) else {}
        frac = 0.0
        for tid, n in used.items():
            if not n:
                continue
            c = _cap(ctx, spec, i + 1, tid)
            if c <= 0:
                return None
            frac += n / c
        if frac > 1 + EPS:
            return None
        if ref is not None:
            free += int(math.floor(_cap(ctx, spec, i + 1, ref) * max(0.0, 1 - frac) + EPS))
    return free


def _respace(ctx: _Ctx, st, base: float, content: list[dict], locked: int, ref: Optional[int],
             cur_vaos: list[float]) -> Optional[list[float]]:
    """Níveis 1..locked com o menor vão que guarda o conteúdo; níveis novos no vão da bin de referência."""
    beam = ctx.beam(st, st.vaos(), _f(st.first_level_cm))
    pitch = ctx.pitch(st)
    ref_h = ctx.height(ref) if ref is not None else None
    ref_need = (ref_h + TOP_GAP) if ref_h else None
    vaos = []
    for i in range(locked):
        hs = [ctx.height(t) for t, n in (content[i] if i < len(content) else {}).items() if n]
        hs = [h for h in hs if h]
        need = max(hs) + TOP_GAP if hs else (ref_need or (cur_vaos[i] if i < len(cur_vaos) else max(st.vaos())))
        vaos.append(_step(need, beam, pitch))
    H = float(st.height)
    used = base + sum(v + beam for v in vaos)
    if used > H + EPS:
        return None
    if ref_need:
        vref = _step(ref_need, beam, pitch)
        k = int(math.floor((H - used) / (vref + beam) + EPS)) if vref + beam > 0 else 0
        vaos += [vref] * max(0, k)
    if not vaos:
        return None
    left = H - (base + sum(v + beam for v in vaos))
    if left > EPS:
        vaos[-1] = round(vaos[-1] + left, 1)  # a sobra fica no nível de cima
    return vaos


def _bays(db: Session, branch_id: int, wid: Optional[int]) -> list[dict]:
    """Estantes do escopo com estrutura: níveis (vão atual), conteúdo de cada nível e o que impede tirar o nível."""
    from models import BayStructure, StructureType
    from services.level_service import bay_levels, usage_for

    links = db.execute(select(BayStructure, StructureType).join(StructureType, StructureType.id == BayStructure.structure_type_id)
                       .where(BayStructure.branch_id == branch_id,
                              BayStructure.warehouse_id.is_(None) if wid is None else BayStructure.warehouse_id == wid)).all()
    out = []
    for link, st in links:
        levels = bay_levels(db, branch_id, wid, link.aisle, link.bay)
        keys = [lv["key"] for lv in levels]
        use = usage_for(db, keys) if keys else {}
        base = _f(link.first_level_cm, _f(st.first_level_cm))
        sv = st.vaos()
        cur = []
        content = []
        locked = 0
        for i, lv in enumerate(levels):
            u = use.get(lv["key"])
            cur.append(u.height if u is not None and u.height else (sv[i] if i < len(sv) else sv[-1]))
            c = {t: n for t, n in (u.used if u is not None else {}).items() if n}
            content.append(c)
            if c or lv["has_locations"]:
                locked = i + 1
        out.append({"aisle": link.aisle, "bay": link.bay, "st": st, "base": base, "levels": levels, "vaos": cur,
                    "content": content, "locked": locked})
    return out


def propose(db: Session, branch_id: int, wid: Optional[int], pieces: str = "existing", min_gain: int = 1,
            allow_swap: bool = True, time_s: float = 20.0, progress: Optional[Callable] = None,
            cancelled: Optional[Callable] = None) -> dict:
    """Proposta de recomposição das estruturas do depósito (filial + armazém)."""
    from ortools.sat.python import cp_model

    from services.structure_kinds import pieces_for

    prog = progress or (lambda *_a: None)
    stop = cancelled or (lambda: False)
    prog(0, 4, _("Lendo estantes, níveis e bins"))
    ctx = _Ctx(db)
    bays = _bays(db, branch_id, wid)
    skipped = {"floor": 0, "no_levels": 0}
    hmax = max([float(b["st"].height) for b in bays] or [0.0])
    count_all: dict[int, int] = defaultdict(int)
    for b in bays:
        for c in b["content"]:
            for t, n in c.items():
                count_all[t] += n
    scope_ref = max((t for t in count_all if ctx.height(t)), key=lambda t: count_all[t], default=None)
    from services.branch_layout import door_hits, geometry

    try:
        geo = geometry(db, branch_id, wid)  # planta desenhada: confere a abertura das portas de giro na troca
    except Exception:  # noqa: BLE001  (sem planta válida: a porta não é conferida)
        geo = None
    door_blocked = 0
    prog(1, 4, _("Montando as opções de cada estante"))
    items = []  # por estante: {bay, cands: [{st, vaos, gain, pieces, label}]}
    for b in bays:
        st = b["st"]
        if ctx.floor(st):
            skipped["floor"] += 1
            continue
        if not b["levels"]:
            skipped["no_levels"] += 1
            continue
        cnt: dict[int, int] = defaultdict(int)
        for c in b["content"]:
            for t, n in c.items():
                cnt[t] += n
        ref = max((t for t in cnt if ctx.height(t)), key=lambda t: cnt[t], default=scope_ref)
        cur_free = _evaluate(ctx, st, b["vaos"], b["base"], b["content"], ref)
        if cur_free is None:  # o que está guardado já não cabe (vão menor que a bin): mede assim mesmo, sem conteúdo
            cur_free = _evaluate(ctx, st, b["vaos"], b["base"], [], ref) or 0
        keep = {"st": st, "vaos": b["vaos"], "base": b["base"], "gain": 0, "kind": "keep",
                "pieces": pieces_for(ctx.comp[st.id], len(b["vaos"]))}
        cands = [keep]
        seen = {(st.id, tuple(b["vaos"]))}
        targets = [(st, b["base"])]
        if allow_swap:
            for t in ctx.structs:
                if t.id == st.id or ctx.floor(t) or abs(float(t.width) - float(st.width)) > 1 + EPS:
                    continue
                if ctx.condition(t) != ctx.condition(st):
                    continue  # câmara refrigerada só troca por outra da mesma condição (e vice-versa)
                dr = ctx.door(t)
                if dr and dr.get("type") == "swing" and geo is not None:
                    pb = geo.placed.get((b["aisle"], b["bay"]))
                    if pb is not None:
                        import dataclasses

                        trial = dataclasses.replace(pb, bay=dataclasses.replace(pb.bay, door=dr))
                        if door_hits(geo, (b["aisle"], b["bay"]), trial):
                            door_blocked += 1
                            continue
                if float(t.depth) > float(st.depth) + 1 + EPS or float(t.height) > max(hmax, float(st.height)) + EPS:
                    continue
                targets.append((t, _f(t.first_level_cm)))
        for t, base in targets:
            opts = [_respace(ctx, t, base, b["content"], b["locked"], ref, b["vaos"])]
            if t.id != st.id:
                opts.append(t.vaos() if len(t.vaos()) >= b["locked"] else None)
            for vaos in opts:
                if not vaos or (t.id, tuple(vaos)) in seen:
                    continue
                seen.add((t.id, tuple(vaos)))
                free = _evaluate(ctx, t, vaos, base, b["content"], ref)
                if free is None or free - cur_free < min_gain:
                    continue
                cands.append({"st": t, "vaos": vaos, "base": base, "gain": free - cur_free,
                              "kind": "respace" if t.id == st.id else "swap",
                              "pieces": pieces_for(ctx.comp[t.id], len(vaos))})
        items.append({"bay": b, "ref": ref, "cands": cands})
    if stop():
        return {"error": _("Busca cancelada.")}
    # ---- escolha
    prog(2, 4, _("Escolhendo as recomposições (maior ganho)"))
    all_pieces = sorted({p for it in items for c in it["cands"] for p in c["pieces"]})
    have = {p: sum(it["cands"][0]["pieces"].get(p, 0.0) for it in items) for p in all_pieces}
    from models import StructurePiece

    pc = {p.id: p for p in db.scalars(select(StructurePiece))}
    cost_of = {p: float(pc[p].cost) if p in pc and pc[p].cost is not None else None for p in all_pieces}
    m = cp_model.CpModel()
    x = {}
    for i, it in enumerate(items):
        vs = [m.NewBoolVar(f"x{i}_{j}") for j in range(len(it["cands"]))]
        m.AddExactlyOne(vs)
        for j, v in enumerate(vs):
            x[i, j] = v
    S = 100  # peças em centésimos (composição com frações)
    need = {p: sum(int(round(c["pieces"].get(p, 0.0) * S)) * x[i, j] for i, it in enumerate(items)
                   for j, c in enumerate(it["cands"])) for p in all_pieces}
    buy = {}
    for p in all_pieces:
        hv = int(round(have[p] * S))
        if pieces == "existing":
            m.Add(need[p] <= hv)
        else:
            buy[p] = m.NewIntVar(0, 10 ** 9, f"buy{p}")
            m.Add(buy[p] >= need[p] - hv)
    gain = sum(c["gain"] * x[i, j] for i, it in enumerate(items) for j, c in enumerate(it["cands"]) if c["gain"])
    changed = sum(x[i, j] for i, it in enumerate(items) for j in range(1, len(it["cands"])))
    any_cost = any(cost_of[p] for p in all_pieces)
    money = sum(int(round((cost_of[p] or 0) * 100)) * buy[p] for p in buy) if any_cost else sum(buy.values())
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_s
    solver.parameters.num_workers = 8
    status = "optimal"

    def solve(obj, sense):
        nonlocal status
        if isinstance(obj, int):
            return True
        (m.Maximize if sense > 0 else m.Minimize)(obj)
        r = solver.Solve(m)
        if r not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return False
        if r != cp_model.OPTIMAL:
            status = "feasible"
        return True

    has_options = items and not isinstance(gain, int)  # alguma estante com opção além de manter
    ok = solve(gain, 1) if has_options else True
    if has_options and ok:
        m.Add(gain >= int(round(solver.ObjectiveValue())))
        if buy and not isinstance(money, int):
            prog(3, 4, _("Escolhendo as recomposições (menor custo)"))
            if solve(money, -1):
                m.Add(money <= int(round(solver.ObjectiveValue())))
        if not isinstance(changed, int):
            solve(changed, -1)
    pick = [0] * len(items)
    if has_options and ok:
        for i, it in enumerate(items):
            pick[i] = next(j for j in range(len(it["cands"])) if solver.Value(x[i, j]))
    # ---- resultado
    types = ctx.types
    changes = []
    used_p: dict[int, float] = defaultdict(float)
    for i, it in enumerate(items):
        c = it["cands"][pick[i]]
        for p, q in c["pieces"].items():
            used_p[p] += q
        if pick[i] == 0:
            continue
        b, k = it["bay"], it["cands"][0]
        delta = {p: c["pieces"].get(p, 0.0) - k["pieces"].get(p, 0.0) for p in set(c["pieces"]) | set(k["pieces"])}
        changes.append({"aisle": b["aisle"], "bay": b["bay"], "kind": c["kind"], "from_id": b["st"].id,
                        "from_code": b["st"].code, "to_id": c["st"].id, "to_code": c["st"].code,
                        "levels_from": len(k["vaos"]), "levels_to": len(c["vaos"]), "vaos_from": [round(v, 1) for v in k["vaos"]],
                        "vaos_to": c["vaos"], "base": c["base"], "gain": c["gain"],
                        "ref": types[it["ref"]].code if it["ref"] in types else "",
                        "pieces": {p: d for p, d in delta.items() if abs(d) > EPS}})
    plist = []
    total_cost = 0.0 if any_cost else None
    for p in all_pieces:
        hv, nd = have[p], used_p.get(p, 0.0)
        bought = max(0.0, nd - hv)
        cst = bought * cost_of[p] if cost_of[p] is not None else None
        if cst is not None and total_cost is not None:
            total_cost += cst
        if abs(nd - hv) > EPS or bought:
            plist.append({"id": p, "code": pc[p].code if p in pc else str(p), "name": pc[p].name if p in pc else "",
                          "have": hv, "need": nd, "freed": max(0.0, hv - nd), "bought": bought,
                          "unit": cost_of[p], "cost": cst})
    changes.sort(key=lambda c: (-c["gain"], c["aisle"], c["bay"]))
    prog(4, 4, _("Pronto"))
    return {"branch_id": branch_id, "warehouse_id": wid, "pieces_mode": pieces, "min_gain": min_gain,
            "bays": len(items), "skipped": skipped, "changes": changes, "gain": sum(c["gain"] for c in changes),
            "levels_added": sum(c["levels_to"] - c["levels_from"] for c in changes), "pieces": plist,
            "cost": total_cost if any(p["bought"] for p in plist) else (0.0 if any_cost else None),
            "status": status if ok else "infeasible", "no_structure": _count_no_structure(db, branch_id, wid),
            "door_blocked": door_blocked, "door_checked": geo is not None}


def _count_no_structure(db: Session, branch_id: int, wid: Optional[int]) -> int:
    from models import BayStructure, StorageLocation

    bays = {(a, b) for a, b in db.execute(select(StorageLocation.aisle, StorageLocation.bay).distinct()
                                          .where(StorageLocation.branch_id == branch_id,
                                                 StorageLocation.warehouse_id.is_(None) if wid is None
                                                 else StorageLocation.warehouse_id == wid,
                                                 StorageLocation.bay != ""))}
    linked = {(a, b) for a, b in db.execute(select(BayStructure.aisle, BayStructure.bay)
                                            .where(BayStructure.branch_id == branch_id,
                                                   BayStructure.warehouse_id.is_(None) if wid is None
                                                   else BayStructure.warehouse_id == wid))}
    return len(bays - linked)


# --------------------------------------------------------------------------- #
# Aplicar e desfazer
# --------------------------------------------------------------------------- #
def _snapshot_bay(db: Session, branch_id: int, wid: Optional[int], aisle: str, bay: str) -> dict:
    from models import BayStructure, LevelCapacity
    from services.level_service import bay_levels, get_level

    link = db.scalar(select(BayStructure).where(BayStructure.branch_id == branch_id, BayStructure.aisle == aisle,
                                                BayStructure.bay == bay,
                                                BayStructure.warehouse_id.is_(None) if wid is None
                                                else BayStructure.warehouse_id == wid))
    levels = []
    for lv in bay_levels(db, branch_id, wid, aisle, bay):
        sl = get_level(db, lv["key"])
        caps = {}
        if sl is not None:
            caps = {str(t): n for t, n in db.execute(select(LevelCapacity.bin_type_id, LevelCapacity.max_bins)
                                                     .where(LevelCapacity.shelf_level_id == sl.id))}
        levels.append({"level": lv["level"], "exists": sl is not None,
                       "height": float(sl.height_cm) if sl is not None and sl.height_cm is not None else None,
                       "notes": sl.notes if sl is not None else "", "caps": caps})
    return {"aisle": aisle, "bay": bay, "structure_id": link.structure_type_id if link else None, "levels": levels}


def apply(db: Session, res: dict, idx: Optional[list[int]] = None) -> dict:
    """Grava as recomposições escolhidas (todas ou as de `idx`): estrutura da estante, vão de cada nível, níveis
    novos (reservados, sem endereço) e de cima removidos (sem endereço nem bins), e a capacidade de cada nível.
    Guarda o estado anterior para desfazer. Retorna {bays, levels_created, levels_removed, log_id}."""
    from decimal import Decimal

    from models import BayStructure, BinType, RecompositionLog, StructureType
    from services.level_service import bay_levels, drop_unused_levels, ensure_level, set_capacity
    from services.structure_service import _level_codes

    b_id, wid = res["branch_id"], res["warehouse_id"]
    chosen = [c for i, c in enumerate(res["changes"]) if idx is None or i in idx]
    snap = [_snapshot_bay(db, b_id, wid, c["aisle"], c["bay"]) for c in chosen]
    ctx = _Ctx(db)
    bins = [bt for bt in db.scalars(select(BinType)) if not bt.is_fraction and bt.id in ctx.spec]
    stats = {"bays": 0, "levels_created": 0, "levels_removed": 0}
    created: dict[str, list[str]] = {}
    for c in chosen:
        st = db.get(StructureType, c["to_id"])
        if st is None:
            continue
        link = db.scalar(select(BayStructure).where(BayStructure.branch_id == b_id, BayStructure.aisle == c["aisle"],
                                                    BayStructure.bay == c["bay"],
                                                    BayStructure.warehouse_id.is_(None) if wid is None
                                                    else BayStructure.warehouse_id == wid))
        if link is None:
            continue
        if link.structure_type_id != st.id:
            link.structure_type_id = st.id
            link.first_level_cm = None
        levels = bay_levels(db, b_id, wid, c["aisle"], c["bay"])
        keys = [lv["key"] for lv in levels]
        codes = [lv["level"] for lv in levels]
        n = len(c["vaos_to"])
        if n > len(keys):
            for code in _level_codes(codes, n)[len(keys):]:
                k = (b_id, wid, c["aisle"], c["bay"], code)
                ensure_level(db, k)
                keys.append(k)
                created.setdefault(f"{c['aisle']}|{c['bay']}", []).append(code)
                stats["levels_created"] += 1
        spec = _spec(st, c["vaos_to"], c["base"])
        for i, k in enumerate(keys[:n]):
            sl = ensure_level(db, k)
            sl.height_cm = Decimal(str(c["vaos_to"][i]))
            if not sl.notes:
                sl.notes = _("Estrutura {code}", code=st.code)
            for bt in bins:
                cap = _cap(ctx, spec, i + 1, bt.id)
                set_capacity(db, [k], bt.id, cap or None)
        if len(keys) > n:
            dropped, _kept = drop_unused_levels(db, keys[n:])
            stats["levels_removed"] += len(dropped)
        stats["bays"] += 1
    db.flush()
    log = RecompositionLog(branch_id=b_id, warehouse_id=wid, data={"before": snap, "created": created,
                                                                   "summary": {**stats, "gain": sum(c["gain"] for c in chosen)}})
    db.add(log)
    db.commit()
    stats["log_id"] = log.id
    return stats


def last_log(db: Session, branch_id: int, wid: Optional[int]):
    from models import RecompositionLog

    return db.scalar(select(RecompositionLog).where(RecompositionLog.branch_id == branch_id,
                                                    RecompositionLog.warehouse_id.is_(None) if wid is None
                                                    else RecompositionLog.warehouse_id == wid,
                                                    RecompositionLog.undone.is_(False))
                     .order_by(RecompositionLog.id.desc()))


def undo(db: Session, log) -> int:
    """Volta as estantes da recomposição ao estado anterior (estrutura, vãos e capacidades) e tira os níveis criados
    que continuam sem endereço nem bins. Retorna o nº de estantes."""
    from decimal import Decimal

    from models import BayStructure, LevelCapacity
    from services.level_service import drop_unused_levels, ensure_level, get_level, set_capacity

    b_id, wid = log.branch_id, log.warehouse_id
    for snap in log.data.get("before") or []:
        a, bay = snap["aisle"], snap["bay"]
        link = db.scalar(select(BayStructure).where(BayStructure.branch_id == b_id, BayStructure.aisle == a,
                                                    BayStructure.bay == bay,
                                                    BayStructure.warehouse_id.is_(None) if wid is None
                                                    else BayStructure.warehouse_id == wid))
        if link is not None and snap["structure_id"]:
            link.structure_type_id = snap["structure_id"]
        for lv in snap["levels"]:
            k = (b_id, wid, a, bay, lv["level"])
            if not lv["exists"]:
                continue
            sl = ensure_level(db, k)
            sl.height_cm = Decimal(str(lv["height"])) if lv["height"] is not None else None
            sl.notes = lv["notes"]
            db.query(LevelCapacity).filter(LevelCapacity.shelf_level_id == sl.id).delete()
            db.flush()
            for t, n in lv["caps"].items():
                set_capacity(db, [k], int(t), n)
        for code in (log.data.get("created") or {}).get(f"{a}|{bay}", []):
            if get_level(db, (b_id, wid, a, bay, code)) is not None:
                drop_unused_levels(db, [(b_id, wid, a, bay, code)])
    log.undone = True
    db.commit()
    return len(log.data.get("before") or [])
