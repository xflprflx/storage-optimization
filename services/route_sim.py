"""
Simulador de trajeto de separação a partir dos pedidos (amostra guardada na importação da aba Pedidos).

Para cada pedido: sai do ponto de referência (expedição), passa em uma posição de cada SKU e volta. Duas rotas:
  - "lista": na ordem dos endereços (como a lista de separação costuma vir impressa) — mostra o efeito da numeração
    das ruas (por exemplo, a serpentina);
  - "otimizada": vizinho mais próximo + melhorias 2-opt — o melhor caminho para aquele pedido.

Distância pelos corredores (projeto de filial nova, onde há a planta):
  - no mesmo corredor: ao longo dele;
  - corredores diferentes da mesma zona: sai pela ponta do corredor ou por um transversal (o que der menos) e entra
    no outro;
  - zonas diferentes, cabeceiras e o ponto de referência: em L (distância de Manhattan) até a ponta usada.
Nas filiais existentes (sem planta), a distância é a estimada pelo endereço (services/slotting.geo_dist).
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Callable, Optional

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


def _natural(v: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v or "")]


def plan_nodes(plan) -> tuple[dict[str, list[Node]], dict]:
    """SKU -> pontos de parada (um por posição do plano) e o ponto de referência."""
    from services.site_planner import access_point

    ref = (plan.slotting or {}).get("ref") or {"x": 0.0, "y": plan.floor.width_cm / 2, "label": "frente"}
    out: dict[str, list[Node]] = {}
    for m in plan.modules:
        gx, gy = access_point(m)
        zn = m.zone
        if m.rotated or zn is None or not zn.aisles:
            node = dict(gx=gx, gy=gy)
        else:
            y, w = zn.aisles[m.row.aisle]
            ends = sorted({zn.x_start, zn.x_end} | {v for seg in zn.segments for v in seg})
            node = dict(gx=gx, gy=gy, zone=zn.index, aisle=m.row.aisle, along=m.x0 + m.struct.width / 2,
                        cross=y + w / 2 if w > 0 else (m.row.y + m.row.depth if m.row.side == "L" else m.row.y),
                        ends=tuple(ends), to_global=zn.to_global)
        for lv in m.levels:
            for p in lv.positions:
                out.setdefault(p.sku, []).append(Node(key=p.address, **node))
    return out, ref


def _glob(n: Node, e: float) -> tuple[float, float]:
    x, y, _w, _d = n.to_global(e, n.cross, 0, 0)
    return x, y


def dist(a: Node, b: Node) -> float:
    """cm, pelos corredores."""
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
    while improved:
        improved = False
        pts = [start] + route + [start]
        for i in range(1, len(pts) - 2):
            for j in range(i + 1, len(pts) - 1):
                delta = (d(pts[i - 1], pts[j]) + d(pts[i], pts[j + 1])) - (d(pts[i - 1], pts[i]) + d(pts[j], pts[j + 1]))
                if delta < -1e-6:
                    pts[i:j + 1] = reversed(pts[i:j + 1])
                    improved = True
        route = pts[1:-1]
    return route


def _s_shape(nodes: list[Node], start: Node, d: Callable) -> list[Node]:
    """Rota em S (serpentina): os corredores com paradas em ordem, alternando o sentido (ida num, volta no
    seguinte); paradas fora de corredor (cabeceiras) entram onde aumentam menos o trajeto."""
    corr: dict[tuple, list] = {}
    loose = []
    for n in nodes:
        if n.aisle >= 0:
            corr.setdefault((n.zone, n.cross), []).append(n)
        else:
            loose.append(n)
    route = []
    for i, k in enumerate(sorted(corr)):
        route += sorted(corr[k], key=lambda n: n.along, reverse=bool(i % 2))
    for n in loose:  # inserção mais barata
        pts = [start] + route + [start]
        j = min(range(1, len(pts)), key=lambda j: d(pts[j - 1], n) + d(n, pts[j]) - d(pts[j - 1], pts[j]))
        route.insert(j - 1, n)
    return route


def simulate(sample: list, nodes_of: dict[str, list[Node]], start: Node, d: Callable = dist,
             unit: float = 100.0, keep_routes: Optional[set] = None, s_shape: bool = True) -> dict:
    """Simula a amostra de pedidos. Cada SKU usa a sua posição mais perto da referência. `unit` = divisor para
    metros (100 para cm). Retorna indicadores e, por pedido, as duas distâncias (e as rotas pedidas)."""
    near = {s: min(ns, key=lambda n: d(start, n)) for s, ns in nodes_of.items() if ns}
    rows, skipped_lines, routes = [], 0, {}
    for oid, skus in sample:
        pts = [near[s] for s in skus if s in near]
        skipped_lines += len(skus) - len(pts)
        if not pts:
            continue
        uniq = list({n.key: n for n in pts}.values())  # o mesmo endereço uma vez
        by_list = sorted(uniq, key=lambda n: _natural(n.key))
        opt = _optimized(uniq, start, d)
        s_route = _s_shape(uniq, start, d) if s_shape else None
        # "otimizada" = a melhor das rotas calculadas (a heurística não garante ser melhor que a em S)
        opt = min([r for r in (opt, s_route, by_list) if r is not None], key=lambda r: _tour(r, start, d))
        dl, do = _tour(by_list, start, d) / unit, _tour(opt, start, d) / unit
        row = {"order": oid, "lines": len(skus), "stops": len(uniq), "list_m": dl, "opt_m": do}
        if s_route is not None:
            row["s_m"] = _tour(s_route, start, d) / unit
        rows.append(row)
        if keep_routes is not None and oid in keep_routes:
            routes[oid] = {"list": by_list, "opt": opt, "s": s_route}
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
            "lines": lines, "list_avg": statistics.mean(L), "opt_avg": statistics.mean(O),
            "list_per_line": sum(L) / lines, "opt_per_line": sum(O) / lines,
            "list_p50": q(L, 0.5), "list_p90": q(L, 0.9), "opt_p50": q(O, 0.5), "opt_p90": q(O, 0.9),
            "list_total": sum(L), "opt_total": sum(O),
            "gain_pct": (1 - sum(O) / sum(L)) * 100 if sum(L) else None}


def path_points(route: list[Node], start: Node) -> list[tuple[float, float]]:
    """Pontos (cm, planta) do trajeto para desenhar: pelos corredores (sai pela ponta usada) e em L entre eles."""
    pts = [(start.gx, start.gy)]
    seq = [start] + route + [start]
    for a, b in zip(seq, seq[1:]):
        if a.aisle >= 0 and b.aisle >= 0 and a.zone == b.zone and a.aisle == b.aisle:
            pts += [_glob(a, a.along), _glob(b, b.along), (b.gx, b.gy)]  # pelo meio do corredor
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
    return pts
