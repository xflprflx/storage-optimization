"""
Arranjo 3D do projeto de filial nova: a geometria do plano (imóvel, zonas, obstáculos, estruturas com os níveis e
cada bin no lugar) num formato simples para o visualizador (`templates/site/view3d.html`, Three.js).

Coordenadas em cm, como na planta: X ao longo do comprimento do imóvel, Y ao longo da largura (a partir da parede de
cima), Z para cima a partir do piso.

Posição das bins num nível: as colunas correm ao longo da frente do módulo (largura da bin + folga lateral) e, se a
bin couber mais de uma vez na profundidade, as linhas vão da frente (lado do corredor) para o fundo. Cada endereço
ocupa as vagas seguintes do nível, na ordem das posições. Compartimentos são desenhados como fatias da bin pai.
"""
from __future__ import annotations

import math
from typing import Optional

from services.site_planner import SitePlan, aisle_labels, struct_colors

EPS = 1e-6
TYPE_PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#edc948", "#76b7b2", "#ff9da7", "#9c755f", "#e15759",
                "#bab0ac", "#86bcb6", "#d37295"]
OBSTACLE_H = 300.0  # altura de desenho dos obstáculos (cm), limitada ao pé-direito


def _r(v: float) -> float:
    return round(v, 1)


def plan_3d(plan: SitePlan) -> dict:
    b, fl = plan.building, plan.floor
    specs, parents, prm = plan.specs or {}, plan.parents or {}, plan.params
    side_gap = prm.side_gap_cm if prm else 0.0
    clear_h = b.clear_height_m * 100.0
    out: dict = {
        "building": {"L": fl.length_cm, "W": fl.width_cm, "H": clear_h, "name": ""},
        "areas": [], "obstacles": [], "sectors": [], "zones": [], "structs": [], "types": [], "modules": [], "bins": [],
        "aisles": [],
    }
    for kind, X, Y, w, d, label in fl.shapes():
        if kind == "obstacle":
            out["obstacles"].append({"x": _r(X), "y": _r(Y), "w": _r(w), "d": _r(d), "h": min(OBSTACLE_H, clear_h),
                                     "name": label})
        elif kind == "sector":
            out["sectors"].append({"x": _r(X), "y": _r(Y), "w": _r(w), "d": _r(d), "name": label})
        elif kind == "zone":
            out["zones"].append({"x": _r(X), "y": _r(Y), "w": _r(w), "d": _r(d), "name": label})
        elif kind in ("reserved", "avenue", "aisle", "cross"):
            out["areas"].append({"kind": kind, "x": _r(X), "y": _r(Y), "w": _r(w), "d": _r(d)})

    out["aisles"] = [{"label": t, "x": _r(X), "y": _r(Y)} for t, X, Y, _v in aisle_labels(plan)]
    scol = struct_colors(plan)
    for sl in plan.structs:
        if sl.qty:
            s = sl.struct
            out["structs"].append({"id": s.id, "code": s.code, "name": s.name, "qty": sl.qty, "color": scol[s.id],
                                   "w": s.width, "d": s.depth, "h": s.height, "levels": s.levels,
                                   "golden": [s.golden_from, s.golden_to] if s.golden_from is not None else None,
                                   "golden_levels": s.golden_levels})
    tidx: dict[int, int] = {}
    for i, tl in enumerate(t for t in plan.types if t.placed or t.needed):
        tidx[tl.spec.id] = i
        out["types"].append({"id": tl.spec.id, "code": tl.spec.code, "color": TYPE_PALETTE[i % len(TYPE_PALETTE)],
                             "placed": tl.placed, "fill": tl.bin_fill})
    skus = {}
    abc = {d.sku: d.abc for d in plan.demands}
    out["ref"] = (plan.slotting or {}).get("ref")

    for mi, m in enumerate(plan.modules):
        s = m.struct
        X, Y, gw, gd = m.grect
        zrot = bool(m.zone.rot) if m.zone is not None else False
        front_x = (not m.rotated) != zrot  # a frente do módulo corre ao longo de X na planta?
        # lado do corredor (frente): fileira "L" fica antes do corredor (corredor em y local maior)
        front_far = (m.row.side == "L") if not m.rotated else False
        pitch = s.height / max(1, s.levels)
        mod = {"x": _r(X), "y": _r(Y), "w": _r(gw), "d": _r(gd), "h": s.height, "s": s.id, "code": f"{m.aisle}-{m.bay}",
               "fx": front_x, "levels": [], "cap": bool(m.rotated)}
        for lv in sorted(m.levels, key=lambda v: v.n):
            z0 = (lv.n - 1) * pitch
            mod["levels"].append({"n": lv.n, "z": _r(z0), "t": tidx.get(lv.type_id) if lv.type_id is not None else None,
                                  "used": lv.used, "cap": lv.cap, "g": s.is_golden(lv.n)})
            if lv.type_id is None or not lv.positions:
                continue
            spec = specs.get(lv.type_id)
            par = parents.get(lv.type_id)
            body = par[0] if par else spec
            per = par[1] if par else 1
            if body is None or not body.has_dims:
                continue
            bw, bl, bh = body.w, body.l, body.h
            deep = max(1, int(math.floor(s.depth / bl + EPS)))
            k = 0
            golden = s.is_golden(lv.n)
            for pos in lv.positions:
                skus.setdefault(pos.sku, pos.name)
                for _ in range(pos.bins):
                    slot, part = divmod(k, per)
                    col, row = divmod(slot, deep)
                    along = col * (bw + side_gap) + part * bw / per  # ao longo da frente
                    across = row * bl  # da frente para o fundo
                    across = (s.depth - across - bl) if front_far else across
                    cw = bw / per
                    if front_x:
                        bx, by, bwx, bdy = X + along, Y + across, cw, bl
                    else:
                        bx, by, bwx, bdy = X + across, Y + along, bl, cw
                    out["bins"].append([_r(bx), _r(by), _r(z0 + 2.0), _r(bwx), _r(bdy), _r(bh), tidx.get(lv.type_id, 0),
                                        None if pos.fill is None else round(pos.fill, 1), mi, pos.address, pos.sku,
                                        golden, abc.get(pos.sku, ""), bool(pos.walk_golden)])
                    k += 1
        out["modules"].append(mod)
    out["skus"] = skus
    out["kpis"] = {"modules": len(plan.modules), "bins": plan.bins_placed, "bin_fill": plan.bin_fill,
                   "level_fill": plan.fill}
    return out
