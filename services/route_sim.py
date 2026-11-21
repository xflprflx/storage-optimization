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
    door: Optional[tuple] = None  # (porta, segundos para abrir e fechar): estante com porta (tipo de estrutura)


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
        dr = m.struct.door
        if dr and dr.get("time"):
            node["door"] = (f"{m.aisle}-{m.bay}", dr["time"])  # estrutura com porta: abrir e fechar a cada visita
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


def _optimized(nodes: list[Node], start: Node, d: Callable, max_two_opt: int = 70) -> list[Node]:
    """Vizinho mais próximo + 2-opt (só com até `max_two_opt` paradas: em lotes grandes fica o vizinho mais próximo,
    e a serpentina costuma ganhar)."""
    left, route, cur = list(nodes), [], start
    while left:
        nxt = min(left, key=lambda n: d(cur, n))
        left.remove(nxt)
        route.append(nxt)
        cur = nxt
    improved = 3 < len(route) <= max_two_opt
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
    """Ponto de parada: endereços do mesmo módulo (estante) param no mesmo lugar — uma parada só. Com planta, pelo
    ponto na planta; sem planta (sem coordenadas), pela posição no corredor (corredor, eixo e posição da estante)."""
    if n.gx or n.gy or n.net is not None:
        return (round(n.gx), round(n.gy))
    return ("addr", n.zone, n.aisle, round(n.cross), round(n.along))


def stop_groups(nodes: list[Node]) -> dict[str, list[Node]]:
    """Endereço representante (o 1º na ordem dos endereços) -> endereços do mesmo módulo."""
    groups: dict[tuple, list] = {}
    for n in sorted(nodes, key=lambda n: _natural(n.key)):
        groups.setdefault(_stop_key(n), []).append(n)
    return {g[0].key: g for g in groups.values()}


BATCH_KEYS = ("blist", "bs", "bopt")  # lote: trajeto na ordem da lista, em serpentina e otimizado
BATCH_CANDIDATES = 12  # pedidos mais próximos avaliados a cada inclusão no trajeto


def _xy(n: Node) -> tuple[float, float]:
    """Posição aproximada da parada para achar pedidos vizinhos (planta: x, y; sem planta: corredor, posição)."""
    return (n.gx, n.gy) if (n.gx or n.gy) else (n.cross, n.along)


def _rel0(rows_by: dict, oids) -> dict:
    """Liberação de cada pedido em segundos desde o INÍCIO do dia (o 1º horário de liberação do dia); pedido sem
    horário = início (0)."""
    rels = [rows_by[o].get("rel") for o in oids if rows_by[o].get("rel") is not None]
    base = min(rels) if rels else 0
    return {o: (rows_by[o]["rel"] - base if rows_by[o].get("rel") is not None else 0) for o in oids}


def _batch(rows: list, stops_of: dict, size: int, start: Node, d: Callable, unit: float, s_shape: bool,
           keep_routes: Optional[set], routes: dict, release: bool = False, tp: Optional[dict] = None) -> list[dict]:
    """Separação em lote: agrupa até `size` pedidos do MESMO dia (sem data: todos juntos) por trajeto e percorre as
    paradas de todos numa rota só. Agrupamento guloso: um pedido abre o trajeto — o de maior caminhada ainda livre ou,
    com `release` (horário de liberação), o liberado há mais tempo — e depois entra, um de cada vez, o pedido que
    menos acrescenta caminhada (soma, para cada parada nova, da distância até a parada mais perto já no trajeto),
    entre os BATCH_CANDIDATES de centro mais perto. Com `release`, só entram pedidos JÁ liberados quando o trajeto sai:
    o relógio do separador avança com o tempo estimado de cada trajeto (parâmetros `tp`) e, sem pedido liberado, ele
    espera o próximo; com `t_bwait`, o lote incompleto espera até t_bwait min desde a liberação do 1º pedido
    dele (sai antes se completar). A rota do trajeto é feita nas três formas (lista, serpentina, otimizada = a melhor delas).
    Endereços repetidos e do mesmo módulo = uma parada. A caminhada e as paradas do trajeto são divididas igualmente
    entre os pedidos; peças e pega continuam do pedido. Grava em cada linha blist_m/bs_m/bopt_m, batch_stops e trip."""
    tp = {**TIME_DEFAULTS, **(tp or {})}
    wait_s = float(tp.get("t_bwait") or 0) * 60 if release else 0.0  # espera máxima para completar o lote (s)
    by_row = {r["order"]: r for r in rows}
    pools: dict[str, list] = {}
    for oid in stops_of:
        pools.setdefault(by_row[oid].get("date") or "", []).append(oid)
    trips = []

    def make(trip: list, start_s: Optional[float] = None) -> float:
        """Fecha o trajeto: rota, distâncias, partilha entre os pedidos. Devolve o tempo estimado (s)."""
        nodes = {}
        for o in trip:
            nodes.update({n.key: n for n in stops_of[o][0]})
        groups = stop_groups(list(nodes.values()))
        uniq = [g[0] for g in groups.values()]
        trip_doors = sum({n.door[0]: n.door[1] for n in nodes.values() if n.door}.values())
        by_list = sorted(uniq, key=lambda n: _natural(n.key))
        s_route = _s_shape(uniq, start, d) if s_shape else None
        opt = min([r for r in (_optimized(uniq, start, d), s_route, by_list) if r is not None],
                  key=lambda r: _tour(r, start, d))
        tid = len(trips) + 1
        m = {"blist": _tour(by_list, start, d) / unit, "bopt": _tour(opt, start, d) / unit}
        if s_route is not None:
            m["bs"] = _tour(s_route, start, d) / unit
        n = len(trip)
        for oid in trip:
            r = by_row[oid]
            for k, v in m.items():
                r[f"{k}_m"] = v / n
            r["batch_stops"] = len(uniq) / n
            r["trip"] = tid
            r["batch_door_s"] = trip_doors / n  # no lote, cada porta abre uma vez por trajeto
        t = {"id": tid, "date": by_row[trip[0]].get("date") or "", "orders": list(trip), "stops": len(uniq),
             **{f"{k}_m": v for k, v in m.items()}, "route": opt, "groups": {k: [x.key for x in g] for k, g in groups.items()}}
        if start_s is not None:
            t["start_s"] = start_s
        trips.append(t)
        if keep_routes is not None and keep_routes & set(trip):
            for oid in trip:
                routes.setdefault(oid, {})["batch"] = {"route": opt, "trip": tid, "orders": trip}
        rr = [by_row[o] for o in trip]
        return (m["bopt"] / tp["t_v"] + len(uniq) * tp["t_stop"] + trip_doors + sum(r.get("units", r["lines"]) for r in rr) * tp["t_piece"]
                + sum(r.get("out_gold", 0) for r in rr) * tp["t_gold"] + sum(r.get("out_units", 0) for r in rr) * tp["t_gold_piece"])

    for day, pool in sorted(pools.items()):
        center = {}
        for oid in pool:
            pts = [_xy(n) for n in stops_of[oid][0]]
            center[oid] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        rel = _rel0(by_row, pool) if release else {}
        left = sorted(pool, key=lambda o: (rel[o], o)) if release else sorted(pool, key=lambda o: -stops_of[o][1])
        clock = 0.0
        while left:
            if release:
                clock = max(clock, rel[left[0]])  # sem pedido liberado: espera o próximo
                avail = [o for o in left if rel[o] <= clock + 1e-9]
                if wait_s > 0 and len(avail) < size:
                    # lote incompleto: a janela conta da liberação do 1º pedido do lote (o mais antigo na fila) — o
                    # separador não sai com ele; sai quando o lote completa ou quando a janela acaba, com o que tiver
                    deadline = rel[left[0]] + wait_s
                    if deadline > clock + 1e-9:
                        need = size - len(avail)
                        coming = sorted(rel[o] for o in left if rel[o] > clock + 1e-9)[:need]
                        clock = coming[-1] if len(coming) == need and coming[-1] <= deadline + 1e-9 else deadline
                        avail = [o for o in left if rel[o] <= clock + 1e-9]
            else:
                avail = left
            seed = avail[0]
            left.remove(seed)
            ok = set(avail)
            avail = [o for o in left if o in ok] if release else list(left)  # cópia (sem o pedido que abriu)
            trip = [seed]
            nodes = {n.key: n for n in stops_of[trip[0]][0]}
            while len(trip) < size and avail:
                cx = sum(center[o][0] for o in trip) / len(trip)
                cy = sum(center[o][1] for o in trip) / len(trip)
                cands = sorted(avail, key=lambda o: abs(center[o][0] - cx) + abs(center[o][1] - cy))[:BATCH_CANDIDATES]
                have = list(nodes.values())

                def extra(o):
                    return sum(min(d(n, h) for h in have) for n in stops_of[o][0] if n.key not in nodes)

                best = min(cands, key=extra)
                avail.remove(best)
                left.remove(best)
                trip.append(best)
                nodes.update({n.key: n for n in stops_of[best][0]})
            clock += make(trip, clock if release else None)
    return trips


def simulate(sample: list, nodes_of: dict[str, list[Node]], start: Node, d: Callable = dist,
             unit: float = 100.0, keep_routes: Optional[set] = None, s_shape: bool = True, batch: int = 0,
             release: bool = False, tp: Optional[dict] = None) -> dict:
    """Simula a amostra de pedidos. Cada SKU usa a sua posição mais perto da referência. `unit` = divisor para
    metros (100 para cm). Retorna indicadores e, por pedido, as distâncias de cada rota (e as rotas pedidas).
    Com `batch` >= 2, também a separação em lote: até `batch` pedidos do mesmo dia por trajeto (ver _batch).
    Com `release` (e a amostra com o horário de liberação), o lote só junta pedidos já liberados e add_times monta a
    linha do tempo do dia (término, ocioso e espera de cada pedido); `tp` = tempos (para o relógio do lote)."""
    has_release = any(len(e) > 4 for e in sample)
    release = bool(release and has_release)
    near = {s: min(ns, key=lambda n: d(start, n)) for s, ns in nodes_of.items() if ns}
    rows, skipped_lines, routes = [], 0, {}
    stops_of: dict = {}  # pedido -> paradas (representantes) e lista completa de endereços, para o lote
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
        doors = {n.door[0]: n.door[1] for n in pts if n.door}  # cada porta abre e fecha uma vez no pedido
        if doors:
            row["door_s"] = sum(doors.values())
        if len(entry) > 3 and entry[3]:
            row["date"] = entry[3]
        if len(entry) > 4:
            row["rel"] = entry[4]  # horário de liberação (s desde 0h); None = sem horário (início do dia)
        if s_route is not None:
            row["s_m"] = _tour(s_route, start, d) / unit
        rows.append(row)
        if batch and batch >= 2:
            stops_of[oid] = (list({n.key: n for n in pts}.values()), _tour(opt, start, d))
        if keep_routes is not None and oid in keep_routes:
            routes[oid] = {"list": by_list, "opt": opt, "s": s_route, "groups": {k: [n.key for n in g] for k, g in groups.items()}}
    if not rows:
        return {"orders": 0, "rows": [], "skipped_lines": skipped_lines, "routes": routes, "has_release": has_release,
                "release_on": release}
    trips = _batch(rows, stops_of, batch, start, d, unit, s_shape, keep_routes, routes, release, tp) if stops_of else []
    L = [r["list_m"] for r in rows]
    O = [r["opt_m"] for r in rows]
    lines = sum(r["lines"] for r in rows)
    q = lambda v, p: sorted(v)[min(len(v) - 1, int(round(p * (len(v) - 1))))]  # noqa: E731
    extra = {}
    for k in ("s",) + BATCH_KEYS:  # serpentina e as rotas do lote
        V = [r[f"{k}_m"] for r in rows if f"{k}_m" in r]
        if V:
            extra.update({f"{k}_avg": statistics.mean(V), f"{k}_per_line": sum(V) / lines, f"{k}_p50": q(V, 0.5),
                          f"{k}_p90": q(V, 0.9), f"{k}_total": sum(V)})
    if trips:
        extra.update(batch=batch, trips=trips, trips_n=len(trips),
                     batch_gain_pct=(1 - extra["bopt_total"] / sum(O)) * 100 if sum(O) else None)
    return {**extra, "orders": len(rows), "rows": rows, "skipped_lines": skipped_lines, "routes": routes,
            "has_release": has_release, "release_on": release,
            "has_qty": any(len(e) > 2 and e[2] for e in sample),
            "lines": lines, "list_avg": statistics.mean(L), "opt_avg": statistics.mean(O),
            "list_per_line": sum(L) / lines, "opt_per_line": sum(O) / lines,
            "list_p50": q(L, 0.5), "list_p90": q(L, 0.9), "opt_p50": q(O, 0.5), "opt_p90": q(O, 0.9),
            "list_total": sum(L), "opt_total": sum(O),
            "gain_pct": (1 - sum(O) / sum(L)) * 100 if sum(L) else None}


def trip_of(sim: dict, order: str) -> Optional[dict]:
    """Trajeto do lote em que o pedido foi separado (None sem lote)."""
    return next((t for t in sim.get("trips") or [] if order in t["orders"]), None)


BATCH_COLOR = "#7b3fb5"


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
TIME_DEFAULTS = {"t_v": 1.0, "t_stop": 10.0, "t_piece": 2.0, "t_gold": 5.0, "t_gold_piece": 0.0,
                 "t_batch": 4,  # separação em lote: pedidos por trajeto (2 a 20)
                 "t_bwait": 0}  # lote com horário de liberação: espera máxima para completar o lote (min)


def time_params(qp) -> dict:
    """Parâmetros de tempo da tela (velocidade de caminhada, tempo por parada e por peça), com limites."""
    from services.app_settings import sim_times

    dflt = {**TIME_DEFAULTS, **sim_times()}  # padrão das Configurações (a tela pode mudar)
    out = {}
    for k, (lo, hi) in (("t_v", (0.2, 3.0)), ("t_stop", (0.0, 600.0)), ("t_piece", (0.0, 600.0)), ("t_gold", (0.0, 600.0)),
                        ("t_gold_piece", (0.0, 600.0)), ("t_batch", (2, 20)), ("t_bwait", (0.0, 240.0))):
        try:
            v = float(str(qp.get(k) if qp.get(k) not in (None, "") else dflt[k]).replace(",", "."))
        except ValueError:
            v = dflt[k]
        out[k] = min(hi, max(lo, v))
    out["t_batch"] = int(round(out["t_batch"]))
    return out


def add_times(sim: dict, tp: dict) -> dict:
    """Tempo por pedido (s) de cada rota: caminhada ÷ velocidade + paradas × tempo por parada + peças × tempo por
    peça. Grava no `sim` (times: média, total em horas e o tempo de cada pedido nas linhas)."""
    rows = sim.get("rows") or []
    if not rows:
        return sim
    fixed = lambda r: (r["stops"] * tp["t_stop"] + r.get("units", r["lines"]) * tp["t_piece"]  # noqa: E731
                       + r.get("out_gold", 0) * tp["t_gold"] + r.get("out_units", 0) * tp["t_gold_piece"]
                       + r.get("door_s", 0.0))  # portas (câmara, armário): abrir e fechar
    # no lote, as paradas são as do trajeto divididas entre os pedidos (batch_stops); as portas também
    fixed_b = lambda r: (fixed(r) + (r.get("batch_stops", r["stops"]) - r["stops"]) * tp["t_stop"]  # noqa: E731
                         - r.get("door_s", 0.0) + r.get("batch_door_s", r.get("door_s", 0.0)))
    out = {"params": tp, "pick_avg": sum(fixed(r) for r in rows) / len(rows)}
    for k in ("list", "s", "opt") + BATCH_KEYS:
        if all(f"{k}_m" in r for r in rows):
            fx = fixed_b if k in BATCH_KEYS else fixed
            ts = [r[f"{k}_m"] / tp["t_v"] + fx(r) for r in rows]
            for r, t in zip(rows, ts):
                r[f"{k}_t"] = t
            out[f"{k}_avg"] = sum(ts) / len(ts)
            out[f"{k}_h"] = sum(ts) / 3600
    if sim.get("trips"):  # tempo de cada trajeto (soma dos pedidos dele)
        t_of = {r["order"]: r.get("bopt_t", 0.0) for r in rows}
        for t in sim["trips"]:
            t["bopt_t"] = sum(t_of.get(o, 0.0) for o in t["orders"])
    out["units_avg"] = sum(r.get("units", r["lines"]) for r in rows) / len(rows)
    out["door_avg"] = sum(r.get("door_s", 0.0) for r in rows) / len(rows)  # portas por pedido (s)
    out["out_gold_avg"] = sum(r.get("out_gold", 0) for r in rows) / len(rows)
    out["out_units_avg"] = sum(r.get("out_units", 0) for r in rows) / len(rows)
    sim["times"] = out
    sim["days"] = by_day(sim)
    if sim.get("release_on"):
        _timeline(sim)
        sim["insights"] = {k: v for k in ("opt", "bopt") if (v := idle_insights(sim, k))}
    return sim


TIMELINE_KEYS = ("list", "s", "opt") + BATCH_KEYS


def _timeline(sim: dict) -> None:
    """Linha do tempo de cada dia com o horário de liberação (um separador): os pedidos na ordem em que foram
    liberados; o separador começa no 1º horário do dia e, sem pedido liberado, espera (ocioso). No lote, os trajetos na
    ordem em que foram montados; um trajeto sai quando o separador está livre e todos os pedidos dele foram liberados.
    Grava em cada dia: início (s desde 0h), e por rota o término (s desde o início), o ocioso e a espera média do pedido
    (da liberação até ficar pronto); em times, as médias gerais."""
    rows = sim.get("rows") or []
    by_row = {r["order"]: r for r in rows}
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("date") or "", []).append(r["order"])
    trips_of: dict[str, list] = {}
    for t in sim.get("trips") or []:
        trips_of.setdefault(t.get("date") or "", []).append(t)
    days = {e["date"]: e for e in sim.get("days") or []}
    leads: dict[str, list] = {k: [] for k in TIMELINE_KEYS}
    ends: dict[str, list] = {k: [] for k in TIMELINE_KEYS}
    idles: dict[str, float] = {k: 0.0 for k in TIMELINE_KEYS}
    bwaits: dict[str, float] = {}  # espera para completar o lote (parte do ocioso)
    # para os insights de ociosidade (rota otimizada e lote): intervalos ociosos e trabalho, em s desde 0h, e a
    # liberação/início/fim de cada pedido
    log: dict[str, dict] = {k: {"idle": [], "busy": [], "orders": [], "days": []} for k in ("opt", "bopt")}
    for day, oids in groups.items():
        rel = _rel0(by_row, oids)
        rels = [by_row[o]["rel"] for o in oids if by_row[o].get("rel") is not None]
        tl = {"start_s": min(rels) if rels else None}
        for k in TIMELINE_KEYS:
            if k in BATCH_KEYS:  # lote: os trajetos, na ordem em que foram montados (saída = a do agrupamento)
                units = [(t["orders"], sum(by_row[o].get(f"{k}_t", 0.0) for o in t["orders"]), t.get("start_s", 0.0))
                         for t in trips_of.get(day, [])]
                if not units or not all(f"{k}_t" in by_row[o] for o in oids):
                    continue
            else:
                if not all(f"{k}_t" in by_row[o] for o in oids):
                    continue
                units = [([o], by_row[o][f"{k}_t"], 0.0) for o in sorted(oids, key=lambda o: (rel[o], o))]
            clock = idle = bwait = 0.0
            lead = []
            lg = log.get(k)
            d0 = tl["start_s"] or 0
            for os_, dur, depart in units:
                natural = max(clock, max(rel[o] for o in os_))
                st = max(natural, depart if k == "bopt" else natural)  # lote: esperou para completar o trajeto
                # espera para agrupar: desde quando podia sair (livre e com o 1º pedido do trajeto liberado) até a saída
                bwait += st - max(clock, min(rel[o] for o in os_)) if k == "bopt" else 0.0
                if lg is not None and st > clock + 1e-9:
                    lg["idle"].append((d0 + clock, d0 + st))
                idle += st - clock
                if lg is not None:
                    lg["busy"].append((d0 + st, d0 + st + dur))
                for o in os_:
                    if lg is not None:
                        lg["orders"].append((d0 + rel[o], d0 + st, d0 + st + dur, day, dur / len(os_)))
                clock = st + dur
                for o in os_:
                    lead.append(clock - rel[o])
                    by_row[o][f"{k}_lead"] = clock - rel[o]
            if lg is not None:
                lg["days"].append((day, d0, d0 + clock, idle))
            tl.update({f"{k}_end": clock, f"{k}_idle": idle, f"{k}_lead": sum(lead) / len(lead), f"{k}_bwait": bwait})
            bwaits[k] = bwaits.get(k, 0.0) + bwait
            leads[k] += lead
            ends[k].append(clock)
            idles[k] += idle
        if day in days:
            days[day].update(tl)
    sim["_tl_log"] = log  # só em memória (insights); não vai para JSON
    t = sim.setdefault("times", {})
    for k in TIMELINE_KEYS:
        if leads[k]:
            t[f"{k}_lead_avg"] = sum(leads[k]) / len(leads[k])
            t[f"{k}_end_avg"] = sum(ends[k]) / len(ends[k])
            t[f"{k}_idle_h"] = idles[k] / 3600
            t[f"{k}_bwait_h"] = bwaits.get(k, 0.0) / 3600


def _spread(intervals, buckets: dict) -> None:
    """Soma em cada hora do dia (0–23) os segundos de cada intervalo (a, b) em s desde 0h."""
    for a, b in intervals:
        h = int(a // 3600)
        while a < b - 1e-9:
            end = min(b, (h + 1) * 3600)
            buckets[h % 24] = buckets.get(h % 24, 0.0) + end - a
            a, h = end, h + 1


def idle_insights(sim: dict, k: str = "opt") -> Optional[dict]:
    """Insights de ociosidade com o horário de liberação (um separador), pela rota `k` ("opt" ou "bopt" = lote):
    horas ociosas (total, por dia e % do turno), por hora do dia (ocioso, trabalho, pedidos liberados, espera média e
    separadores que dariam conta do trabalho liberado), os horários mais ociosos, o pior horário de fila, o maior
    intervalo ocioso, os dias mais e menos ociosos e recomendações em texto. None sem a opção ligada."""
    import math

    log = (sim.get("_tl_log") or {}).get(k)
    if not sim.get("release_on") or not log or not log["days"]:
        return None
    n = len(log["days"])
    idle_h, busy_h, rel_n, rel_work, wait_s = {}, {}, {}, {}, {}
    _spread(log["idle"], idle_h)
    _spread(log["busy"], busy_h)
    waits = []
    for rel, st, _end, _day, work in log["orders"]:
        h = int(rel // 3600) % 24
        rel_n[h] = rel_n.get(h, 0) + 1
        rel_work[h] = rel_work.get(h, 0.0) + work
        wait_s.setdefault(h, []).append(st - rel)
        waits.append(st - rel)
    hours = sorted(set(idle_h) | set(busy_h) | set(rel_n))
    per_hour = []
    for h in range(hours[0], hours[-1] + 1):
        idle_m, busy_m = idle_h.get(h, 0.0) / n / 60, busy_h.get(h, 0.0) / n / 60
        work_h = rel_work.get(h, 0.0) / n / 3600  # horas de trabalho liberadas nessa hora, por dia
        per_hour.append({"h": h, "idle_m": idle_m, "busy_m": busy_m, "util": busy_m / (busy_m + idle_m) if busy_m + idle_m else None,
                         "released": rel_n.get(h, 0) / n, "work_m": work_h * 60,
                         "wait_m": (sum(wait_s[h]) / len(wait_s[h]) / 60) if wait_s.get(h) else None,
                         "pickers": max(1, math.ceil(work_h - 1e-9)) if rel_n.get(h) else 0})
    total_idle = sum(b - a for a, b in log["idle"])
    shift = sum(e - s for _d, s, e, _i in log["days"])
    gaps = sorted(((b - a, a, b) for a, b in log["idle"]), reverse=True)
    by_idle = sorted(log["days"], key=lambda x: -x[3])
    top_idle = sorted((r for r in per_hour if r["idle_m"] > 0.5), key=lambda r: -r["idle_m"])[:3]
    worst_wait = max((r for r in per_hour if r["wait_m"] is not None), key=lambda r: r["wait_m"], default=None)
    peak_work = max(per_hour, key=lambda r: r["work_m"])
    out = {"days": n, "idle_h": total_idle / 3600, "idle_day_h": total_idle / n / 3600,
           "bwait_day_m": (sim.get("times") or {}).get(f"{k}_bwait_h", 0.0) * 60 / n,
           "idle_pct": total_idle / shift * 100 if shift else None, "shift_h": shift / n / 3600,
           "wait_avg_m": sum(waits) / len(waits) / 60 if waits else 0.0,
           "wait_p90_m": sorted(waits)[int(0.9 * (len(waits) - 1))] / 60 if waits else 0.0,
           "longest_gap": {"m": gaps[0][0] / 60, "from": gaps[0][1], "to": gaps[0][2]} if gaps else None,
           "gaps_over_15": sum(1 for g in gaps if g[0] >= 900) / n,
           "per_hour": per_hour, "top_idle": top_idle, "worst_wait": worst_wait, "peak_work": peak_work,
           "max_idle": max([r["idle_m"] for r in per_hour] + [1.0]),
           "most_idle_days": [{"date": d, "idle_h": i / 3600} for d, _s, _e, i in by_idle[:3]],
           "least_idle_days": [{"date": d, "idle_h": i / 3600} for d, _s, _e, i in by_idle[-3:][::-1]]}
    # recomendações (texto montado na tela, com os números daqui)
    tips = []
    if top_idle:  # horários mais ociosos, juntos em janelas contínuas (08h–10h, 13h–14h)
        wins: list[list[int]] = []
        for h in sorted(r["h"] for r in top_idle):
            if wins and wins[-1][1] == h:
                wins[-1][1] = h + 1
            else:
                wins.append([h, h + 1])
        tips.append({"kind": "idle", "windows": wins, "m": sum(r["idle_m"] for r in top_idle)})
    busiest = max((r for r in per_hour if r["util"] is not None), key=lambda r: r["util"], default=None)
    if busiest and busiest["util"] >= 0.5:
        tips.append({"kind": "busy", "h": busiest["h"], "pct": busiest["util"] * 100})
    if worst_wait and worst_wait["wait_m"] >= 5:
        tips.append({"kind": "queue", "h": worst_wait["h"], "m": worst_wait["wait_m"], "pickers": worst_wait["pickers"]})
    if peak_work["pickers"] >= 2:
        tips.append({"kind": "pickers", "h": peak_work["h"], "pickers": peak_work["pickers"], "work_m": peak_work["work_m"]})
    if out["idle_pct"] is not None and out["idle_pct"] >= 40:
        tips.append({"kind": "oversized", "pct": out["idle_pct"]})
    if out["gaps_over_15"] >= 1:
        tips.append({"kind": "gaps", "n": out["gaps_over_15"]})
    out["tips"] = tips
    return out


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
        e = days.setdefault(r["date"], {"date": r["date"], "orders": 0, "lines": 0, "units": 0.0, "stops": 0, "trips": set()})
        if "trip" in r:
            e["trips"].add(r["trip"])
        e["orders"] += 1
        e["lines"] += r["lines"]
        e["units"] += r.get("units", r["lines"])
        e["stops"] += r["stops"]
        for k in ("list", "s", "opt") + BATCH_KEYS:
            if f"{k}_m" in r:
                e[f"{k}_m"] = e.get(f"{k}_m", 0.0) + r[f"{k}_m"]
            if f"{k}_t" in r:
                e[f"{k}_h"] = e.get(f"{k}_h", 0.0) + r[f"{k}_t"] / 3600
    out = sorted(days.values(), key=lambda e: e["date"], reverse=True)
    for e in out:
        e["gain_pct"] = (1 - e["opt_m"] / e["list_m"]) * 100 if e.get("list_m") else None
        e["trips"] = len(e["trips"])
    return out


ROUTE_KINDS = (("list", N_("Lista de separação"), "#d62728"), ("s", N_("Serpentina (rota em S)"), "#2f6db5"),
               ("opt", N_("Otimizada"), "#1a9850"))
ROUTE_SHORT = {"list": N_("Lista"), "s": N_("Serpentina"), "opt": N_("Otimizada"), "batch": N_("Lote")}  # rótulo no 3D


def orders_payload(sample: list, nodes_of: dict, start: Node, ref: dict, d: Callable = dist) -> dict:
    """Lista de pedidos da amostra para a caminhada em 3D (maiores caminhadas na lista primeiro)."""
    sim = simulate(sample, nodes_of, start, d)
    rows = sorted(sim["rows"], key=lambda r: -r["list_m"])
    r2 = lambda v: round(v, 2) if v is not None else None  # noqa: E731
    return {"ref": ref, "kinds": [{"key": k, "name": _(n), "short": _(ROUTE_SHORT[k]), "color": c} for k, n, c in ROUTE_KINDS],
            "summary": {k: r2(sim.get(k)) for k in ("list_avg", "s_avg", "opt_avg", "gain_pct")} | {"orders": sim["orders"],
                                                                                                "has_qty": sim.get("has_qty", False),
                                                                                                "has_release": sim.get("has_release", False)},
            "orders": [{"order": r["order"], "lines": r["lines"], "stops": r["stops"], "list_m": r2(r["list_m"]),
                        "s_m": r2(r.get("s_m")), "opt_m": r2(r["opt_m"]), "date": r.get("date", "")} for r in rows],
            "days": [{k: (r2(v) if isinstance(v, float) else v) for k, v in e.items()} for e in by_day(sim)]}


def _same_day(sample: list, order: str) -> list:
    """Pedidos que podem ir no mesmo trajeto em lote: os do mesmo dia (sem data: a amostra toda)."""
    e = next((o for o in sample if o[0] == order), None)
    day = e[3] if e is not None and len(e) > 3 else None
    return [o for o in sample if (o[3] if len(o) > 3 else None) == day] if day else list(sample)


def _trip_route(trip: dict, entries: dict, nodes_of: dict, start: Node, d: Callable, near: dict) -> dict:
    """Rota de um trajeto em lote no formato das rotas do 3D: a rota do trajeto (melhor entre otimizada, serpentina e
    lista) com as peças, os SKUs e os itens fora da zona de ouro de TODOS os pedidos em cada parada."""
    units_at: dict[str, float] = {}
    out_at: dict[str, int] = {}
    outq_at: dict[str, float] = {}
    skus_at: dict[str, list] = {}
    for oid in trip["orders"]:
        e = entries.get(oid)
        if e is None:
            continue
        qty_of = dict(zip(e[1], e[2])) if len(e) > 2 and e[2] else {}
        for s_ in e[1]:
            if s_ in near:
                k = near[s_].key
                q = float(qty_of.get(s_, 1.0))
                units_at[k] = units_at.get(k, 0.0) + q
                out_at[k] = out_at.get(k, 0) + int(near[s_].gold is False)
                if near[s_].gold is False:
                    outq_at[k] = outq_at.get(k, 0.0) + q
                skus_at.setdefault(k, []).append(s_)
    route, groups = trip["route"], trip["groups"]
    idx: list = []
    pts = path_points(route, start, idx)
    grp = [groups.get(n.key, [n.key]) for n in route]
    return {"name": _("Lote — {n} pedidos no trajeto", n=len(trip["orders"])), "short": _(ROUTE_SHORT["batch"]), "color": BATCH_COLOR,
            "pts": [[round(x, 1), round(y, 1)] for x, y in pts], "stops": idx[:-1], "keys": [n.key for n in route],
            "groups": grp, "skus": [", ".join(x for k in g for x in skus_at.get(k, [])) for g in grp],
            "qty": [round(sum(units_at.get(k, 0.0) for k in g), 3) for g in grp],
            "out": [sum(out_at.get(k, 0) for k in g) for g in grp],
            "outq": [round(sum(outq_at.get(k, 0.0) for k in g), 3) for g in grp],
            "m": round(trip["bopt_m"], 2), "orders": list(trip["orders"])}


def order_payload(sample: list, nodes_of: dict, start: Node, ref: dict, order: str, d: Callable = dist,
                  batch: int = 0, release: bool = False, tp: Optional[dict] = None) -> dict:
    """As rotas de um pedido: pontos (cm, planta), índice de cada parada em pts, endereços, SKUs e a distância. Com
    `batch` >= 2, também "batch": o trajeto em lote em que o pedido caiu (com os outros pedidos do mesmo dia)."""
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
        routes[kind] = {"name": _(name), "short": _(ROUTE_SHORT[kind]), "color": color, "pts": [[round(x, 1), round(y, 1)] for x, y in pts],
                        "stops": idx[:-1], "keys": [n.key for n in rt[kind]], "groups": grp,
                        "skus": [", ".join(x for k in g for x in skus_at.get(k, [sku_of.get(k, "")])) for g in grp],
                        "qty": [round(sum(units_at.get(k, 1.0) for k in g), 3) for g in grp],
                        "out": [sum(out_at.get(k, 0) for k in g) for g in grp],
                        "outq": [round(sum(outq_at.get(k, 0.0) for k in g), 3) for g in grp],
                        "m": round(row[f"{kind}_m"], 2)}  # a distância da simulação (pelos corredores)
    if batch and batch >= 2:
        pool = _same_day(sample, order)
        trip = trip_of(simulate(pool, nodes_of, start, d, batch=batch, release=release, tp=tp or time_params({})), order)
        if trip:
            routes["batch"] = _trip_route(trip, {e[0]: e for e in pool}, nodes_of, start, d, near)
    return {"order": order, "lines": row["lines"], "stops": row["stops"], "units": row["units"], "has_qty": bool(qty_of),
            "out_gold": row["out_gold"], "has_gold": any(n.gold is not None for ns in nodes_of.values() for n in ns),
            "ref": ref, "routes": routes}


def day_payload(sample: list, nodes_of: dict, start: Node, ref: dict, day: str, d: Callable = dist,
                batch: int = 0, release: bool = False, tp: Optional[dict] = None) -> dict:
    """Um dia inteiro para a caminhada em 3D: os pedidos do dia, um depois do outro (cada um sai da referência e
    volta a ela), no mesmo formato de order_payload. Cada rota traz também `ord` (índice do pedido de cada parada) e
    `starts` (índice em pts onde começa cada pedido); `orders` lista os pedidos na ordem feita."""
    entries = [o for o in sample if len(o) > 3 and o[3] == day]
    if not entries:
        return {"error": _("Nenhum pedido do dia {day} na amostra.", day=day)}
    done, routes, tot = [], {}, {"lines": 0, "stops": 0, "units": 0.0, "out_gold": 0}
    has_qty = has_gold = False
    release = bool(release and any(len(e) > 4 for e in entries))
    rels = [e[4] for e in entries if len(e) > 4 and e[4] is not None]
    day0 = min(rels) if rels else 0  # início do dia: o 1º horário de liberação
    rel0 = {e[0]: ((e[4] - day0) if len(e) > 4 and e[4] is not None else 0) for e in entries}  # s desde o início
    order_key = (lambda o: (rel0[o[0]], o[0])) if release else (lambda o: o[0])  # com liberação: na ordem liberada
    for e in sorted(entries, key=order_key):
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
            if release:
                m.setdefault("rel", []).extend([rel0[e[0]]] * len(r["keys"]))
            m["m"] = round(m["m"] + r["m"], 2)
    if not done:
        return {"error": _("Os SKUs dos pedidos do dia {day} não estão na planta.", day=day)}
    if batch and batch >= 2:  # lote: os trajetos do dia, um depois do outro
        sim_b = simulate(entries, nodes_of, start, d, batch=batch, release=release, tp=tp or time_params({}))
        near = {s: min(ns, key=lambda n: d(start, n)) for s, ns in nodes_of.items() if ns}
        by_id = {e[0]: e for e in entries}
        pos = {o["order"]: i for i, o in enumerate(done)}
        m = None
        for t in sim_b.get("trips") or []:
            r = _trip_route(t, by_id, nodes_of, start, d, near)
            if m is None:
                m = {**{k: r[k] for k in ("name", "color")}, "pts": [], "stops": [], "keys": [], "groups": [], "skus": [],
                     "qty": [], "out": [], "outq": [], "m": 0.0, "ord": [], "starts": [], "trips": []}
            base = len(m["pts"])
            m["starts"].append(base)
            m["pts"] += r["pts"]
            m["stops"] += [base + i for i in r["stops"]]
            for f in ("keys", "groups", "skus", "qty", "out", "outq"):
                m[f] += r[f]
            m["ord"] += [min(pos.get(o, 0) for o in t["orders"])] * len(r["keys"])
            m.setdefault("trip", []).extend([len(m["trips"])] * len(r["keys"]))  # trajeto de cada parada
            if release:  # o trajeto só sai com todos os pedidos dele liberados (e depois da espera para completar o lote)
                last = max(t["orders"], key=lambda o: rel0.get(o, 0))
                go = max(rel0.get(last, 0), t.get("start_s") or 0)
                m.setdefault("rel", []).extend([go] * len(r["keys"]))
                # por que o trajeto espera: o pedido liberado por último ou a espera para completar o lote
                m.setdefault("trip_why", []).append("" if go > rel0.get(last, 0) + 1e-9 else last)
            m["m"] = round(m["m"] + r["m"], 2)
            m["trips"].append(t["orders"])
        if m is not None:
            m["name"] = _("Lote — {n} trajetos", n=len(m["trips"]))
            routes["batch"] = m
    # relógio do vídeo: começa no 1º horário de liberação do dia (com a coluna, mesmo com a opção desligada) ou, sem
    # horário nenhum, às 8h (presumido)
    return {"day": day, "order": day, "orders": done, "release": release, "start_s": day0 if release else None,
            "clock_s": day0 if rels else 8 * 3600, "clock_guess": not rels, "lines": tot["lines"], "stops": tot["stops"],
            "units": round(tot["units"], 3), "out_gold": tot["out_gold"], "has_qty": has_qty, "has_gold": has_gold,
            "ref": ref, "routes": routes}
