"""
Rede de caminhada da planta: por onde o separador pode andar sem atravessar estruturas nem obstáculos.

Antes, a troca de corredor e a ida/volta à referência eram calculadas em L (distância de Manhattan), e o trajeto
desenhado podia cortar estantes. Aqui o piso livre vira um grafo:
  - pontos: a referência, as saídas de cada corredor (pontas e passagens) e os cantos de cada bloco de estantes ou
    obstáculo, afastados CLEAR cm para fora (é por eles que o separador contorna os blocos);
  - arestas: entre dois pontos que se enxergam (o segmento não cruza nenhum bloco) e, dentro de cada corredor, entre
    saídas vizinhas pelo eixo;
  - blocos: as fileiras (e as faixas entre fileiras com vão menor que MIN_PASSAGE, que não servem de passagem), os
    obstáculos e, nos corredores de mão única, o próprio corredor (só se anda nele pelo eixo, no sentido permitido).
A distância entre duas paradas é o menor caminho (Dijkstra, calculado sob demanda e guardado por ponto de saída).
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

CLEAR = 30.0  # afastamento dos pontos de contorno a partir do canto dos blocos (cm)
MIN_PASSAGE = 80.0  # vão mínimo entre blocos para passar (cm); menos que isso, o vão é fechado
SHRINK = 1.0  # os blocos encolhem 1 cm no teste de visada (andar rente à face é permitido)
INF = float("inf")


@dataclass
class CorridorSpec:
    """Corredor da rede: saídas (posições ao longo do eixo), eixo -> ponto da planta e sentido permitido.
    flow: 0 = dois sentidos; +1 = só no sentido crescente do eixo; -1 = só no decrescente.
    covered: trechos (ao longo) com estantes dos lados — num corredor de mão única viram bloqueio fora do eixo."""
    ends: tuple
    to_xy: Callable[[float], tuple[float, float]]
    flow: int = 0
    covered: list = field(default_factory=list)
    half_width: float = 0.0
    orient: str = "h"  # "h": eixo ao longo de X; "v": ao longo de Y (só para o bloqueio da mão única)
    center: float = 0.0


def _fillers(rects: list[tuple]) -> list[tuple]:
    """Faixas entre blocos vizinhos com vão menor que MIN_PASSAGE (costas com costas, encostados…): fecham o vão."""
    out = []
    n = len(rects)
    for i in range(n):
        x0, y0, w0, d0 = rects[i]
        for j in range(i + 1, n):
            x1, y1, w1, d1 = rects[j]
            ox0, ox1 = max(x0, x1), min(x0 + w0, x1 + w1)  # sobreposição em X
            oy0, oy1 = max(y0, y1), min(y0 + d0, y1 + d1)
            if ox1 - ox0 > 0:
                lo, hi = (y0 + d0, y1) if y0 + d0 <= y1 else (y1 + d1, y0)
                if 0 <= hi - lo < MIN_PASSAGE and hi >= lo:
                    out.append((ox0, lo - 2 * SHRINK, ox1 - ox0, hi - lo + 4 * SHRINK))
            if oy1 - oy0 > 0:
                lo, hi = (x0 + w0, x1) if x0 + w0 <= x1 else (x1 + w1, x0)
                if 0 <= hi - lo < MIN_PASSAGE and hi >= lo:
                    out.append((lo - 2 * SHRINK, oy0, hi - lo + 4 * SHRINK, oy1 - oy0))
    return out


class WalkNet:
    def __init__(self, blocks: list[tuple], corridors: dict, ref: tuple[float, float], bounds: tuple[float, float],
                 extra: Optional[list[tuple[float, float]]] = None):
        """blocks: retângulos (x, y, w, d) em cm; corridors: {id: CorridorSpec}; ref: ponto de referência;
        bounds: (L, W) do imóvel (pontos de contorno fora dele são descartados); extra: pontos soltos (paradas fora
        de corredor, como as cabeceiras)."""
        self.corridors = corridors
        L, W = bounds
        rects = [b for b in blocks if b[2] > 0 and b[3] > 0]
        solid = rects + _fillers(rects)
        # mão única: o corredor também bloqueia (fora do eixo), nos trechos com estantes
        for c in corridors.values():
            if c.flow and c.half_width > 0:
                for a0, a1 in c.covered:
                    if c.orient == "h":
                        solid.append((a0, c.center - c.half_width, a1 - a0, 2 * c.half_width))
                    else:
                        solid.append((c.center - c.half_width, a0, 2 * c.half_width, a1 - a0))
        self.blocks = solid
        bx = np.array([[b[0] + SHRINK, b[1] + SHRINK, b[0] + b[2] - SHRINK, b[1] + b[3] - SHRINK] for b in solid]
                      or [[0, 0, -1, -1]], dtype=float)
        self._bx = bx
        pts: list[tuple[float, float]] = [tuple(ref)]
        self.ref_pid = 0
        self.end_pid: dict[tuple, int] = {}
        for cid, c in corridors.items():
            for e in c.ends:
                self.end_pid[(cid, e)] = len(pts)
                pts.append(tuple(c.to_xy(e)))
        self.extra_pid: dict[tuple, int] = {}
        for p in extra or []:
            key = (round(p[0], 1), round(p[1], 1))
            if key not in self.extra_pid:
                self.extra_pid[key] = len(pts)
                pts.append(key)
        fixed = len(pts)
        for x, y, w, d in rects:
            for cx, cy in ((x - CLEAR, y - CLEAR), (x + w + CLEAR, y - CLEAR), (x - CLEAR, y + d + CLEAR),
                           (x + w + CLEAR, y + d + CLEAR)):
                if -1 <= cx <= L + 1 and -1 <= cy <= W + 1:
                    pts.append((cx, cy))
        P = np.array(pts, dtype=float)
        # cantos dentro de algum bloco não servem
        inside = np.zeros(len(P), dtype=bool)
        inside[fixed:] = ((P[fixed:, None, 0] > bx[None, :, 0]) & (P[fixed:, None, 0] < bx[None, :, 2])
                          & (P[fixed:, None, 1] > bx[None, :, 1]) & (P[fixed:, None, 1] < bx[None, :, 3])).any(axis=1)
        keep = [i for i in range(len(P)) if not inside[i]]
        remap = {old: new for new, old in enumerate(keep)}
        self.pts = [pts[i] for i in keep]
        self.ref_pid = remap[self.ref_pid]
        self.end_pid = {k: remap[v] for k, v in self.end_pid.items()}
        self.extra_pid = {k: remap[v] for k, v in self.extra_pid.items()}
        self.adj: list[list[tuple[int, float]]] = [[] for _ in self.pts]
        self._visibility()
        for cid, c in corridors.items():  # pelo eixo do corredor, entre saídas vizinhas
            es = sorted(c.ends)
            for a, b in zip(es, es[1:]):
                pa, pb = self.end_pid[(cid, a)], self.end_pid[(cid, b)]
                if c.flow >= 0:
                    self.adj[pa].append((pb, b - a))
                if c.flow <= 0:
                    self.adj[pb].append((pa, b - a))
        self._sp: dict[int, tuple[list, list]] = {}
        self._memo: dict[tuple, float] = {}

    # ------------------------------------------------------------------ grafo
    def _visibility(self):
        P = np.array(self.pts, dtype=float)
        n = len(P)
        bx = self._bx
        x0, y0, x1, y1 = (bx[:, k][None, :] for k in range(4))
        linked = np.zeros(n, dtype=bool)
        for i in range(n - 1):
            Q = P[i + 1:]
            dx = (Q[:, 0] - P[i, 0])[:, None]
            dy = (Q[:, 1] - P[i, 1])[:, None]
            with np.errstate(divide="ignore", invalid="ignore"):
                tx1, tx2 = (x0 - P[i, 0]) / dx, (x1 - P[i, 0]) / dx
                ty1, ty2 = (y0 - P[i, 1]) / dy, (y1 - P[i, 1]) / dy
            inx = (P[i, 0] > x0) & (P[i, 0] < x1)  # segmento vertical: dentro da faixa em X?
            iny = (P[i, 1] > y0) & (P[i, 1] < y1)
            zx, zy = dx == 0, dy == 0
            txmin = np.where(zx, np.where(inx, -INF, INF), np.minimum(tx1, tx2))
            txmax = np.where(zx, np.where(inx, INF, -INF), np.maximum(tx1, tx2))
            tymin = np.where(zy, np.where(iny, -INF, INF), np.minimum(ty1, ty2))
            tymax = np.where(zy, np.where(iny, INF, -INF), np.maximum(ty1, ty2))
            enter = np.maximum(np.maximum(txmin, tymin), 0.0)
            leave = np.minimum(np.minimum(txmax, tymax), 1.0)
            blocked = (enter < leave - 1e-9).any(axis=1)
            for k in np.nonzero(~blocked)[0]:
                j = i + 1 + int(k)
                w = math.hypot(P[j, 0] - P[i, 0], P[j, 1] - P[i, 1])
                self.adj[i].append((j, w))
                self.adj[j].append((i, w))
                linked[i] = linked[j] = True
        # ponto isolado (referência dentro de um bloco, por exemplo): liga aos 3 mais perto, para não ficar sem caminho
        for i in np.nonzero(~linked)[0]:
            d = np.hypot(P[:, 0] - P[i, 0], P[:, 1] - P[i, 1])
            d[i] = INF
            for j in np.argsort(d)[:3]:
                if math.isfinite(d[j]):
                    self.adj[i].append((int(j), float(d[j])))
                    self.adj[int(j)].append((int(i), float(d[j])))

    def _paths(self, src: int) -> tuple[list, list]:
        hit = self._sp.get(src)
        if hit is not None:
            return hit
        dist = [INF] * len(self.pts)
        prev = [-1] * len(self.pts)
        dist[src] = 0.0
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            for v, w in self.adj[u]:
                nd = d + w
                if nd < dist[v] - 1e-9:
                    dist[v], prev[v] = nd, u
                    heapq.heappush(heap, (nd, v))
        self._sp[src] = (dist, prev)
        return dist, prev

    # ------------------------------------------------------------------ paradas
    def _corr(self, n):
        return self.corridors.get(n.corridor) if getattr(n, "corridor", None) is not None else None

    def _exits(self, n, leaving: bool) -> list[tuple[int, float]]:
        """(ponto, custo) para sair da parada (leaving) ou chegar a ela, respeitando o sentido do corredor."""
        c = self._corr(n)
        if c is None:
            if n.key == "REF":
                return [(self.ref_pid, 0.0)]
            pid = self.extra_pid.get((round(n.gx, 1), round(n.gy, 1)))
            return [(pid, 0.0)] if pid is not None else []
        out = []
        for e in c.ends:
            ok = c.flow == 0 or (c.flow > 0 and (e >= n.along - 1e-6 if leaving else e <= n.along + 1e-6)) \
                or (c.flow < 0 and (e <= n.along + 1e-6 if leaving else e >= n.along - 1e-6))
            if ok:
                out.append((self.end_pid[(n.corridor, e)], abs(n.along - e)))
        return out

    def _direct(self, a, b) -> Optional[float]:
        c = self._corr(a)
        if c is None or a.corridor != getattr(b, "corridor", None):
            return None
        d = b.along - a.along
        if c.flow == 0 or d * c.flow >= -1e-6:
            return abs(d)
        return None

    def _best(self, a, b):
        best = (INF, None, None)
        direct = self._direct(a, b)
        if direct is not None:
            best = (direct, None, None)
        for pa, ca in self._exits(a, True):
            dist, _prev = self._paths(pa)
            for pb, cb in self._exits(b, False):
                v = ca + dist[pb] + cb
                if v < best[0] - 1e-9:
                    best = (v, pa, pb)
        return best

    def dist(self, a, b) -> float:
        if a.key == b.key and a.key != "":
            return 0.0
        # a chave inclui a posição da parada: a mesma rede serve plantas com as estantes em outra ordem (numeração
        # invertida, outro lado/corredor na fileira), onde o mesmo código fica em outro lugar
        k = (a.key, a.corridor, round(a.along, 1), round(a.gx), round(a.gy),
             b.key, b.corridor, round(b.along, 1), round(b.gx), round(b.gy))
        v = self._memo.get(k)
        if v is None:
            v = self._best(a, b)[0]
            if not math.isfinite(v):  # sem caminho (planta inconsistente): em L, como antes
                v = abs(a.gx - b.gx) + abs(a.gy - b.gy)
            if len(self._memo) > 400_000:
                self._memo.clear()
            self._memo[k] = v
        return v

    def center(self, n) -> tuple[float, float]:
        c = self._corr(n)
        return tuple(c.to_xy(n.along)) if c is not None else (n.gx, n.gy)

    def path(self, a, b) -> list[tuple[float, float]]:
        """Pontos (cm) de a até b: sai da parada para o eixo do corredor, segue até a saída, contorna os blocos pela
        rede e entra no corredor de b (o último ponto é a parada b)."""
        v, pa, pb = self._best(a, b)
        out = []
        if self._corr(a) is not None:
            out.append(self.center(a))
        if math.isfinite(v) and pa is not None:
            _d, prev = self._paths(pa)
            chain, u = [], pb
            while u != -1:
                chain.append(self.pts[u])
                if u == pa:
                    break
                u = prev[u]
            out += chain[::-1]
        elif not math.isfinite(v):
            out.append((b.gx, a.gy))  # sem caminho: em L
        if self._corr(b) is not None:
            out.append(self.center(b))
        out.append((b.gx, b.gy))
        return out


# ---------------------------------------------------------------------------
# Rede do projeto de filial nova (plano do site_planner)
# ---------------------------------------------------------------------------
def plan_net(plan, nodes_of: dict, start) -> WalkNet:
    """Monta a rede a partir do plano (módulos agrupados em blocos por trecho de fileira, obstáculos) e liga as
    paradas (route_sim.Node) e a referência a ela."""
    groups: dict[tuple, list] = {}
    blocks = []
    for m in plan.modules:
        if m.rotated or m.zone is None:
            blocks.append(m.grect)
            continue
        groups.setdefault((m.zone.index, m.row.index, m.segment), []).append(m)
    for ms in groups.values():  # trechos contínuos da fileira viram um bloco só
        ms.sort(key=lambda m: m.x0)
        run = [ms[0]]
        for m in ms[1:] + [None]:
            if m is not None and m.x0 - (run[-1].x0 + run[-1].struct.width) < 1.0:
                run.append(m)
                continue
            zn = run[0].zone
            x0, x1 = run[0].x0, run[-1].x0 + run[-1].struct.width
            blocks.append(zn.to_global(x0, run[0].row.y, x1 - x0, run[0].row.depth))
            run = [m] if m is not None else []
    for o in getattr(plan.floor, "obstacles", []) or []:
        if getattr(o, "kind", "obstacle") == "obstacle":
            blocks.append((o.x_m * 100, o.y_m * 100, o.length_m * 100, o.width_m * 100))
    corridors: dict = {}
    extra = []
    for ns in nodes_of.values():
        for n in ns:
            if n.aisle >= 0 and n.to_global is not None:
                cid = (n.zone, n.aisle)
                n.corridor = cid
                if cid not in corridors:
                    tg, cross = n.to_global, n.cross
                    corridors[cid] = CorridorSpec(ends=tuple(n.ends), to_xy=(lambda e, tg=tg, cr=cross: tg(e, cr, 0, 0)[:2]))
            else:
                extra.append((n.gx, n.gy))
    net = WalkNet(blocks, corridors, (start.gx, start.gy), (plan.floor.length_cm, plan.floor.width_cm), extra)
    for ns in nodes_of.values():
        for n in ns:
            n.net = net
    start.net = net
    return net
