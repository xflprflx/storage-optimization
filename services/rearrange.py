"""
Proposta de rearranjo da disposição das estruturas, medida pelo trajeto dos pedidos do histórico.

Projeto de filial nova (`propose_site`): o planejador já escolhe a disposição que COMPORTA MAIS. A proposta testa
variações da planta — orientação das fileiras e quantidade de corredores transversais (com zonas: a orientação e os
transversais de todas as zonas) — rodando o projeto inteiro em cada uma (estruturas, bins e posição dos SKUs pela
saída) e mede, além da capacidade, o trajeto: os pedidos da aba Pedidos percorridos na planta (rota otimizada) ou,
sem pedidos, a caminhada média por saída ponderada pela popularidade.

Filial existente com planta desenhada (`propose_branch`): as estantes já existem, então as variações são obras
possíveis na planta atual, com as mesmas estantes:
  - passagem transversal: abre um vão de `gap` cm atravessando as fileiras paralelas (as estantes depois do vão
    andam para a frente) — só quando cabe no imóvel, sem bater em outra fileira, obstáculo ou na referência;
  - troca de corredores: os corredores com mais saídas vão para as posições mais perto da referência (só entre
    corredores de mesmo formato: mesma orientação, mesmo número de fileiras e mesmas medidas — as estantes mudam de
    lugar com o que está nelas);
  - as duas juntas.
Cada variação é medida com os mesmos pedidos e os SKUs nas mesmas estantes (hoje ou na proposta do otimizador).
"""
from __future__ import annotations

import copy
import math
from typing import Callable, Optional

from services.i18n import _, fmt_num

MAX_ORDERS = 2000  # pedidos da amostra usados em cada variação (amostra espaçada, determinística)
PASSAGE_CM = 200.0  # largura padrão da passagem transversal aberta na planta existente
SITE_CROSS = (0, 1, 2, 3)  # corredores transversais testados no projeto de filial nova


def _subsample(sample: list, n: int = MAX_ORDERS) -> list:
    if len(sample) <= n:
        return list(sample)
    step = len(sample) / n
    return [sample[int(i * step)] for i in range(n)]


def _walk(sim: dict) -> dict:
    """Indicadores de trajeto de uma simulação (m por pedido)."""
    if not sim or not sim.get("orders"):
        return {}
    return {"orders": sim["orders"], "opt_avg": sim.get("opt_avg"), "list_avg": sim.get("list_avg"),
            "opt_total": sim.get("opt_total")}


def rank(variants: list[dict], key: str) -> None:
    """Ordena: sem falta de espaço primeiro (menos bins sem lugar), depois o menor trajeto; marca a melhor e a
    diferença de cada uma para a atual."""
    cur = next((v for v in variants if v.get("current")), None)
    base = (cur or {}).get(key)
    for v in variants:
        v["delta_pct"] = ((v[key] - base) / base * 100) if v.get(key) is not None and base else None
    variants.sort(key=lambda v: (bool(v.get("error")), v.get("shortage") or 0,
                                 v[key] if v.get(key) is not None else math.inf, not v.get("current")))
    ok = [v for v in variants if not v.get("error") and v.get(key) is not None]
    if ok:
        ok[0]["best"] = True


# --------------------------------------------------------------------------- #
# Projeto de filial nova
# --------------------------------------------------------------------------- #
def site_variants(cfg: dict) -> list[dict]:
    """Variações da planta do projeto: [{"label", "changes": {...}, "current"}]. A atual vai primeiro."""
    from services.site_planner import ORIENTATIONS, ZONE_ORIENTATIONS

    bc = cfg["building"]
    zones = bc.get("zones") or []
    out = [{"label": _("Disposição atual"), "changes": {}, "current": True}]
    if zones:
        positioned = any(str(z.get("x_m") or "").strip() and str(z.get("y_m") or "").strip() for z in zones)
        oris = sorted({z.get("orientation") or "along" for z in zones})
        cur_ori = oris[0] if len(oris) == 1 else None
        cur_cross = {str(z.get("cross_aisles") or "0").strip() or "0" for z in zones}
        cur_cross = int(next(iter(cur_cross))) if len(cur_cross) == 1 and next(iter(cur_cross)).isdigit() else None
        for ori in ZONE_ORIENTATIONS:
            for n in SITE_CROSS:
                if ori == cur_ori and n == cur_cross:
                    continue
                if positioned and ori != (cur_ori or ori):
                    continue  # zonas posicionadas por X/Y: só os transversais (a orientação muda o desenho da zona)
                out.append({"label": _("Zonas: {ori}, {n} transversal(is)", ori=_(ZONE_ORIENTATIONS[ori]).lower(), n=n),
                            "changes": {"zone_orientation": ori, "zone_cross": n}})
        return out
    cur_ori = bc.get("orientation") or "auto"
    try:
        cur_cross = int(float(str(bc.get("cross_aisles") or "0").replace(",", ".")))
    except ValueError:
        cur_cross = 0
    for ori in ("length", "width"):
        for n in SITE_CROSS:
            if ori == cur_ori and n == cur_cross:
                continue
            out.append({"label": _("{ori}, {n} transversal(is)", ori=_(ORIENTATIONS[ori]), n=n),
                        "changes": {"orientation": ori, "cross_aisles": n}})
    return out


def apply_site_changes(cfg: dict, changes: dict) -> dict:
    """Configuração do projeto com a variação aplicada (cópia)."""
    c2 = copy.deepcopy(cfg)
    b = c2["building"]
    if "orientation" in changes:
        b["orientation"] = changes["orientation"]
    if "cross_aisles" in changes:
        b["cross_aisles"] = str(changes["cross_aisles"])
    for z in b.get("zones") or []:
        if "zone_orientation" in changes:
            z["orientation"] = changes["zone_orientation"]
        if "zone_cross" in changes:
            z["cross_aisles"] = str(changes["zone_cross"])
    return c2


def _site_walk(plan, sample: Optional[list]) -> dict:
    """Trajeto do plano: pedidos da amostra (rota otimizada) ou a caminhada média por saída do slotting."""
    out = {"avg_dist": (plan.slotting or {}).get("avg_dist")}
    if sample and plan.modules:
        from services.route_sim import plan_nodes, ref_node, simulate
        from services.walk_net import plan_net

        nodes, ref = plan_nodes(plan)
        start = ref_node(ref)
        plan_net(plan, nodes, start)
        out.update(_walk(simulate(sample, nodes, start)))
    return out


def propose_site(db, cfg: dict, per_run_s: float = 6.0, progress: Optional[Callable] = None,
                 cancelled: Optional[Callable] = None) -> dict:
    """Roda o projeto em cada variação da planta e compara capacidade e trajeto. Os SKUs ficam pela saída (slotting
    ligado em todas, quando há dados de saída), para a comparação ser justa."""
    from services.site_planner import _slots
    from services.site_service import _probe_cfg, popularity, run_project

    prog = progress or (lambda *_a: None)
    stop = cancelled or (lambda: False)
    variants = site_variants(cfg)
    sample = _subsample((cfg.get("orders") or {}).get("sample") or [])
    has_pop = bool(popularity(cfg, [])[0]) or bool(sample)
    total = len(variants)
    chosen = None  # orientação "automática": a variação igual à que o planejador escolheu é a própria atual
    for i, v in enumerate(variants):
        if stop():
            break
        ch = v["changes"]
        if chosen and ch.get("orientation") == chosen[0] and ch.get("cross_aisles") == chosen[1]:
            total -= 1
            continue
        prog(i, total, _("Testando: {label}", label=v["label"]))
        c2 = apply_site_changes(cfg, v["changes"])
        if has_pop:
            c2["slotting"] = {**(c2.get("slotting") or {}), "on": "1"}
        plan, errors = run_project(db, _probe_cfg(c2, per_run_s))
        if errors or plan is None or plan.errors or not plan.modules:
            v["error"] = " ".join(errors or (plan.errors if plan else []) or [_("Nenhuma estrutura coube.")])
            v["done"] = True
            continue
        active = [s.struct for s in plan.structs]
        v.update(modules=plan.total_modules, shortage=plan.shortage, slots=_slots(plan.floor, active) if active else 0,
                 rows=len(plan.floor.rows), row_m=round(plan.floor.total_row_m, 1), cost=plan.total_cost,
                 done=True, **_site_walk(plan, sample))
        v["plan"] = plan
        if v.get("current") and (cfg["building"].get("orientation") or "auto") == "auto" and not cfg["building"].get("zones"):
            zs = getattr(plan.floor, "zones", None) or []
            try:
                cross = int(float(str(cfg["building"].get("cross_aisles") or "0").replace(",", ".")))
            except ValueError:
                cross = 0
            if zs and getattr(zs[0], "along", None) in ("length", "width"):
                chosen = (zs[0].along, cross)
    variants = [v for v in variants if v.get("done")]
    key = "opt_avg" if sample else "avg_dist"
    rank(variants, key)
    prog(total, total, _("Pronto"))
    return {"variants": variants, "key": key, "orders": len(sample), "partial": len(variants) < total,
            "slotting_forced": has_pop and not (cfg.get("slotting") or {}).get("on")}


# --------------------------------------------------------------------------- #
# Filial existente (planta desenhada)
# --------------------------------------------------------------------------- #
def moved_any(why: str) -> bool:
    """Motivo de recusa que vale mostrar (não cabe); "nenhuma fileira atravessa" é só uma posição sem fileiras."""
    return why != _("nenhuma fileira atravessa essa posição")


def _row_parts(g, ri: int) -> list:
    """Estantes da fileira `ri`, na ordem da planta (ao longo de X ou Y, crescente)."""
    r = g.data["rows"][ri]
    ps = [p for p in g.placed.values() if p.row == ri]
    return sorted(ps, key=lambda p: p.x if r["orient"] == "h" else p.y)


def _overlap(a, b, eps: float = 1.0) -> bool:
    return a[0] < b[0] + b[2] - eps and b[0] < a[0] + a[2] - eps and a[1] < b[1] + b[3] - eps and b[1] < a[1] + a[3] - eps


def add_passages(data: dict, g, orient: str, cuts: list[float], gap: float = PASSAGE_CM,
                 fixed: Optional[dict] = None) -> tuple[Optional[dict], str]:
    """Abre passagens transversais de `gap` cm nas posições `cuts` (cm, ao longo de X para fileiras "h", de Y para
    "v"), atravessando todas as fileiras dessa orientação. Cada fileira cortada vira duas (lista fixa de estantes);
    o que fica depois do corte anda para a frente. (nova planta, "") ou (None, motivo)."""
    rows = copy.deepcopy(data["rows"])
    L, W = data["L"], data["W"]
    along_max = L if orient == "h" else W
    nxt = max([r["id"] for r in rows] + [0]) + 1
    out_rows = []
    moved: set[int] = set()
    for ri, r in enumerate(rows):
        parts = _row_parts(g, ri) if r["orient"] == orient else []
        if not parts:
            out_rows.append(r)
            continue
        starts = [p.x if orient == "h" else p.y for p in parts]
        ends = [st + (p.w if orient == "h" else p.d) for st, p in zip(starts, parts)]
        for k, cut in enumerate(sorted(cuts)):
            c = cut + k * gap  # posição da passagem já com as anteriores abertas
            idx = next((i for i, e in enumerate(ends) if e > c + 1e-6), None)
            if idx is None:
                break
            sh = max(0.0, c + gap - starts[idx])  # o que fica depois do corte começa no fim da passagem
            for i in range(idx, len(parts)):
                starts[i] += sh
                ends[i] += sh
        for i, p in enumerate(parts):  # estante com posição fixa não anda
            k_ = (p.bay.aisle, p.bay.bay)
            if fixed and k_ in fixed and abs(starts[i] - (p.x if orient == "h" else p.y)) > 1e-6:
                why = f" ({fixed[k_]})" if fixed[k_] else ""
                return None, _("não cabe: a estante fixa {bay} teria de andar{why}", bay=f"{k_[0]}-{k_[1]}", why=why)
        breaks = [0] + [i for i in range(1, len(parts)) if starts[i] > ends[i - 1] + 1e-6] + [len(parts)]
        if len(breaks) == 2 and abs(starts[0] - (parts[0].x if orient == "h" else parts[0].y)) < 1e-6:
            out_rows.append(r)  # a fileira não cruza nenhuma passagem
            continue
        for j in range(len(breaks) - 1):
            i0, i1 = breaks[j], breaks[j + 1]
            nr = {**r, "rev": False, "bays": [parts[i].bay.bay for i in range(i0, i1)]}
            # a fileira começa na 1ª estante (a profundidade da estante pode ser menor que a da fileira: só o eixo
            # ao longo muda)
            if orient == "h":
                nr["x"] = round(starts[i0], 1)
            else:
                nr["y"] = round(starts[i0], 1)
            if j:
                nr["id"], nxt = nxt, nxt + 1
                nr["pair"] = 0
            elif r.get("pair", 0) > 0:
                nr["pair"] = 0  # o vizinho escolhido à mão pode ter sido cortado: volta ao automático
            moved.add(nr["id"])
            out_rows.append(nr)
        if ends[-1] > along_max + 1e-6:
            return None, _("não cabe: as estantes passariam do fim do imóvel ({v} m a mais)",
                           v=f"{(ends[-1] - along_max) / 100:.1f}")
    if not moved:
        return None, _("nenhuma fileira atravessa essa posição")
    new = {**data, "rows": out_rows}
    from services.branch_layout import build

    g2 = build(new, g.bays)
    rects = g2.row_rects
    by_id = {r["id"]: i for i, r in enumerate(out_rows)}
    mv = {by_id[i] for i in moved}
    for a in rects:
        if a[4] not in mv:
            continue
        for b in rects:
            if b[4] != a[4] and _overlap(a, b):
                return None, _("não cabe: bateria na fileira {aisle}", aisle=out_rows[b[4]]["aisle"])
        for o in data.get("obstacles") or []:
            if _overlap(a, (o["x"], o["y"], o["w"], o["d"])):
                return None, _("não cabe: bateria no obstáculo {name}", name=o.get("name") or "")
        ref = data["ref"]
        if a[0] <= ref["x"] <= a[0] + a[2] and a[1] <= ref["y"] <= a[1] + a[3]:
            return None, _("não cabe: cobriria a referência")
    return new, ""


def _nat(v: str):
    from services.layout_service import natural_key

    return natural_key(v)


def _unit_sig(data: dict, g, cor) -> tuple:
    rows = data["rows"]
    shapes = []
    for ri in sorted(set(cor.rows)):
        r = rows[ri]
        x, y, w, d, _ri = g.row_rects[ri]
        shapes.append((round(w if r["orient"] == "h" else d), round(r["depth"]), len(_row_parts(g, ri))))
    return (cor.orient, tuple(sorted(shapes)))


def swap_corridors(data: dict, g, demand: dict[int, float], dist_of: dict[int, float],
                   fixed: Optional[dict] = None) -> tuple[Optional[dict], list]:
    """Troca de corredores: dentro de cada grupo de corredores de mesmo formato, os de mais saídas (`demand`, por
    índice de corredor) vão para as posições mais perto da referência (`dist_of`). Uma fileira só pode estar num
    corredor. (nova planta ou None se nada muda, [(de, para)] em letras)."""
    rows = data["rows"]
    owner: dict[int, int] = {}
    for c in g.corridors:
        for ri in c.rows:
            owner.setdefault(ri, c.index)
    units = [c for c in g.corridors if c.rows and all(owner.get(ri) == c.index for ri in c.rows)
             and len(set(c.rows)) == len(c.rows)
             and not any((p.bay.aisle, p.bay.bay) in (fixed or {}) for ri in c.rows for p in _row_parts(g, ri))]
    # (corredor com estante de posição fixa não troca de lugar)
    groups: dict[tuple, list] = {}
    for c in units:
        groups.setdefault(_unit_sig(data, g, c), []).append(c)
    new_rows = copy.deepcopy(rows)
    moves = []
    label_keys = ("aisle", "side", "bays", "rev", "both")

    def ordered(c):  # fileiras do corredor na ordem transversal (para casar as fileiras de dois corredores)
        return sorted(set(c.rows), key=lambda ri: (rows[ri]["y"] if c.orient == "h" else rows[ri]["x"]))

    def name(c):  # as estantes do corredor: "A 01–19 | A 02–20"
        out = []
        for ri in ordered(c):
            ps = _row_parts(g, ri)
            if ps:
                bs = sorted((p.bay.bay for p in ps), key=_nat)
                out.append(f"{rows[ri]['aisle']} {bs[0]}–{bs[-1]}" if len(bs) > 1 else f"{rows[ri]['aisle']} {bs[0]}")
        return " | ".join(out)

    for cs in groups.values():
        if len(cs) < 2:
            continue
        spots = sorted(cs, key=lambda c: dist_of.get(c.index, math.inf))  # posições: da mais perto à mais longe
        busy = sorted(cs, key=lambda c: (-demand.get(c.index, 0.0), dist_of.get(c.index, math.inf)))
        for spot, src in zip(spots, busy):
            if spot is src:
                continue
            for ri_dst, ri_src in zip(ordered(spot), ordered(src)):
                sr = rows[ri_src]
                parts = _row_parts(g, ri_src)
                lab = {k: sr.get(k) for k in label_keys if k in sr}
                lab["bays"] = [p.bay.bay for p in parts]  # lista fixa, na ordem da planta
                lab["rev"] = False
                for k in label_keys:
                    new_rows[ri_dst].pop(k, None)
                new_rows[ri_dst].update({k: v for k, v in lab.items() if v not in (None, False) or k == "rev"})
            moves.append((name(src), name(spot)))
    if not moves:
        return None, []
    return {**data, "rows": new_rows}, moves


def _stock(db, branch_id: int, wid: Optional[int]) -> list:
    from sqlalchemy import select

    from models import Product, StockItem, StorageLocation
    from services.addressing import scope_filter

    return list(db.execute(scope_filter(select(Product.sku, StorageLocation.aisle, StorageLocation.bay,
                                               StorageLocation.address, StockItem.quantity, StorageLocation.id)
                                        .join(StockItem, StockItem.product_id == Product.id)
                                        .join(StorageLocation, StorageLocation.id == StockItem.location_id)
                                        .where(StockItem.quantity >= 0), branch_id, wid)))


def _nodes(db, g, branch_id: int, wid: Optional[int], stock: list, gold: dict, assign: Optional[dict]) -> dict:
    """SKU -> paradas na geometria `g`: onde o SKU está hoje (`stock`) ou na proposta (`assign`)."""
    import dataclasses

    from services.branch_layout import mark_back

    mark_back(db, g, branch_id, wid)
    if assign is not None:
        from routers.layout import assign_nodes

        return assign_nodes(db, g, branch_id, wid, assign)
    nodes: dict[str, list] = {}
    zero: dict[str, list] = {}
    for sku, a, bay, addr, qty, lid in stock:
        n = g.node(a, bay, addr)
        if n is not None:
            (nodes if qty and qty > 0 else zero).setdefault(sku, []).append(dataclasses.replace(n, key=addr, gold=gold.get(lid)))
    for sku, ns in zero.items():
        nodes.setdefault(sku, ns)
    return nodes


def _corridor_demand(g, nodes: dict, sample: list, start) -> tuple[dict, dict]:
    """Saídas por corredor (cada linha conta no corredor do endereço mais perto da referência) e a distância da
    referência até o meio de cada corredor."""
    from services.route_sim import dist

    best = {}
    for sku, ns in nodes.items():
        if ns:
            best[sku] = min(ns, key=lambda n: dist(start, n))
    demand: dict[int, float] = {}
    for o in sample:
        for sku in o[1]:
            n = best.get(sku)
            if n is not None and n.corridor is not None:
                demand[n.corridor] = demand.get(n.corridor, 0.0) + 1
    by_c: dict[int, list] = {}
    for n in g.nodes.values():
        by_c.setdefault(n.corridor, []).append(dist(start, n))
    dist_of = {c: sum(v) / len(v) for c, v in by_c.items() if v}
    return demand, dist_of


def _lateral_info(db, branch_id: int, wid: Optional[int]):
    """Para comparar as laterais: {(corredor, estante): (peças de UMA lateral, tipo que compartilha, profundidade)} e
    as peças (código, nome, custo)."""
    from sqlalchemy import select

    from models import BayStructure, StructureKind, StructurePiece, StructureType
    from services.structure_kinds import composition, side_pieces

    kinds = {k.id: k for k in db.scalars(select(StructureKind))}
    info = {}
    for link, st in db.execute(select(BayStructure, StructureType).join(StructureType, StructureType.id == BayStructure.structure_type_id)
                               .where(BayStructure.branch_id == branch_id,
                                      BayStructure.warehouse_id.is_(None) if wid is None else BayStructure.warehouse_id == wid)):
        k = kinds.get(st.kind_id)
        side = side_pieces(composition(k)) if k is not None else {}
        if k is not None and k.shared_sides and side:
            info[(link.aisle, link.bay)] = (side, k.id, float(st.depth))
    return info, {p.id: p for p in db.scalars(select(StructurePiece))}


def lateral_delta(db, branch_id: int, wid: Optional[int], g_before, g_after, info=None) -> Optional[dict]:
    """Laterais (colunas / quadros de montantes) a mais ou a menos na variação: estantes vizinhas que compartilhavam a
    lateral e deixam de ser vizinhas (passagem no meio) precisam de uma lateral a mais; as que passam a ser vizinhas
    devolvem uma. None = nada muda (ou nenhum tipo compartilha a lateral)."""
    from services.structure_service import bay_neighbors

    info, pieces = info or _lateral_info(db, branch_id, wid)
    if not info:
        return None

    def shared(pairs) -> set:
        out = set()
        for a, b in pairs:
            if a in info and b in info and info[a][1] == info[b][1] and abs(info[a][2] - info[b][2]) < 1.0:
                out.add(tuple(sorted((a, b))))
        return out

    before = shared(bay_neighbors(db, branch_id, wid, g_before)[0])
    after = shared(bay_neighbors(db, branch_id, wid, g_after)[0])
    lost, gained = before - after, after - before
    if not lost and not gained:
        return None
    qty: dict[int, float] = {}
    for pair, sign in [(p, 1) for p in lost] + [(p, -1) for p in gained]:
        for pid, q in info[pair[0]][0].items():
            qty[pid] = qty.get(pid, 0.0) + sign * q
    rows = [{"code": pieces[p].code if p in pieces else str(p), "name": pieces[p].name if p in pieces else "", "qty": q,
             "cost": q * float(pieces[p].cost) if p in pieces and pieces[p].cost is not None else None}
            for p, q in sorted(qty.items()) if abs(q) > 1e-9]
    costs = [r["cost"] for r in rows if r["cost"] is not None]
    return {"sides": len(lost) - len(gained), "pieces": rows, "cost": sum(costs) if costs else None}


def propose_branch(db, branch_id: int, wid: Optional[int], assign: Optional[dict] = None, gap: float = PASSAGE_CM,
                   progress: Optional[Callable] = None, cancelled: Optional[Callable] = None) -> dict:
    """Variações da planta desenhada (passagens transversais, troca de corredores e as duas) medidas pelos pedidos do
    histórico. `assign` (SKU -> [(endereço, un.)]) = os SKUs na proposta do otimizador; None = onde estão hoje."""
    from sqlalchemy import select

    from models import PickHistory
    from services.branch_layout import address_golden, build, clean, get_layout, scope_bays
    from services.route_sim import simulate

    prog = progress or (lambda *_a: None)
    stop = cancelled or (lambda: False)
    lay = get_layout(db, branch_id, wid)
    if lay is None or not (lay.data or {}).get("rows"):
        return {"error": _("Esta filial ainda não tem planta desenhada.")}
    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    sample = _subsample((h.data or {}).get("sample") or []) if h else []
    if not sample:
        return {"error": _("A proposta usa a amostra de pedidos do histórico de saídas da filial (aba Pedidos). Envie o "
                           "histórico na etapa Posição pela saída.")}
    data = clean(lay.data)
    from services.bay_locks import fixed_bays

    fixed = fixed_bays(db, branch_id, wid)  # estantes com posição fixa não andam
    bays = scope_bays(db, branch_id, wid)
    stock = _stock(db, branch_id, wid)
    gold = address_golden(db, branch_id, wid)

    def measure(d: dict) -> tuple[dict, object]:
        g2 = build(d, bays, with_net=True)
        nodes = _nodes(db, g2, branch_id, wid, stock, gold, assign)
        return _walk(simulate(sample, nodes, g2.ref_node())), g2

    def door_block(g2) -> str:
        """Porta de giro que passaria a bater em algo na variação (a que já batia na planta atual não conta)."""
        new = [dr for dr in g2.doors if dr["hit"] and dr["bay"] not in base_hits]
        if not new:
            return ""
        more = _(" e mais {n}", n=len(new) - 1) if len(new) > 1 else ""
        return _("não serve: a porta de {bay} bateria em {what} ao abrir{more}", bay=new[0]["bay"], what=new[0]["hit"], more=more)

    steps = 6
    prog(0, steps, _("Medindo a planta atual"))
    w0, g = measure(data)
    base_hits = {dr["bay"] for dr in g.doors if dr["hit"]}
    if not w0:
        return {"error": _("Nenhum pedido da amostra tem SKUs em estantes da planta.")}
    variants = [{"label": _("Planta atual"), "current": True, "data": data, **w0, "what": []}]
    lat_info = _lateral_info(db, branch_id, wid)  # laterais compartilhadas entre estantes vizinhas (peças)
    tried: list[tuple] = []
    orients = sorted({r["orient"] for r in data["rows"]})
    for orient in orients:
        rr = [g.row_rects[i] for i, r in enumerate(data["rows"]) if r["orient"] == orient]
        lo = min((x if orient == "h" else y) for x, y, w, d, _i in rr)
        hi = max((x + w if orient == "h" else y + d) for x, y, w, d, _i in rr)
        if hi - lo < 4 * gap:
            continue
        for cuts in ([lo + (hi - lo) / 3], [lo + (hi - lo) / 2], [lo + 2 * (hi - lo) / 3],
                     [lo + (hi - lo) / 3, lo + 2 * (hi - lo) / 3]):
            tried.append((orient, cuts))
    best_pass = None
    for k, (orient, cuts) in enumerate(tried):
        if stop():
            break
        prog(1 + k * 3 // max(1, len(tried)), steps, _("Testando passagens transversais"))
        new, why = add_passages(data, g, orient, cuts, gap, fixed)
        where = ", ".join(f"{fmt_num(c / 100, 1)} m" for c in cuts)
        axis = "X" if orient == "h" else "Y"
        label = _("{n} passagem(ns) transversal(is) de {gap} m em {axis} = {where}", n=len(cuts), gap=fmt_num(gap / 100),
                  axis=axis, where=where)
        if new is None:
            if moved_any(why):
                variants.append({"label": label, "error": why})
            continue
        wv, g_new = measure(new)
        blocked = door_block(g_new)
        if blocked:
            variants.append({"label": label, "error": blocked})
            continue
        v = {"label": label, "data": new, **wv, "orient": orient, "cuts": cuts,
             "what": [_("Abrir {n} passagem(ns) de {gap} m atravessando as fileiras ao longo de {axis}, em {where}; as "
                        "estantes depois de cada passagem andam {gap} m.", n=len(cuts), gap=fmt_num(gap / 100), axis=axis,
                        where=where)]}
        variants.append(v)
        if v.get("opt_avg") is not None and (best_pass is None or v["opt_avg"] < best_pass["opt_avg"]):
            best_pass = v
    if not stop():
        prog(4, steps, _("Testando troca de corredores"))
        base_nodes = _nodes(db, g, branch_id, wid, stock, gold, assign)
        demand, dist_of = _corridor_demand(g, base_nodes, sample, g.ref_node())
        new, moves = swap_corridors(data, g, demand, dist_of, fixed)
        blocked = door_block(build(new, bays)) if new is not None else ""
        if new is not None and blocked:
            variants.append({"label": _("Troca de corredores ({n} mudança(s))", n=len(moves)), "error": blocked})
        elif new is not None:
            wv, _g = measure(new)
            what = [_("Trocar as estantes de lugar (com o que está nelas), os corredores com mais saídas mais perto da "
                      "referência.")]
            mv = [list(m) for m in moves]  # a tela mostra a lista recolhível: "A vai para o lugar de B"
            variants.append({"label": _("Troca de corredores ({n} mudança(s))", n=len(moves)), "data": new, **wv,
                             "what": what, "moves": mv, "swap": True})
            if best_pass is not None and not stop():
                prog(5, steps, _("Testando troca de corredores + passagem"))
                g_sw = build(new, bays)
                orient, cuts = best_pass["orient"], best_pass["cuts"]
                both, why = add_passages(new, g_sw, orient, cuts, gap, fixed)
                if both is not None and not door_block(build(both, bays)):
                    wb, _g = measure(both)
                    variants.append({"label": _("Troca de corredores + {label}", label=best_pass["label"]), "data": both,
                                     **wb, "what": what + best_pass["what"], "moves": mv})
        else:
            variants.append({"label": _("Troca de corredores"), "error": _("nenhuma troca ajuda: os corredores de mesmo "
                                                                           "formato já estão na ordem das saídas, ou não há "
                                                                           "corredores de mesmo formato (os corredores com "
                                                                           "estante de posição fixa ficam de fora)")})
    for v in variants:  # passagem no meio de estantes que compartilham a lateral: laterais a mais (peças)
        if v.get("data") is not None and not v.get("current") and not v.get("error"):
            v["laterals"] = lateral_delta(db, branch_id, wid, g, build(v["data"], bays), lat_info)
    rank(variants, "opt_avg")
    prog(steps, steps, _("Pronto"))
    return {"variants": variants, "key": "opt_avg", "orders": len(sample), "basis": "proposal" if assign else "stock",
            "branch_id": branch_id, "warehouse_id": wid, "partial": bool(stop())}
