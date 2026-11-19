"""
Portas das estruturas (câmara refrigerada, armário, gaiola): a área que a porta de giro varre ao abrir, para não
bater em outra fileira, obstáculo, coluna, parede ou na referência.

A porta fica na frente do módulo (o lado do corredor). Com 1 folha, a folha tem a frente inteira e gira na dobradiça
(esquerda ou direita, vista de frente); com 2 folhas, cada uma tem metade da frente e gira na sua ponta. A área de
cada folha é um setor de círculo com raio = largura da folha, de 0° (fechada, rente à frente) até a abertura máxima
(90° ou 180°), do lado do corredor. Porta de correr não ocupa o corredor.

Convenção de frente e lado (a mesma do 3D, static/look3d.js `place`): no módulo (x, y, w, d) da planta, `fx` = a
frente corre ao longo de X; `ff` = o corredor fica do lado maior (y+d ou x+d).
"""
from __future__ import annotations

import math
from typing import Optional

STEP_CM = 10.0  # amostragem do setor para a checagem (cm)


def door_of(kind) -> Optional[dict]:
    """Porta do tipo de estrutura: None, {"type": "sliding", "time"} ou {"type": "swing", leaves, hinge, angle, time}."""
    if kind is None or not getattr(kind, "door_type", ""):
        return None
    t = float(kind.door_time_s) if kind.door_time_s is not None else 0.0
    if kind.door_type == "sliding":
        return {"type": "sliding", "time": t}
    return {"type": "swing", "leaves": 2 if kind.door_leaves == 2 else 1, "hinge": kind.door_hinge or "left",
            "angle": 180 if kind.door_angle == 180 else 90, "time": t}


def frame(x: float, y: float, w: float, d: float, fx: bool, ff: bool) -> tuple:
    """(origem = canto esquerdo da frente, eixo ao longo da frente, eixo para dentro do módulo, frente, profundidade):
    o lado do corredor é o oposto do eixo para dentro."""
    if fx:
        W, D = w, d
        if not ff:
            return (x, y), (1.0, 0.0), (0.0, 1.0), W, D
        return (x + w, y + d), (-1.0, 0.0), (0.0, -1.0), W, D
    W, D = d, w
    if not ff:
        return (x, y + d), (0.0, -1.0), (1.0, 0.0), W, D
    return (x + w, y), (0.0, 1.0), (-1.0, 0.0), W, D


def leaves(door: dict, W: float) -> list[tuple[float, int, float]]:
    """[(posição da dobradiça ao longo da frente, sentido da folha fechada: +1 = para a direita, raio)]."""
    if door.get("type") != "swing" or W <= 0:
        return []
    if door["leaves"] == 2:
        return [(0.0, 1, W / 2), (W, -1, W / 2)]
    return [(0.0, 1, W)] if door["hinge"] == "left" else [(W, -1, W)]


def sectors(x: float, y: float, w: float, d: float, fx: bool, ff: bool, door: dict, steps: int = 12) -> list[list]:
    """Polígonos (lista de pontos da planta) da área varrida por cada folha: dobradiça + o arco."""
    (ox, oy), eu, et, W, _D = frame(x, y, w, d, fx, ff)
    out = []
    for a0, sgn, r in leaves(door, W):
        pts = [(ox + eu[0] * a0, oy + eu[1] * a0)]
        for i in range(steps + 1):
            phi = math.radians(door["angle"]) * i / steps
            a, b = a0 + sgn * r * math.cos(phi), -r * math.sin(phi)  # b < 0 = no corredor
            pts.append((ox + eu[0] * a + et[0] * b, oy + eu[1] * a + et[1] * b))
        out.append(pts)
    return out


def sample(x: float, y: float, w: float, d: float, fx: bool, ff: bool, door: dict) -> list[tuple[float, float]]:
    """Pontos dentro da área varrida (para a checagem de colisão)."""
    (ox, oy), eu, et, W, _D = frame(x, y, w, d, fx, ff)
    pts = []
    for a0, sgn, r in leaves(door, W):
        n_r = max(1, int(r // STEP_CM))
        for i in range(1, n_r + 1):
            rr = r * i / n_r
            n_a = max(2, int(math.radians(door["angle"]) * rr // STEP_CM) + 1)
            for j in range(n_a + 1):
                phi = math.radians(door["angle"]) * j / n_a
                a, b = a0 + sgn * rr * math.cos(phi), -rr * math.sin(phi)
                if b > -1.0:
                    continue  # rente à frente: é a própria estrutura
                pts.append((ox + eu[0] * a + et[0] * b, oy + eu[1] * a + et[1] * b))
    return pts


def _inside(p, rect, eps: float = 1.0) -> bool:
    x, y, w, h = rect
    return x + eps < p[0] < x + w - eps and y + eps < p[1] < y + h - eps


def hits(pts: list, rects: list[tuple], bounds: Optional[tuple] = None) -> Optional[str]:
    """Primeiro obstáculo que a porta toca: rects = [(x, y, w, h, rótulo)]; bounds = (L, W) do imóvel (parede)."""
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    for x, y, w, h, label in rects:
        if x > bx1 or x + w < bx0 or y > by1 or y + h < by0:
            continue
        if any(_inside(p, (x, y, w, h)) for p in pts):
            return label
    if bounds is not None:
        L, Wd = bounds
        if bx0 < -1 or by0 < -1 or bx1 > L + 1 or by1 > Wd + 1:
            return "@wall"
    return None
