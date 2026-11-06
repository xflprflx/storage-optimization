"""
Arranjo das unidades dentro de uma bin: orientação de cada unidade, grade (por camada × camadas) e a posição
de cada unidade, para o desenho na tela (vista de cima + vista de frente) e para a visualização 3D.

Convenção de eixos da bin: x = largura, y = altura (vertical), z = comprimento (profundidade).
A orientação é a mesma usada no cálculo de capacidade da otimização: todas as unidades de um SKU numa bin ficam na
mesma orientação, em grade. Com vários SKUs na mesma bin, o comprimento da bin é dividido entre eles na proporção
do espaço que cada um precisa (aproximação).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Optional

EPS = 1e-9
PALETTE = ["#2f7ed8", "#e8743b", "#19a979", "#ed4a7b", "#945ecf", "#13a4b4", "#c3a32c", "#6f7c87"]
DIM_NAMES = ("altura", "largura", "comprimento")


@dataclass
class ItemArrangement:
    sku: str
    qty: int  # unidades físicas nesta bin
    dims: tuple  # (altura, largura, comprimento) do produto, cm
    oriented: Optional[tuple] = None  # (vertical, ao longo da largura, ao longo do comprimento) da bin
    vertical: str = ""  # qual medida do produto fica na vertical
    nx: int = 0  # unidades ao longo da largura da bin
    nz: int = 0  # ao longo do comprimento
    ny: int = 0  # camadas
    per_bin: int = 0  # capacidade em grade (bin só deste SKU, ou o espaço reservado a ele)
    placed: int = 0  # unidades desenhadas
    overflow: int = 0  # unidades que não cabem no espaço geométrico
    color: str = PALETTE[0]
    region: tuple = (0.0, 0.0)  # (z inicial, z final) reservado na bin
    boxes: list = field(default_factory=list)  # [(x, y, z, dx, dy, dz)]
    reserved: int = 0  # espaço reservado (plano por min/max level acima do saldo): desenhado vazio, tracejado
    real: int = 0  # quantas das `boxes` são unidades físicas (as demais são reserva)

    def layers(self) -> list[tuple[int, float, float, int, int]]:
        """[(nº da camada a partir de baixo, y, altura, unidades físicas, posições de reserva)]"""
        by_y: dict[float, list] = {}
        for k, (x, y, z, dx, dy, dz) in enumerate(self.boxes):
            by_y.setdefault(y, [0, 0, dy])[0 if k < self.real else 1] += 1
        return [(i + 1, y, v[2], v[0], v[1]) for i, (y, v) in enumerate(sorted(by_y.items()))]

    def is_ghost(self, k: int) -> bool:
        return k >= self.real

    @property
    def per_layer(self) -> int:
        return self.nx * self.nz

    @property
    def layers_used(self) -> int:
        return math.ceil(self.placed / self.per_layer) if self.per_layer else 0

    @property
    def text(self) -> str:
        if not self.oriented:
            return "sem dimensões: orientação não calculada"
        oh, ow, ol = self.oriented
        if not self.per_bin:
            return "não cabe nesta bin com a rotação permitida"
        t = (f"{self.vertical} na vertical · base {_n(ow)}×{_n(ol)} cm · {self.nx}×{self.nz} por camada"
             f" · {self.layers_used} de {self.ny} camada(s)")
        if self.reserved:
            t += f" · + espaço reservado para mais {self.reserved} un. do plano"
        if self.overflow:
            t += f" · {self.overflow} un. não cabem"
        return t


@dataclass
class BinArrangement:
    code: str
    dims: Optional[tuple]  # (altura, largura, comprimento) internos, cm
    items: list
    approx: bool = False  # vários SKUs: divisão aproximada
    # compartimento (fração de uma bin pai): {"h","w","l" da pai, "x0","z0" do compartimento, "k", "axis", "label", "code"}
    parent: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.dims is not None and all(i.oriented for i in self.items)

    def as_json(self) -> dict:
        h, w, l_ = self.dims or (0, 0, 0)
        return {"code": self.code, "h": h, "w": w, "l": l_, "approx": self.approx, "parent": self.parent,
                "items": [{"sku": i.sku, "qty": i.qty, "color": i.color, "text": i.text, "boxes": i.boxes,
                           "layers": [list(x) for x in i.layers()], "real": i.real, "reserved": i.reserved,
                           "dims": i.dims, "oriented": i.oriented, "overflow": i.overflow} for i in self.items]}


def _n(v) -> str:
    return f"{v:g}".replace(".", ",")


def _orientations(dims: tuple, rotation: str):
    h, w, l_ = dims
    if rotation == "any":
        seen = []
        for perm in itertools.permutations(range(3)):
            o = tuple(dims[i] for i in perm)
            if o not in [s[0] for s in seen]:
                seen.append((o, perm[0]))
        return seen
    return [((h, w, l_), 0), ((h, l_, w), 0)]


def best_orientation(dims: tuple, box: tuple, rotation: str = "upright"):
    """Melhor orientação em grade dentro de um espaço (altura, largura, comprimento).
    Retorna (orientado, índice da medida vertical, nx, nz, ny, total)."""
    H, W, L = box
    best = None
    for o, vert in _orientations(dims, rotation):
        oh, ow, ol = o
        ny, nx, nz = (math.floor(H / oh + EPS), math.floor(W / ow + EPS), math.floor(L / ol + EPS))
        total = nx * ny * nz
        # empate: prefere menos camadas ocupadas (mais estável) e a orientação "em pé"
        key = (total, -vert)
        if best is None or key > best[0]:
            best = (key, o, vert, nx, nz, ny, total)
    _, o, vert, nx, nz, ny, total = best
    return o, vert, nx, nz, ny, total


def arrange(bin_code: str, bin_dims: Optional[tuple], items: list[tuple], rotation: str = "upright") -> BinArrangement:
    """items = [(sku, qty, (altura, largura, comprimento) | None[, reserva])]. Calcula orientação e posições.
    `reserva` = posições planejadas além das unidades físicas (desenhadas vazias)."""
    out = []
    for i, it in enumerate(items):
        sku, q, d = it[:3]
        res = int(math.ceil(float(it[3]) - EPS)) if len(it) > 3 and it[3] else 0
        out.append(ItemArrangement(sku, int(math.ceil(float(q) - EPS)), d or (None, None, None),
                                   color=PALETTE[i % len(PALETTE)], reserved=max(res, 0)))
    arr = BinArrangement(bin_code, bin_dims, out, approx=len(out) > 1)
    if not bin_dims or not all(bin_dims):
        return arr
    H, W, L = bin_dims
    # Um SKU após o outro ao longo do comprimento: cada um ocupa só as fileiras de que precisa
    z0 = 0.0
    for it in out:
        if None in it.dims or not all(it.dims):
            continue
        room = max(L - z0, 0.0)
        o, vert, nx, nz, ny, total = best_orientation(it.dims, (H, W, room), rotation)
        want = it.qty + it.reserved
        if len(out) > 1 and nx * ny:
            rows = math.ceil(want / (nx * ny))
            nz = min(nz, rows)
            total = nx * ny * nz
        it.oriented, it.vertical = o, DIM_NAMES[vert]
        it.nx, it.nz, it.ny, it.per_bin = nx, nz, ny, total
        it.placed = min(want, total)
        it.real = min(it.qty, it.placed)
        it.overflow = max(0, want - total)
        it.region = (z0, z0 + nz * o[2])
        oh, ow, ol = o
        # preenche camada por camada (de baixo para cima), linha por linha
        k = 0
        for iy in range(ny):
            for iz in range(nz):
                for ix in range(nx):
                    if k >= it.placed:
                        break
                    it.boxes.append((round(ix * ow, 3), round(iy * oh, 3), round(z0 + iz * ol, 3), ow, oh, ol))
                    k += 1
        z0 = it.region[1] if len(out) > 1 else L
    return arr


def svg_mini(arr: BinArrangement, size: int = 70) -> str:
    """Desenho: vista de cima (largura × comprimento, mostra a camada mais alta de cada coluna, com o nº dela) e
    vista de frente (largura × altura) com as camadas numeradas de baixo (1) para cima."""
    if not arr.dims or (not arr.items and not arr.parent):
        return ""
    H, W, L = arr.dims
    pr = arr.parent
    PH, PW, PL = (pr["h"], pr["w"], pr["l"]) if pr else (H, W, L)
    ox, oz = (pr["x0"], pr["z0"]) if pr else (0.0, 0.0)
    sc = size / max(PW, PL, PH)
    tw, tl, th = PW * sc, PL * sc, PH * sc
    fs = max(4.5, min(7.0 if size < 100 else 11.0, size / 10))
    num_w = fs * 1.6  # coluna dos números das camadas
    gap = 6
    fx = tw + gap + num_w
    width = fx + tw + 2
    height = max(tl, th) + fs + 5
    oy = fs + 3
    parts = [f'<svg class="nk-pack" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.1f} {height:.1f}">']
    parts.append(f'<text x="1" y="{fs:.1f}" font-size="{fs:.1f}" fill="#6b7780">cima</text>'
                 f'<text x="{fx + 1:.1f}" y="{fs:.1f}" font-size="{fs:.1f}" fill="#6b7780">frente · camadas</text>')
    parts.append(f'<rect x="0.5" y="{oy + .5:.1f}" width="{tw:.1f}" height="{tl:.1f}" fill="#f7f9fa" stroke="#6c7a84" stroke-width=".8"/>')
    parts.append(f'<rect x="{fx + .5:.1f}" y="{oy + .5:.1f}" width="{tw:.1f}" height="{th:.1f}" fill="#f7f9fa" stroke="#6c7a84" stroke-width=".8"/>')
    if pr:
        # bin pai com a grade de divisórias; os outros compartimentos ficam em cinza (não pertencem a esta bin)
        nx, nz = pr.get("nx", 1), pr.get("nz", 1)
        cw_, cl_ = PW / nx, PL / nz
        my_col = int(round(ox / cw_)) if cw_ else 0
        for c in range(nx):
            for r in range(nz):
                if abs(c * cw_ - ox) < 1e-6 and abs(r * cl_ - oz) < 1e-6:
                    continue
                parts.append(f'<rect x="{0.5 + c * cw_ * sc:.1f}" y="{oy + .5 + r * cl_ * sc:.1f}" width="{cw_ * sc:.1f}" '
                             f'height="{cl_ * sc:.1f}" fill="#e3e7ea"><title>outro compartimento {pr["label"]} da {pr["code"]}</title></rect>')
            if c != my_col:  # de frente, as outras colunas
                parts.append(f'<rect x="{fx + .5 + c * cw_ * sc:.1f}" y="{oy + .5:.1f}" width="{cw_ * sc:.1f}" height="{th:.1f}" fill="#e3e7ea"/>')
        for c in range(1, nx):
            xx = c * cw_ * sc
            parts.append(f'<line x1="{0.5 + xx:.1f}" y1="{oy + .5:.1f}" x2="{0.5 + xx:.1f}" y2="{oy + .5 + tl:.1f}" stroke="#3b5b8c" stroke-width=".9" stroke-dasharray="2 1.2"/>')
            parts.append(f'<line x1="{fx + .5 + xx:.1f}" y1="{oy + .5:.1f}" x2="{fx + .5 + xx:.1f}" y2="{oy + .5 + th:.1f}" stroke="#3b5b8c" stroke-width=".9" stroke-dasharray="2 1.2"/>')
        for r in range(1, nz):
            yy = oy + .5 + r * cl_ * sc
            parts.append(f'<line x1="0.5" y1="{yy:.1f}" x2="{0.5 + tw:.1f}" y2="{yy:.1f}" stroke="#3b5b8c" stroke-width=".9" stroke-dasharray="2 1.2"/>')
    numbered: set = set()
    for it in arr.items:
        layer_no = {y: n for n, y, *_ in it.layers()}
        top: dict = {}  # (x, z) -> (dx, dz, nº da camada mais alta, reserva?)
        front: dict = {}
        for k, (x, y, z, dx, dy, dz) in enumerate(it.boxes):
            n = layer_no.get(y, 0)
            g = it.is_ghost(k)
            if (x, z) not in top or (top[(x, z)][3] and not g) or (n > top[(x, z)][2] and not g):
                top[(x, z)] = (dx, dz, n, g)
            if (x, y) not in front or not g:
                front[(x, y)] = (dx, dy, g)
        for (x, z), (dx, dz, n, g) in top.items():
            cw, ch = max(dx * sc - .6, .6), max(dz * sc - .6, .6)
            if g:
                parts.append(f'<rect x="{0.5 + (x + ox) * sc:.1f}" y="{oy + .5 + (z + oz) * sc:.1f}" width="{cw:.1f}" height="{ch:.1f}" '
                             f'fill="none" stroke="{it.color}" stroke-width=".7" stroke-dasharray="2 1.5"><title>{it.sku}: reserva</title></rect>')
                continue
            parts.append(f'<rect x="{0.5 + (x + ox) * sc:.1f}" y="{oy + .5 + (z + oz) * sc:.1f}" width="{cw:.1f}" height="{ch:.1f}" '
                         f'fill="{it.color}" fill-opacity="{0.35 + 0.5 * n / max(it.ny, 1):.2f}"><title>{it.sku}: camada {n}</title></rect>')
            if size >= 60 and min(cw, ch) >= fs * 1.1:
                parts.append(f'<text x="{0.5 + (x + ox) * sc + cw / 2:.1f}" y="{oy + .5 + (z + oz) * sc + ch / 2:.1f}" font-size="{fs * .8:.1f}" '
                             f'text-anchor="middle" dominant-baseline="central" fill="#fff">{n}</text>')
        for (x, y), (dx, dy, g) in front.items():
            style = (f'fill="none" stroke="{it.color}" stroke-width=".7" stroke-dasharray="2 1.5"' if g
                     else f'fill="{it.color}" fill-opacity=".75"')
            parts.append(f'<rect x="{fx + .5 + (x + ox) * sc:.1f}" y="{oy + .5 + th - (y + dy) * sc:.1f}" width="{max(dx * sc - .6, .6):.1f}" '
                         f'height="{max(dy * sc - .6, .6):.1f}" {style}/>')
        for n, y, dy, cnt, gh in it.layers():
            yy = oy + .5 + th - (y + dy / 2) * sc
            if round(yy, 1) in numbered:
                continue
            numbered.add(round(yy, 1))
            parts.append(f'<text x="{fx - 1.5:.1f}" y="{yy:.1f}" font-size="{min(fs, max(dy * sc, 3.5)):.1f}" text-anchor="end" '
                         f'dominant-baseline="central" fill="#1f2d33"><title>Camada {n}: {cnt} un. de {it.sku}{f" + {gh} de reserva" if gh else ""}</title>{n}</text>')
    parts.append("</svg>")
    return "".join(parts)
