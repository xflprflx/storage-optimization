"""Raio-X da filial: os dados, as estatísticas e os insights da filial (ou de um armazém dela) no estado atual —
endereços, bins, ocupação das bins e dos níveis, SKUs (saldo, medidas, política de estoque, fragmentação), saídas
(curva ABC, caminhada e zona de ouro, quando há histórico de pedidos), unitização e o que vale a pena fazer.

Tudo é calculado na hora, a partir do cadastro e do saldo: nada é gravado.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.i18n import _

LOW_FILL = 30.0  # % de ocupação das bins abaixo da qual o endereço é candidato a consolidar
FULL_LEVEL = 95.0  # % de uso do nível a partir do qual ele está cheio
MAX_ROWS = 12  # linhas das listas da tela


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def _natural(v: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", v or "")]


def _bin_vol(bt) -> float:
    """Volume útil de uma bin (cm³): o volume cadastrado (L) ou as medidas internas. Bin aberta em cima (palete): a
    carga sobe até o máximo acima da borda; sem esse máximo, não dá para medir (0 = fora da conta)."""
    if bt.open_top:
        if not (bt.over_cm and bt.height and bt.width and bt.length):
            return 0.0
        return float(bt.width) * float(bt.length) * (float(bt.height) + float(bt.over_cm))
    if bt.max_volume:
        return float(bt.max_volume) * 1000.0
    if bt.height and bt.width and bt.length:
        return float(bt.height) * float(bt.width) * float(bt.length)
    return 0.0


def _bucket(p: float) -> str:
    return "0-25" if p < 25 else "25-50" if p < 50 else "50-75" if p < 75 else "75-100" if p <= 100 else ">100"


def build(db: Session, branch_id: int, warehouse_id: Optional[int] = None) -> dict:
    """Indicadores e insights da filial (e do armazém, se informado)."""
    from models import (BayLock, BinType, Branch, BranchLayout, LevelCapacity, LocationBin, PickHistory, Product,
                        ProductPackaging, ShelfLevel, StockItem, StockPolicy, StorageLocation, Warehouse)
    from models.entities import packaging_branches

    br = db.get(Branch, branch_id)
    if br is None:
        return {}
    out: dict = {"branch": br, "warehouse": db.get(Warehouse, warehouse_id) if warehouse_id else None}

    # ---- endereços ----
    lq = select(StorageLocation).where(StorageLocation.branch_id == branch_id)
    if warehouse_id:
        lq = lq.where(StorageLocation.warehouse_id == warehouse_id)
    locs = {l.id: l for l in db.scalars(lq)}
    ids = list(locs)
    types = {t.id: t for t in db.scalars(select(BinType))}
    bins_at: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    lbins = []
    for i in range(0, len(ids), 900):
        for lb in db.scalars(select(LocationBin).where(LocationBin.location_id.in_(ids[i:i + 900]))):
            bins_at[lb.location_id][lb.bin_type_id] += lb.quantity
            lbins.append(lb)
    stock: dict[int, dict[int, float]] = defaultdict(dict)
    for i in range(0, len(ids), 900):
        for si in db.scalars(select(StockItem).where(StockItem.location_id.in_(ids[i:i + 900]), StockItem.quantity > 0)):
            stock[si.location_id][si.product_id] = round(stock[si.location_id].get(si.product_id, 0.0)
                                                         + float(si.quantity), 4)
    pids = {p for d in stock.values() for p in d}
    prods = {}
    plist = list(pids)
    for i in range(0, len(plist), 900):
        for p in db.scalars(select(Product).where(Product.id.in_(plist[i:i + 900]))):
            prods[p.id] = p

    n_loc = len(locs)
    with_stock = [l for l in ids if stock.get(l)]
    with_bins = [l for l in ids if sum(bins_at[l].values()) > 0]
    idle = [l for l in with_bins if not stock.get(l)]  # bins sem saldo (ociosas)
    total_bins = sum(sum(v.values()) for v in bins_at.values())
    by_type = Counter()
    for l, d in bins_at.items():
        for t, q in d.items():
            by_type[t] += q
    out["locations"] = {"total": n_loc, "with_stock": len(with_stock), "empty": n_loc - len(with_stock),
                        "with_bins": len(with_bins), "without_bins": n_loc - len(with_bins), "idle": len(idle),
                        "idle_bins": sum(sum(bins_at[l].values()) for l in idle),
                        "pct": round(len(with_stock) * 100.0 / n_loc, 1) if n_loc else 0.0}
    out["bins"] = {"total": total_bins,
                   "types": [{"code": types[t].code, "name": types[t].name, "qty": q,
                              "store_as": types[t].store_as or "units",
                              "pct": round(q * 100.0 / total_bins, 1) if total_bins else 0}
                             for t, q in by_type.most_common() if t in types]}

    # ---- ocupação das bins (volume dos SKUs ÷ volume das bins), por endereço ----
    fills: dict[int, float] = {}
    no_dims_locs = 0
    for l in with_stock:
        vols = [(_bin_vol(types[t]), q) for t, q in bins_at[l].items() if t in types]
        if not vols or any(v == 0 for v, _q in vols):
            continue  # bin sem medidas (ou aberta sem o máximo acima da borda): não dá para medir
        bv = sum(v * q for v, q in vols)
        uv, missing = 0.0, False
        for pid, q in stock[l].items():
            p = prods.get(pid)
            if p is None or not (p.height and p.width and p.length):
                missing = True
                continue
            uv += _f(p.height) * _f(p.width) * _f(p.length) * q
        if missing and not uv:
            no_dims_locs += 1
            continue
        fills[l] = uv * 100.0 / bv
    hist = Counter(_bucket(v) for v in fills.values())
    vals = sorted(fills.values())
    out["fill"] = {
        "avg": round(sum(vals) / len(vals), 1) if vals else None,
        "median": round(vals[len(vals) // 2], 1) if vals else None,
        "buckets": [{"label": k, "n": hist.get(k, 0), "pct": round(hist.get(k, 0) * 100.0 / len(vals), 1) if vals else 0}
                    for k in ("0-25", "25-50", "50-75", "75-100", ">100")],
        "low": sorted(([locs[l].address, round(v, 1), sum(bins_at[l].values())] for l, v in fills.items() if v < LOW_FILL),
                      key=lambda r: r[1])[:MAX_ROWS],
        "low_n": sum(1 for v in fills.values() if v < LOW_FILL),
        "over": sorted(([locs[l].address, round(v, 1), sum(bins_at[l].values())] for l, v in fills.items() if v > 100),
                       key=lambda r: -r[1])[:MAX_ROWS],
        "over_n": sum(1 for v in fills.values() if v > 100),
        "measured": len(vals), "no_dims": no_dims_locs,
    }

    # ---- por corredor ----
    aisles: dict[str, dict] = {}
    for l in ids:
        a = locs[l].aisle or "—"
        row = aisles.setdefault(a, {"aisle": a, "locs": 0, "stock": 0, "bins": 0, "skus": set(), "fills": []})
        row["locs"] += 1
        row["stock"] += 1 if stock.get(l) else 0
        row["bins"] += sum(bins_at[l].values())
        row["skus"] |= set(stock.get(l, {}))
        if l in fills:
            row["fills"].append(fills[l])
    out["aisles"] = [{"aisle": r["aisle"], "locs": r["locs"], "stock": r["stock"],
                      "pct": round(r["stock"] * 100.0 / r["locs"], 1) if r["locs"] else 0, "bins": r["bins"],
                      "skus": len(r["skus"]), "fill": round(sum(r["fills"]) / len(r["fills"]), 1) if r["fills"] else None}
                     for r in sorted(aisles.values(), key=lambda r: _natural(r["aisle"]))]

    # ---- SKUs ----
    where: dict[int, list[int]] = defaultdict(list)
    units: dict[int, float] = defaultdict(float)
    for l, d in stock.items():
        for pid, q in d.items():
            where[pid].append(l)
            units[pid] = round(units[pid] + q, 4)
    no_dims = [p for p in pids if p in prods and not (prods[p].height and prods[p].width and prods[p].length)]
    frag = sorted(((prods[p].sku, len(ls), units[p]) for p, ls in where.items() if len(ls) > 1 and p in prods),
                  key=lambda r: (-r[1], r[0]))
    mixed = sum(1 for l in with_stock if len(stock[l]) > 1)
    pols = {pol.product_id: pol for pol in db.scalars(select(StockPolicy).where(StockPolicy.branch_id == branch_id))}
    above = sorted(((prods[p].sku, units[p], _f(pols[p].max_level)) for p in pids
                    if p in pols and pols[p].max_level and units[p] > _f(pols[p].max_level) and p in prods),
                   key=lambda r: -(r[1] - r[2]))
    below = sorted(((prods[p].sku, units[p], _f(pols[p].min_level)) for p in pids
                    if p in pols and pols[p].min_level and units[p] < _f(pols[p].min_level) and p in prods),
                   key=lambda r: (r[1] - r[2]))
    no_policy = [p for p in pids if p not in pols or (not pols[p].min_level and not pols[p].max_level)]
    no_default = [p for p in pids if p not in pols or not pols[p].default_location_id]
    out["skus"] = {"total": len(pids), "units": round(sum(units.values()), 4), "no_dims": len(no_dims),
                   "no_dims_list": sorted(prods[p].sku for p in no_dims)[:MAX_ROWS],
                   "frag": len(frag), "frag_list": frag[:MAX_ROWS], "mixed_locs": mixed,
                   "avg_per_loc": round(sum(len(d) for d in stock.values()) / len(with_stock), 2) if with_stock else 0,
                   "above": len(above), "above_list": above[:MAX_ROWS], "below": len(below),
                   "below_list": below[:MAX_ROWS], "no_policy": len(no_policy), "no_default": len(no_default)}

    # ---- níveis: uso pela capacidade cadastrada (Σ bins do tipo ÷ capacidade do tipo) ----
    lv_q = select(ShelfLevel).where(ShelfLevel.branch_id == branch_id)
    if warehouse_id:
        lv_q = lv_q.where(ShelfLevel.warehouse_id == warehouse_id)
    levels = {lv.id: lv for lv in db.scalars(lv_q)}
    key_of = {(lv.warehouse_id, lv.aisle, lv.bay, lv.level): lv.id for lv in levels.values()}
    caps: dict[int, dict[int, int]] = defaultdict(dict)
    lids = list(levels)
    for i in range(0, len(lids), 900):
        for c in db.scalars(select(LevelCapacity).where(LevelCapacity.shelf_level_id.in_(lids[i:i + 900]))):
            if c.max_bins:
                caps[c.shelf_level_id][c.bin_type_id] = c.max_bins
    on_level: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for lb in lbins:
        loc = locs[lb.location_id]
        sl = lb.shelf_level_id or key_of.get((loc.warehouse_id, loc.aisle, loc.bay, loc.level))
        if sl in levels:
            on_level[sl][lb.bin_type_id] += lb.quantity
    usage = {}
    for sl in levels:
        if not caps.get(sl):
            continue
        u = sum(q / caps[sl][t] for t, q in on_level[sl].items() if caps[sl].get(t))
        usage[sl] = u * 100.0
    uv_ = list(usage.values())
    out["levels"] = {"total": len(levels), "with_cap": len(usage),
                     "avg": round(sum(uv_) / len(uv_), 1) if uv_ else None,
                     "full": sum(1 for v in uv_ if v >= FULL_LEVEL), "empty": sum(1 for v in uv_ if v <= 0),
                     "over": sorted(([levels[k].code, round(v, 1)] for k, v in usage.items() if v > 100.0 + 1e-6),
                                    key=lambda r: -r[1])[:MAX_ROWS],
                     "over_n": sum(1 for v in uv_ if v > 100.0 + 1e-6)}
    locks = list(db.scalars(select(BayLock).where(BayLock.branch_id == branch_id)))
    out["locks"] = {"bays": len(locks), "fixed": sum(1 for x in locks if x.fixed_position),
                    "no_recompose": sum(1 for x in locks if x.no_recompose)}

    # ---- unitização ----
    enabled = db.scalar(select(func_count()).select_from(packaging_branches)
                        .where(packaging_branches.c.branch_id == branch_id)) or 0
    with_pack = 0
    if pids:
        plist = list(pids)
        for i in range(0, len(plist), 900):
            with_pack += db.scalar(select(func_count(ProductPackaging.product_id.distinct()))
                                   .where(ProductPackaging.product_id.in_(plist[i:i + 900]))) or 0
    out["unit"] = {"mode": br.unitization or "off", "enabled_packs": enabled, "skus_with_pack": with_pack,
                   "store_as": Counter(types[t].store_as or "units" for t in by_type if t in types)}

    # ---- saídas (histórico de pedidos): curva ABC, caminhada e zona de ouro de hoje ----
    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    has_layout = bool(db.scalar(select(BranchLayout.id).where(BranchLayout.branch_id == branch_id)))
    out["layout"] = has_layout
    out["history"] = None
    if h is not None and (h.data or {}).get("skus"):
        out["history"] = _history(db, h, prods, where, stock, locs, ids)

    out["insights"] = _insights(out, br, warehouse_id)
    return out


def func_count(col=None):
    from sqlalchemy import func

    return func.count(col) if col is not None else func.count()


def _history(db: Session, h, prods: dict, where: dict, stock: dict, locs: dict, ids: list) -> dict:
    from services.slotting import address_geo, classify_abc, kpis, popularity

    pop = popularity(h.data, "lines")
    abc = classify_abc(pop)
    by_sku = {p.sku: pid for pid, p in prods.items()}
    total = sum(pop.values()) or 1.0
    res = {"filename": h.filename, "skus": len(pop), "lines": int(sum(pop.values())),
           "abc": Counter(abc.values()), "updated": h.updated_at,
           "not_stocked": sum(1 for s in pop if s not in by_sku),
           "dead": sum(1 for pid in where if prods[pid].sku not in pop)}
    spec = {"metric": "lines", "ref": "", "aisle_m": 3.0, "bay_m": 1.0, "golden_m": 5.0, "golden_levels": set(),
            "affinity": False, "rows": False, "serpentine": False, "walk_golden_m": None}
    geo, ref = address_geo(db, ids, spec) if ids else ({}, "")
    assign = {}
    for pid, ls in where.items():
        sku = prods[pid].sku
        if sku in pop:
            assign[sku] = [(l, stock[l][pid]) for l in ls]
    k = kpis(assign, pop, geo, {}, {}) if geo and assign else {}
    res.update(ref=ref, avg_dist=k.get("avg_dist"), random_dist=k.get("random_dist"),
               golden_picks=k.get("golden_picks"), walk_picks=k.get("walk_picks"))
    # os mais procurados: onde estão e se estão na zona de ouro (pega) / perto da referência
    top = []
    for sku, v in sorted(pop.items(), key=lambda kv: -kv[1])[:MAX_ROWS]:
        pid = by_sku.get(sku)
        ls = where.get(pid, []) if pid else []
        g = [geo[l] for l in ls if l in geo]
        top.append({"sku": sku, "name": prods[pid].name if pid else "", "share": round(v * 100.0 / total, 1),
                    "abc": abc.get(sku, ""), "where": ", ".join(sorted(locs[l].address for l in ls))[:80] or "—",
                    "walk": round(min(x.walk_m for x in g), 1) if g else None,
                    "golden": any(x.golden for x in g) if g and any(x.golden is not None for x in g) else None})
    res["top"] = top
    a_out = [t for t in top if t["abc"] == "A" and t["golden"] is False]
    res["a_out_gold"] = len(a_out)
    # A fora da zona de ouro (todos, não só os do topo)
    n_a, n_out = 0, 0
    for sku, cls in abc.items():
        if cls != "A" or by_sku.get(sku) not in where:
            continue
        g = [geo[l] for l in where[by_sku[sku]] if l in geo]
        if g and any(x.golden is not None for x in g):
            n_a += 1
            n_out += 0 if any(x.golden for x in g) else 1
    res["a_total"], res["a_out"] = n_a, n_out
    return res


def _insights(d: dict, br, warehouse_id) -> list[dict]:
    """O que chama a atenção, do mais importante para o menos, cada um com o caminho para resolver."""
    out = []
    scope = f"branch_id={br.id}" + (f"&warehouse_id={warehouse_id}" if warehouse_id else "")
    L, F, S, H, U, LV = d["locations"], d["fill"], d["skus"], d.get("history"), d["unit"], d["levels"]

    def add(level, icon, text, link=None, label=None):
        out.append({"level": level, "icon": icon, "text": text, "link": link, "label": label})

    if L["total"] == 0:
        add("info", "geo-alt", _("A filial ainda não tem endereços. Gere em lote ou importe para começar."),
            "/locations/batch", _("Gerar endereços"))
        return out
    if S["no_dims"]:
        add("danger", "rulers", _("{n} SKU(s) com saldo sem altura, largura ou comprimento: ficam fora das otimizações "
                                  "e da ocupação das bins.", n=S["no_dims"]), "/products", _("Completar medidas"))
    if F["over_n"]:
        add("danger", "exclamation-octagon",
            _("{n} endereço(s) com o volume dos SKUs maior que o das bins (ocupação acima de 100%): saldo ou medidas "
              "provavelmente errados, ou falta bin.", n=F["over_n"]), f"/optimization?{scope}", _("Por endereço"))
    if LV["over_n"]:
        add("danger", "layers", _("{n} nível(is) com mais bins do que a capacidade cadastrada.", n=LV["over_n"]),
            f"/levels?{scope}", _("Capacidade dos níveis"))
    if L["idle"]:
        add("warning", "inbox", _("{n} endereço(s) sem saldo ainda têm {b} bin(s): espaço parado que o Pool pode "
                                  "reaproveitar.", n=L["idle"], b=L["idle_bins"]), f"/optimization/pool?{scope}",
            _("Pool de bins"))
    if F["low_n"]:
        add("warning", "arrows-collapse",
            _("{n} endereço(s) usam menos de {p}% do volume das bins: candidatos a bins menores ou a juntar com outros.",
              n=F["low_n"], p=int(LOW_FILL)), f"/optimization?{scope}", _("Por endereço"))
    if S["above"]:
        add("warning", "graph-up-arrow", _("{n} SKU(s) com saldo acima do max level da filial.", n=S["above"]))
    if S["below"]:
        add("info", "graph-down-arrow", _("{n} SKU(s) com saldo abaixo do min level da filial.", n=S["below"]))
    if S["frag"]:
        add("info", "diagram-3", _("{n} SKU(s) espalhados em 2 ou mais endereços: juntar reduz caminhada e contagem.",
                                   n=S["frag"]), f"/optimization/pool?{scope}", _("Pool de bins"))
    if H is None:
        add("info", "clock-history", _("Sem histórico de pedidos: importe para ver a curva ABC, a caminhada e a zona de "
                                       "ouro dos mais procurados."), "/imports/history", _("Importar histórico"))
    else:
        if H.get("a_total") and H.get("a_out"):
            add("warning", "star", _("{n} de {t} SKU(s) curva A estão fora da zona de ouro da pega: a posição pela "
                                     "saída sugere trocas.", n=H["a_out"], t=H["a_total"]), f"/optimization?{scope}",
                _("Posição pela saída"))
        if H.get("dead"):
            add("info", "moon", _("{n} SKU(s) com saldo não saíram no período do histórico (sem giro): candidatos a "
                                  "níveis altos ou ao fundo.", n=H["dead"]))
    if not d.get("layout"):
        add("info", "map", _("Sem planta desenhada: a caminhada é estimada pelo endereço. Desenhe a planta para "
                             "distâncias reais, simulador e 3D."), f"/layout/plan?{scope}", _("Planta da filial"))
    if U["mode"] == "off" and U["enabled_packs"]:
        add("info", "box2", _("{n} embalagem(ns) habilitada(s) nesta filial, mas a unitização da filial está "
                              "desligada: elas não entram nas otimizações.", n=U["enabled_packs"]),
            f"/branches/{br.id}/edit", _("Editar filial"))
    if U["mode"] != "off" and not U["enabled_packs"]:
        add("info", "box2", _("Unitização ligada, mas nenhuma embalagem habilitada nesta filial."), "/packagings",
            _("Embalagens dos produtos"))
    if S["no_policy"]:
        add("info", "sliders", _("{n} SKU(s) com saldo sem min/max level na filial: as otimizações por min/max usam o "
                                 "saldo.", n=S["no_policy"]), "/imports/products", _("Importar produtos"))
    if not out:
        add("success", "check2-circle", _("Nada chama a atenção: endereços, bins e SKUs sem pendências."))
    return out


def unit_label(mode: str) -> str:
    return {"off": _("Desligada"), "cases": _("Só caixas"), "full": _("Caixas e paletes")}.get(mode, mode)

