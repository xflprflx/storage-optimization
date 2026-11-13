"""
Simulador de trajeto de separação a partir dos pedidos (amostra guardada na importação da aba Pedidos).

Para cada pedido: sai do ponto de referência (expedição), passa em uma posição de cada SKU e volta. Duas rotas:
  - "lista": na ordem dos endereços (como a lista de separação costuma vir impressa) — mostra o efeito da numeração
    das ruas (por exemplo, a serpentina);
  - "otimizada": vizinho mais próximo + melhorias 2-opt — o melhor caminho para aquele pedido.

Distância: com a rede de caminhada da planta (Node.net — projeto de filial nova ou planta desenhada da filial), o
menor caminho no piso livre (services/walk_net): pelo eixo dos corredores, saindo pela ponta ou por uma passagem, e
contornando fileiras e obstáculos; corredores de mão única só no sentido permitido (a distância pode não ser simétrica).
Sem a rede, o cálculo antigo: no mesmo corredor ao longo dele; corredores da mesma zona pela ponta/transversal; o
resto em L. Nas filiais existentes sem planta, a distância é a estimada pelo endereço (services/slotting.geo_dist).
O tempo do pedido (add_times) soma caminhada ÷ velocidade, paradas × tempo por parada e peças × tempo por peça.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Callable, Optional
from services.i18n import N_, _

EPS = 1e-9


@dataclass
class Node:
    key: str  # endereço
    gx: float  # ponto de parada na planta (cm)
    gy: float
    zone: int = -1
    aisle: int = -1  # corredor (na zona); -1 = sem corredor (cabeceira): só em L
    along: float = 0.0  # posição ao longo do corredor (cm, coordenada local)
    cross: float = 0.0  # centro do corredor (cm, coordenada local)
    ends: tuple = ()  # pontas e transversais do corredor (cm, ao longo)
    to_global: Optional[Callable] = None
    corridor: object = None  # corredor na rede de caminhada (services/walk_net)
    gold: Optional[bool] = None  # endereço na zona de ouro da PEGA (None = estrutura sem zona de ouro)
    net: object = None  # rede de caminhada da planta: com ela, a distância e o trajeto contornam as estruturas


def _natural(v: str):
    """Ordem dos endereços (lista de separação): números pelo valor ou, na configuração "ordem alfabética", pelo texto."""
    from services.app_settings import canon, get

    if get("addr_sort") == "alpha":
        return [canon(v)]
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v or "")]


def plan_nodes(plan) -> tuple[dict[str, list[Node]], dict]:
    """SKU -> pontos de parada (um por posição do plano) e o ponto de referência."""
    from services.site_planner import access_point

    ref = (plan.slotting or {}).get("ref") or {"x": 0.0, "y": plan.floor.width_cm / 2, "label": "frente"}
    out: dict[str, list[Node]] = {}
    for m in plan.modules:
        gx, gy = access_point(m)
        zn = m.zone
        if m.rotated and zn is not None:  # cabeceira: para em frente à face (voltada para a ponta do trecho)
            seg = zn.segments[m.segment] if m.segment < len(zn.segments) else (zn.x_start, zn.x_end)
            lx = m.x0 - 50.0 if m.x0 <= seg[0] + 1.0 else m.x0 + m.struct.depth + 50.0
            gx, gy = zn.to_global(lx, m.y0 + m.struct.width / 2, 0, 0)[:2]
            node = dict(gx=gx, gy=gy)
        elif m.rotated or zn is None or not zn.aisles:
            node = dict(gx=gx, gy=gy)
        else:
            y, w = zn.aisles[m.row.aisle]
            ends = sorted({zn.x_start, zn.x_end} | {v for seg in zn.segments for v in seg})
            node = dict(gx=gx, gy=gy, zone=zn.index, aisle=m.row.aisle, along=m.x0 + m.struct.width / 2,
                        cross=y + w / 2 if w > 0 else (m.row.y + m.row.depth if m.row.side == "L" else m.row.y),
                        ends=tuple(ends), to_global=zn.to_global)
        has_gold = m.struct.golden_from is not None and m.struct.golden_to is not None
        for lv in m.levels:
            gold = m.struct.is_golden(lv.n) if has_gold else None  # zona de ouro da pega do nível
            for p in lv.positions:
                out.setdefault(p.sku, []).append(Node(key=p.address, gold=gold, **node))
    return out, ref


def _glob(n: Node, e: float) -> tuple[float, float]:
    x, y, _w, _d = n.to_global(e, n.cross, 0, 0)
    return x, y


def dist(a: Node, b: Node) -> float:
    """cm, pelos corredores (com a rede de caminhada, pelo menor caminho que contorna as estruturas)."""
    if a.net is not None and a.net is b.net:
        return a.net.dist(a, b)
    if a.aisle >= 0 and b.aisle >= 0 and a.zone == b.zone:
        if a.aisle == b.aisle:
            return abs(a.along - b.along)
        return min(abs(a.along - e) + abs(b.along - e) + abs(a.cross - b.cross) for e in a.ends)
    if a.aisle >= 0 and b.aisle < 0 or a.aisle < 0 and b.aisle >= 0 or a.aisle >= 0 and b.aisle >= 0:
        # sai do(s) corredor(es) pela ponta mais conveniente e anda em L
        best = None
        for e1 in (a.ends if a.aisle >= 0 else (None,)):
            p1 = _glob(a, e1) if e1 is not None else (a.gx, a.gy)
            d1 = abs(a.along - e1) if e1 is not None else 0.0
            for e2 in (b.ends if b.aisle >= 0 else (None,)):
                p2 = _glob(b, e2) if e2 is not None else (b.gx, b.gy)
                d2 = abs(b.along - e2) if e2 is not None else 0.0
                v = d1 + abs(p1[0] - p2[0]) + abs(p1[1] - p2[1]) + d2
                best = v if best is None or v < best else best
        return best
    return abs(a.gx - b.gx) + abs(a.gy - b.gy)


def ref_node(ref: dict) -> Node:
    return Node(key="REF", gx=ref["x"], gy=ref["y"])


def _tour(nodes: list[Node], start: Node, d: Callable) -> float:
    pts = [start] + nodes + [start]
    return sum(d(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _optimized(nodes: list[Node], start: Node, d: Callable) -> list[Node]:
    """Vizinho mais próximo + 2-opt (pedidos pequenos)."""
    left, route, cur = list(nodes), [], start
    while left:
        nxt = min(left, key=lambda n: d(cur, n))
        left.remove(nxt)
        route.append(nxt)
        cur = nxt
    improved = len(route) > 3
    cost = _tour(route, start, d)
    passes = 0
    while improved and passes < 60:
        improved, passes = False, passes + 1
        pts = [start] + route + [start]
        for i in range(1, len(pts) - 2):
            for j in range(i + 1, len(pts) - 1):
                delta = (d(pts[i - 1], pts[j]) + d(pts[i], pts[j + 1])) - (d(pts[i - 1], pts[i]) + d(pts[j], pts[j + 1]))
                if delta < -1e-6:
                    cand = pts[:i] + pts[i:j + 1][::-1] + pts[j + 1:]
                    # com mão única a distância não é simétrica: confere o custo real do trecho invertido
                    new = _tour(cand[1:-1], start, d)
                    if new < cost - 1e-6:
                        pts, cost, improved = cand, new, True
        route = pts[1:-1]
    return route


def _corr_key(n: Node) -> tuple:
    """Corredor da parada para a serpentina, em ordem espacial: na planta da filial cada corredor é uma "zona"
    própria, então a ordem é pela orientação e pela posição do eixo; no projeto, pela zona e pelo eixo."""
    if n.net is not None and isinstance(n.corridor, int):
        c = n.net.corridors.get(n.corridor)
        return (getattr(c, "orient", "h"), n.cross)
    return (n.zone, n.cross)


def _s_shape(nodes: list[Node], start: Node, d: Callable) -> list[Node]:
    """Rota em S (serpentina): os corredores com paradas em ordem, alternando o sentido (ida num, volta no
    seguinte). Testa as duas ordens dos corredores (do primeiro ao último ou o contrário) e os dois sentidos de
    entrada no primeiro, e fica com a de menor caminhada — o separador entra e sai pela ponta mais conveniente.
    Paradas fora de corredor (cabeceiras) entram onde aumentam menos o trajeto."""
    corr: dict[tuple, list] = {}
    loose = []
    for n in nodes:
        if n.aisle >= 0:
            corr.setdefault(_corr_key(n), []).append(n)
        else:
            loose.append(n)
    best, best_d = None, None
    for keys in (sorted(corr), sorted(corr, reverse=True)):
        for first_rev in (False, True):
            route = []
            for i, k in enumerate(keys):
                route += sorted(corr[k], key=lambda n: n.along, reverse=bool(i % 2) != first_rev)
            for n in loose:  # inserção mais barata
                pts = [start] + route + [start]
                j = min(range(1, len(pts)), key=lambda j: d(pts[j - 1], n) + d(n, pts[j]) - d(pts[j - 1], pts[j]))
                route.insert(j - 1, n)
            v = _tour(route, start, d)
            if best_d is None or v < best_d - 1e-9:
                best, best_d = route, v
        if len(keys) <= 1 and not loose:
            break
    return best or []


def _stop_key(n: Node) -> tuple:
    """Ponto de parada: endereços do mesmo módulo (estante) param no mesmo lugar — uma parada só."""
    return (round(n.gx), round(n.gy))


def stop_groups(nodes: list[Node]) -> dict[str, list[Node]]:
    """Endereço representante (o 1º na ordem dos endereços) -> endereços do mesmo módulo."""
    groups: dict[tuple, list] = {}
    for n in sorted(nodes, key=lambda n: _natural(n.key)):
        groups.setdefault(_stop_key(n), []).append(n)
    return {g[0].key: g for g in groups.values()}


def simulate(sample: list, nodes_of: dict[str, list[Node]], start: Node, d: Callable = dist,
             unit: float = 100.0, keep_routes: Optional[set] = None, s_shape: bool = True) -> dict:
    """Simula a amostra de pedidos. Cada SKU usa a sua posição mais perto da referência. `unit` = divisor para
    metros (100 para cm). Retorna indicadores e, por pedido, as duas distâncias (e as rotas pedidas)."""
    near = {s: min(ns, key=lambda n: d(start, n)) for s, ns in nodes_of.items() if ns}
    rows, skipped_lines, routes = [], 0, {}
    for entry in sample:
        oid, skus = entry[0], entry[1]
        qty = entry[2] if len(entry) > 2 and entry[2] else None  # unidades por SKU (amostras antigas não têm)
        pts = [near[s] for s in skus if s in near]
        skipped_lines += len(skus) - len(pts)
        if not pts:
            continue
        units = sum(float(q) for s, q in zip(skus, qty) if s in near) if qty else float(len(pts))
        out_gold = sum(1 for s_ in skus if s_ in near and near[s_].gold is False)  # linhas fora da zona de ouro
        qmap = dict(zip(skus, qty)) if qty else {}
        out_units = sum(float(qmap.get(s_, 1.0)) for s_ in skus if s_ in near and near[s_].gold is False)  # peças
        # o mesmo endereço uma vez; endereços do mesmo módulo = uma parada (não sai e volta; tempo fixo uma vez)
        groups = stop_groups(list({n.key: n for n in pts}.values()))
        uniq = [g[0] for g in groups.values()]
        by_list = sorted(uniq, key=lambda n: _natural(n.key))
        opt = _optimized(uniq, start, d)
        s_route = _s_shape(uniq, start, d) if s_shape else None
        # "otimizada" = a melhor das rotas calculadas (a heurística não garante ser melhor que a em S)
        opt = min([r for r in (opt, s_route, by_list) if r is not None], key=lambda r: _tour(r, start, d))
        dl, do = _tour(by_list, start, d) / unit, _tour(opt, start, d) / unit
        row = {"order": oid, "lines": len(skus), "stops": len(uniq), "units": units, "out_gold": out_gold,
               "out_units": out_units, "list_m": dl, "opt_m": do}
        if len(entry) > 3 and entry[3]:
            row["date"] = entry[3]
        if s_route is not None:
            row["s_m"] = _tour(s_route, start, d) / unit
        rows.append(row)
        if keep_routes is not None and oid in keep_routes:
            routes[oid] = {"list": by_list, "opt": opt, "s": s_route, "groups": {k: [n.key for n in g] for k, g in groups.items()}}
    if not rows:
        return {"orders": 0, "rows": [], "skipped_lines": skipped_lines, "routes": routes}
    L = [r["list_m"] for r in rows]
    O = [r["opt_m"] for r in rows]
    lines = sum(r["lines"] for r in rows)
    q = lambda v, p: sorted(v)[min(len(v) - 1, int(round(p * (len(v) - 1))))]  # noqa: E731
    S = [r["s_m"] for r in rows if "s_m" in r]
    extra = {"s_avg": statistics.mean(S), "s_per_line": sum(S) / lines, "s_p50": q(S, 0.5), "s_p90": q(S, 0.9),
             "s_total": sum(S)} if S else {}
    return {**extra, "orders": len(rows), "rows": rows, "skipped_lines": skipped_lines, "routes": routes,
            "has_qty": any(len(e) > 2 and e[2] for e in sample),
            "lines": lines, "list_avg": statistics.mean(L), "opt_avg": statistics.mean(O),
            "list_per_line": sum(L) / lines, "opt_per_line": sum(O) / lines,
            "list_p50": q(L, 0.5), "list_p90": q(L, 0.9), "opt_p50": q(O, 0.5), "opt_p90": q(O, 0.9),
            "list_total": sum(L), "opt_total": sum(O),
            "gain_pct": (1 - sum(O) / sum(L)) * 100 if sum(L) else None}


def path_points(route: list[Node], start: Node, stops: Optional[list] = None) -> list[tuple[float, float]]:
    """Pontos (cm, planta) do trajeto para desenhar: com a rede de caminhada, o menor caminho que contorna as estruturas;
    sem ela, pelos corredores (sai pela ponta usada) e em L entre eles.
    Se `stops` for uma lista, recebe o índice (em pts) de cada parada, na ordem da rota (o último = a volta)."""
    pts = [(start.gx, start.gy)]
    seq = [start] + route + [start]
    for a, b in zip(seq, seq[1:]):
        if a.net is not None and a.net is b.net:
            pts += a.net.path(a, b)
            if stops is not None:
                stops.append(len(pts) - 1)
            continue
        if a.aisle >= 0 and b.aisle >= 0 and a.zone == b.zone and a.aisle == b.aisle:
            pts += [_glob(a, a.along), _glob(b, b.along), (b.gx, b.gy)]  # pelo meio do corredor
            if stops is not None:
                stops.append(len(pts) - 1)
            continue
        best = None
        for e1 in (a.ends if a.aisle >= 0 else (None,)):
            p1 = _glob(a, e1) if e1 is not None else (a.gx, a.gy)
            d1 = abs(a.along - e1) if e1 is not None else 0.0
            for e2 in (b.ends if b.aisle >= 0 else (None,)):
                p2 = _glob(b, e2) if e2 is not None else (b.gx, b.gy)
                d2 = abs(b.along - e2) if e2 is not None else 0.0
                v = d1 + abs(p1[0] - p2[0]) + abs(p1[1] - p2[1]) + d2
                if best is None or v < best[0]:
                    best = (v, p1, p2)
        _v, p1, p2 = best
        if a.aisle >= 0:
            pts.append(_glob(a, a.along))  # sai para o meio do corredor e segue até a ponta
        pts += [p1, (p2[0], p1[1]), p2]
        if b.aisle >= 0:
            pts.append(_glob(b, b.along))
        pts.append((b.gx, b.gy))
        if stops is not None:
            stops.append(len(pts) - 1)
    return pts


# m/s, s/parada, s/peça, s por item fora da zona de ouro da pega, s por peça fora da zona de ouro
TIME_DEFAULTS = {"t_v": 1.0, "t_stop": 10.0, "t_piece": 2.0, "t_gold": 5.0, "t_gold_piece": 0.0}


def time_params(qp) -> dict:
    """Parâmetros de tempo da tela (velocidade de caminhada, tempo por parada e por peça), com limites."""
    out = {}
    for k, (lo, hi) in (("t_v", (0.2, 3.0)), ("t_stop", (0.0, 600.0)), ("t_piece", (0.0, 600.0)), ("t_gold", (0.0, 600.0)),
                        ("t_gold_piece", (0.0, 600.0))):
        try:
            v = float(str(qp.get(k) if qp.get(k) not in (None, "") else TIME_DEFAULTS[k]).replace(",", "."))
        except ValueError:
            v = TIME_DEFAULTS[k]
        out[k] = min(hi, max(lo, v))
    return out


def add_times(sim: dict, tp: dict) -> dict:
    """Tempo por pedido (s) de cada rota: caminhada ÷ velocidade + paradas × tempo por parada + peças × tempo por
    peça. Grava no `sim` (times: média, total em horas e o tempo de cada pedido nas linhas)."""
    rows = sim.get("rows") or []
    if not rows:
        return sim
    fixed = lambda r: (r["stops"] * tp["t_stop"] + r.get("units", r["lines"]) * tp["t_piece"]  # noqa: E731
                       + r.get("out_gold", 0) * tp["t_gold"] + r.get("out_units", 0) * tp["t_gold_piece"])
    out = {"params": tp, "pick_avg": sum(fixed(r) for r in rows) / len(rows)}
    for k in ("list", "s", "opt"):
        if all(f"{k}_m" in r for r in rows):
            ts = [r[f"{k}_m"] / tp["t_v"] + fixed(r) for r in rows]
            for r, t in zip(rows, ts):
                r[f"{k}_t"] = t
            out[f"{k}_avg"] = sum(ts) / len(ts)
            out[f"{k}_h"] = sum(ts) / 3600
    out["units_avg"] = sum(r.get("units", r["lines"]) for r in rows) / len(rows)
    out["out_gold_avg"] = sum(r.get("out_gold", 0) for r in rows) / len(rows)
    out["out_units_avg"] = sum(r.get("out_units", 0) for r in rows) / len(rows)
    sim["times"] = out
    sim["days"] = by_day(sim)
    return sim


def day_rows(rows: list, day: Optional[str]) -> list:
    """Só os pedidos do dia escolhido (todos, sem dia)."""
    return [r for r in rows if r.get("date") == day] if day else rows


def by_day(sim: dict) -> list[dict]:
    """Totais por dia (pedidos com data): pedidos, linhas, peças, paradas, metros e horas de cada rota.
    Use depois de add_times (para as horas). Lista do dia mais recente para o mais antigo."""
    days: dict[str, dict] = {}
    for r in sim.get("rows") or []:
        if not r.get("date"):
            continue
        e = days.setdefault(r["date"], {"date": r["date"], "orders": 0, "lines": 0, "units": 0.0, "stops": 0})
        e["orders"] += 1
        e["lines"] += r["lines"]
        e["units"] += r.get("units", r["lines"])
        e["stops"] += r["stops"]
        for k in ("list", "s", "opt"):
            if f"{k}_m" in r:
                e[f"{k}_m"] = e.get(f"{k}_m", 0.0) + r[f"{k}_m"]
            if f"{k}_t" in r:
                e[f"{k}_h"] = e.get(f"{k}_h", 0.0) + r[f"{k}_t"] / 3600
    out = sorted(days.values(), key=lambda e: e["date"], reverse=True)
    for e in out:
        e["gain_pct"] = (1 - e["opt_m"] / e["list_m"]) * 100 if e.get("list_m") else None
    return out


ROUTE_KINDS = (("list", N_("Lista de separação"), "#d62728"), ("s", N_("Serpentina (rota em S)"), "#2f6db5"),
               ("opt", N_("Otimizada"), "#1a9850"))


def orders_payload(sample: list, nodes_of: dict, start: Node, ref: dict, d: Callable = dist) -> dict:
    """Lista de pedidos da amostra para a caminhada em 3D (maiores caminhadas na lista primeiro)."""
    sim = simulate(sample, nodes_of, start, d)
    rows = sorted(sim["rows"], key=lambda r: -r["list_m"])
    r2 = lambda v: round(v, 2) if v is not None else None  # noqa: E731
    return {"ref": ref, "kinds": [{"key": k, "name": n, "color": c} for k, n, c in ROUTE_KINDS],
            "summary": {k: r2(sim.get(k)) for k in ("list_avg", "s_avg", "opt_avg", "gain_pct")} | {"orders": sim["orders"],
                                                                                                "has_qty": sim.get("has_qty", False)},
            "orders": [{"order": r["order"], "lines": r["lines"], "stops": r["stops"], "list_m": r2(r["list_m"]),
                        "s_m": r2(r.get("s_m")), "opt_m": r2(r["opt_m"]), "date": r.get("date", "")} for r in rows],
            "days": [{k: (r2(v) if isinstance(v, float) else v) for k, v in e.items()} for e in by_day(sim)]}


def order_payload(sample: list, nodes_of: dict, start: Node, ref: dict, order: str, d: Callable = dist) -> dict:
    """As três rotas de um pedido: pontos (cm, planta), índice de cada parada em pts, endereços, SKUs e a distância."""
    lines = [o for o in sample if o[0] == order]
    if not lines:
        return {"error": _("Pedido {order} não está na amostra.", order=order)}
    sim = simulate(lines, nodes_of, start, d, keep_routes={order})
    rt = sim["routes"].get(order)
    if not rt:
        return {"error": _("Os SKUs do pedido {order} não estão na planta.", order=order)}
    sku_of = {n.key: s for s, ns in nodes_of.items() for n in ns}
    entry = lines[0]
    qty_of = dict(zip(entry[1], entry[2])) if len(entry) > 2 and entry[2] else {}
    units_at: dict[str, float] = {}  # peças em cada endereço (SKUs do pedido naquele endereço)
    out_at: dict[str, int] = {}  # linhas fora da zona de ouro da pega em cada endereço
    outq_at: dict[str, float] = {}  # peças fora da zona de ouro em cada endereço
    skus_at: dict[str, list] = {}
    near = {s: min(ns, key=lambda n: d(start, n)) for s, ns in nodes_of.items() if ns}
    for s_ in entry[1]:
        if s_ in near:
            k = near[s_].key
            units_at[k] = units_at.get(k, 0.0) + float(qty_of.get(s_, 1.0))
            out_at[k] = out_at.get(k, 0) + int(near[s_].gold is False)
            if near[s_].gold is False:
                outq_at[k] = outq_at.get(k, 0.0) + float(qty_of.get(s_, 1.0))
            skus_at.setdefault(k, []).append(s_)
    groups = rt.get("groups") or {}
    row = sim["rows"][0]
    routes = {}
    for kind, name, color in ROUTE_KINDS:
        if rt.get(kind) is None:
            continue
        idx: list = []
        pts = path_points(rt[kind], start, idx)
        grp = [groups.get(n.key, [n.key]) for n in rt[kind]]  # endereços de cada parada (mesmo módulo)
        routes[kind] = {"name": name, "color": color, "pts": [[round(x, 1), round(y, 1)] for x, y in pts],
                        "stops": idx[:-1], "keys": [n.key for n in rt[kind]], "groups": grp,
                        "skus": [", ".join(x for k in g for x in skus_at.get(k, [sku_of.get(k, "")])) for g in grp],
                        "qty": [round(sum(units_at.get(k, 1.0) for k in g), 3) for g in grp],
                        "out": [sum(out_at.get(k, 0) for k in g) for g in grp],
                        "outq": [round(sum(outq_at.get(k, 0.0) for k in g), 3) for g in grp],
                        "m": round(row[f"{kind}_m"], 2)}  # a distância da simulação (pelos corredores)
    return {"order": order, "lines": row["lines"], "stops": row["stops"], "units": row["units"], "has_qty": bool(qty_of),
            "out_gold": row["out_gold"], "has_gold": any(n.gold is not None for ns in nodes_of.values() for n in ns),
            "ref": ref, "routes": routes}


def day_payload(sample: list, nodes_of: dict, start: Node, ref: dict, day: str, d: Callable = dist) -> dict:
    """Um dia inteiro para a caminhada em 3D: os pedidos do dia, um depois do outro (cada um sai da referência e
    volta a ela), no mesmo formato de order_payload. Cada rota traz também `ord` (índice do pedido de cada parada) e
    `starts` (índice em pts onde começa cada pedido); `orders` lista os pedidos na ordem feita."""
    entries = [o for o in sample if len(o) > 3 and o[3] == day]
    if not entries:
        return {"error": _("Nenhum pedido do dia {day} na amostra.", day=day)}
    done, routes, tot = [], {}, {"lines": 0, "stops": 0, "units": 0.0, "out_gold": 0}
    has_qty = has_gold = False
    for e in sorted(entries, key=lambda o: o[0]):
        p = order_payload([e], nodes_of, start, ref, e[0], d)
        if p.get("error"):
            continue
        oi = len(done)
        done.append({"order": e[0], "lines": p["lines"], "stops": p["stops"], "units": p["units"]})
        for k in tot:
            tot[k] += p.get(k) or 0
        has_qty, has_gold = has_qty or p["has_qty"], has_gold or p["has_gold"]
        for kind, r in p["routes"].items():
            m = routes.setdefault(kind, {"name": r["name"], "color": r["color"], "pts": [], "stops": [], "keys": [],
                                         "groups": [], "skus": [], "qty": [], "out": [], "outq": [], "m": 0.0,
                                         "ord": [], "starts": []})
            base = len(m["pts"])
            m["starts"].append(base)
            m["pts"] += r["pts"]
            m["stops"] += [base + i for i in r["stops"]]
            for f in ("keys", "groups", "skus", "qty", "out", "outq"):
                m[f] += r[f]
            m["ord"] += [oi] * len(r["keys"])
            m["m"] = round(m["m"] + r["m"], 2)
    if not done:
        return {"error": _("Os SKUs dos pedidos do dia {day} não estão na planta.", day=day)}
    return {"day": day, "order": day, "orders": done, "lines": tot["lines"], "stops": tot["stops"],
            "units": round(tot["units"], 3), "out_gold": tot["out_gold"], "has_qty": has_qty, "has_gold": has_gold,
            "ref": ref, "routes": routes}
