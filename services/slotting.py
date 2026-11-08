"""
Posição dos SKUs pela saída (slotting) para filiais EXISTENTES (modos Por endereço e Pool).

O projeto de filial nova calcula a caminhada pela planta (X/Y). Numa filial existente o sistema não tem a planta, então
a facilidade de cada endereço é ESTIMADA pelo próprio endereço:

    caminhada ≈ |corredor − corredor de referência| × distância entre corredores
                + posição da estante no corredor × frente da estante
    facilidade = caminhada + penalidade (em metros) quando o nível está fora da zona de ouro

- corredor de referência: o mais perto da expedição (padrão: o primeiro, na ordem natural dos códigos);
- posição no corredor: estantes ímpares de um lado e pares do outro, numeradas a partir da ponta da expedição
  (estante 01 e 02 = posição 1, 03 e 04 = posição 2…);
- zona de ouro: pela estrutura cadastrada na estante (Cadastros → Estruturas, início/fim em cm) e, sem estrutura,
  pelos níveis informados (ex.: "2,3").

Popularidade: histórico de pedidos (linhas de pedido ou unidades por SKU, pares que saem juntos) ou uma coluna Saída,
enviados por planilha e guardados por filial (PickHistory, só o resumo).
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

EPS = 1e-9
SLOT_DEFAULTS = {"slot": "1", "slot_metric": "lines", "slot_ref": "", "slot_aisle_m": "3", "slot_bay_m": "1",
                 "slot_golden_m": "5", "slot_golden_levels": "", "slot_aff": "1", "slot_rows": "", "slot_serp": "", "slot_walk_m": ""}
AFFINITY_GROUP = 0.3  # força mínima para tratar dois SKUs como "saem juntos" (fração dos pedidos do menos vendido)


# --------------------------------------------------------------------------- #
# Histórico de saídas
# --------------------------------------------------------------------------- #
def read_history(path: Path, to_abs: bool) -> tuple[Optional[dict], str]:
    """Lê uma planilha (ou CSV) com o histórico de pedidos (pedido, SKU, quantidade[, data]) ou, na falta dela, uma
    lista de SKUs com a coluna Saída. Retorna (resumo, mensagem de erro)."""
    from services.file_reader import normalize_header
    from services.site_import import ORDER_COLS, SHEETS, SKU_COLS, _map, _orders, read_sheets

    sheets = read_sheets(path)
    if not sheets:
        return None, "A planilha não tem abas com dados."
    order_names = {normalize_header(s) for s in SHEETS["orders"]}
    ranked = sorted(sheets, key=lambda n: (normalize_header(n) not in order_names, n))
    for name in ranked:  # 1º: histórico de pedidos
        rows = sheets[name]
        if len(rows) < 2:
            continue
        cm = _map(rows[0], ORDER_COLS)
        if cm["order"] is not None and cm["sku"] is not None and cm["qty"] is not None:
            res = _orders(rows, cm, to_abs)
            if res.get("error"):
                return None, f"Aba {name}: {res['error']}"
            res["sheet"], res["kind"] = name, "orders"
            return res, ""
    for name in ranked:  # 2º: SKU + Saída
        rows = sheets[name]
        if len(rows) < 2:
            continue
        cm = _map(rows[0], {"sku": SKU_COLS["sku"], "out": SKU_COLS["out"]})
        if cm["sku"] is None or cm["out"] is None:
            continue
        skus: dict[str, list] = {}
        for r in rows[1:]:
            sku = (r[cm["sku"]] if cm["sku"] < len(r) else "") or ""
            raw = (r[cm["out"]] if cm["out"] < len(r) else "") or ""
            try:
                v = abs(float(str(raw).replace(".", "").replace(",", "."))) if "," in str(raw) else abs(float(raw or 0))
            except ValueError:
                continue
            if sku.strip() and v > 0:
                e = skus.setdefault(sku.strip()[:60], [0.0, 0.0])
                e[0] += v
                e[1] += v
        if skus:
            return {"kind": "column", "sheet": name, "orders": 0, "lines": 0, "skus": skus, "pairs": [], "neg": 0,
                    "abs": to_abs, "dates": None}, ""
    return None, ("Nenhuma aba reconhecida: use uma aba Pedidos (pedido, SKU, quantidade) ou uma lista de SKUs com a "
                  "coluna Saída.")


def popularity(data: Optional[dict], metric: str = "lines") -> dict[str, float]:
    """SKU -> popularidade (linhas de pedido ou unidades)."""
    if not data or not data.get("skus"):
        return {}
    k = 1 if metric == "units" else 0
    return {s: float(v[k]) for s, v in data["skus"].items() if v[k]}


def affinity(data: Optional[dict]) -> tuple[dict, dict]:
    """Pares que saem juntos com suporte mínimo: ({(a, b): força}, {(a, b): nº de pedidos juntos}).
    Força = em que fração dos pedidos do menos vendido os dois aparecem juntos (>= 0,2)."""
    if not data or not data.get("pairs"):
        return {}, {}
    lines = {s: v[0] for s, v in (data.get("skus") or {}).items()}
    min_count = max(3, round(max(1, int(data.get("orders") or 1)) * 0.002))
    aff, counts = {}, {}
    for a, b, c in data["pairs"]:
        st = c / max(1, min(lines.get(a, 0), lines.get(b, 0)))
        if c >= min_count and st >= 0.2:
            aff[(a, b)] = round(min(1.0, st), 3)
            counts[(a, b)] = c
    return aff, counts


def classify_abc(pop: dict[str, float]) -> dict[str, str]:
    """Curva ABC: A = SKUs que somam até 80% das saídas, B = até 95%, C = o resto."""
    total = sum(pop.values()) or 1.0
    out, acc = {}, 0.0
    for s, v in sorted(pop.items(), key=lambda kv: -kv[1]):
        out[s] = "A" if acc < 0.80 * total else "B" if acc < 0.95 * total else "C"
        acc += v
    return out


# --------------------------------------------------------------------------- #
# Facilidade estimada pelo endereço
# --------------------------------------------------------------------------- #
def _natural(v: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v or "")]


def _num(v, default: float) -> float:
    try:
        return float(str(v).replace(",", ".")) if str(v or "").strip() else default
    except ValueError:
        return default


@dataclass
class Geo:
    aisle_idx: int
    x_m: float  # posição ao longo do corredor (m, a partir da ponta da expedição)
    y_m: float  # deslocamento entre corredores (m)
    golden: Optional[bool]  # zona de ouro da PEGA (altura); None = sem zona de ouro definida para o nível
    walk_m: float  # caminhada estimada desde a referência
    ease: float  # caminhada + penalidade fora da zona de ouro da pega (menor = melhor)
    walk_golden: bool = False  # zona de ouro da CAMINHADA: entre as posições mais perto da referência
    node: Optional[object] = None  # ponto na planta desenhada da filial (route_sim.Node), quando houver


def slot_spec(raw: dict) -> Optional[dict]:
    """Parâmetros do formulário (slot_*); None quando desligado."""
    if str(raw.get("slot") or "").lower() not in {"1", "on", "true"}:
        return None
    levels = {int(x) for x in re.findall(r"\d+", str(raw.get("slot_golden_levels") or ""))}
    return {"metric": "units" if raw.get("slot_metric") == "units" else "lines",
            "ref": str(raw.get("slot_ref") or "").strip().upper(),
            "aisle_m": max(0.1, _num(raw.get("slot_aisle_m"), 3.0)), "bay_m": max(0.1, _num(raw.get("slot_bay_m"), 1.0)),
            "golden_m": max(0.0, _num(raw.get("slot_golden_m"), 5.0)), "golden_levels": levels,
            "affinity": str(raw.get("slot_aff") or "").lower() in {"1", "on", "true"},
            # letra por rua: duas letras por corredor (A|B, C|D… — a 1ª rua encostada na parede) e estantes 01, 02…
            "rows": str(raw.get("slot_rows") or "").lower() in {"1", "on", "true"},
            # serpentina: a 2ª rua de cada corredor (B, D…) é numerada a partir da ponta oposta
            "serpentine": str(raw.get("slot_serp") or "").lower() in {"1", "on", "true"},
            "walk_golden_m": _num(raw.get("slot_walk_m"), 0.0) or None}


def address_geo(db, location_ids: list[int], spec: dict) -> tuple[dict[int, Geo], str]:
    """Facilidade estimada de cada endereço (location_id -> Geo) e o corredor de referência usado."""
    from sqlalchemy import select

    from models import BayStructure, StorageLocation, StructureType

    locs = []
    for i in range(0, len(location_ids), 900):
        locs += list(db.execute(select(StorageLocation.id, StorageLocation.branch_id, StorageLocation.warehouse_id,
                                       StorageLocation.aisle, StorageLocation.bay, StorageLocation.level)
                                .where(StorageLocation.id.in_(location_ids[i:i + 900]))))
    if not locs:
        return {}, ""
    aisles = sorted({(a or "").upper() for _i, _b, _w, a, _bay, _l in locs}, key=_natural)
    ref = spec.get("ref") if spec.get("ref") in aisles else aisles[0]
    ridx = aisles.index(ref)
    branches = {b for _i, b, *_ in locs}
    st_of = {}
    for bs, st in db.execute(select(BayStructure, StructureType).join(StructureType,
                                                                       StructureType.id == BayStructure.structure_type_id)
                             .where(BayStructure.branch_id.in_(branches))):
        st_of[(bs.branch_id, bs.warehouse_id, bs.aisle.upper(), bs.bay.upper())] = st
    bays_by_aisle: dict[str, list] = {}
    for _i, _b, _w, a, bay, _l in locs:
        bays_by_aisle.setdefault((a or "").upper(), set()).add((bay or "").upper())
    rank = {a: {b: k for k, b in enumerate(sorted(bs, key=_natural))} for a, bs in bays_by_aisle.items()}
    max_pos = {a: max([int(re.sub(r"\D", "", b) or 0) for b in bs] + [len(bs)]) for a, bs in bays_by_aisle.items()}
    from services.branch_layout import geometry
    from services.route_sim import dist

    layouts = {sc: geometry(db, sc[0], sc[1]) for sc in {(b, w) for _i, b, w, *_ in locs}}
    used_layout = None
    out = {}
    for lid, br, wh, a, bay, lvl in locs:
        a, bay = (a or "").upper(), (bay or "").upper()
        digits = re.sub(r"\D", "", bay)
        if spec.get("rows"):  # letra por rua: estantes numeradas em sequência ao longo da rua
            pos = int(digits) if digits else rank[a].get(bay, 0) + 1
            if spec.get("serpentine") and aisles.index(a) % 2 == 1:  # começa na ponta oposta do corredor
                pos = max(1, max_pos.get(a, pos) - pos + 1)
        else:  # letra por corredor: ímpares de um lado, pares do outro
            pos = (int(digits) + 1) // 2 if digits else rank[a].get(bay, 0) // 2 + 1
        n = int(re.sub(r"\D", "", lvl or "") or 0)
        st = st_of.get((br, wh, a, bay)) or st_of.get((br, None, a, bay))
        golden = None
        if st is not None and st.golden_from is not None and st.golden_to is not None and n:
            pitch = float(st.height) / max(1, st.levels)
            mid = (n - 1) * pitch + float(st.level_height) / 2
            golden = float(st.golden_from) - EPS <= mid <= float(st.golden_to) + EPS
        elif spec.get("golden_levels") and n:
            golden = n in spec["golden_levels"]
        geom = layouts.get((br, wh))
        node = geom.node(a, bay) if geom is not None else None
        if node is not None:  # planta desenhada: caminhada pelos corredores desde a referência da planta
            walk = dist(geom.ref_node(), node) / 100.0
            out[lid] = Geo(node.zone, node.along / 100.0, node.cross / 100.0, golden, walk,
                           walk + (spec["golden_m"] if golden is False else 0.0), node=node)
            used_layout = geom
            continue
        ai = aisles.index(a)
        if spec.get("rows"):  # duas ruas por corredor
            ai, r0 = ai // 2, ridx // 2
        else:
            r0 = ridx
        y = abs(ai - r0) * spec["aisle_m"]
        x = max(0, pos - 1) * spec["bay_m"] + spec["bay_m"] / 2
        walk = x + y
        out[lid] = Geo(ai, x, y, golden, walk, walk + (spec["golden_m"] if golden is False else 0.0))
    # zona de ouro da caminhada: até X m (informado) ou os 20% de endereços mais perto da referência
    lim = spec.get("walk_golden_m")
    if not lim and out:
        ws = sorted(g.walk_m for g in out.values())
        lim = ws[max(0, int(math.ceil(len(ws) * 0.2)) - 1)]
    for g in out.values():
        g.walk_golden = g.walk_m <= (lim or 0) + EPS
    spec["walk_limit"] = lim
    if used_layout is not None:
        ref = f"referência da planta ({used_layout.ref['label']})"
        spec["layout"] = True
    return out, ref


def geo_dist(a: Geo, b: Geo) -> float:
    """Caminhada estimada entre dois endereços: no mesmo corredor, ao longo dele; em corredores diferentes, sai pela
    ponta da expedição e entra no outro."""
    if a.node is not None and b.node is not None:  # planta desenhada
        from services.route_sim import dist

        return dist(a.node, b.node) / 100.0
    if a.aisle_idx == b.aisle_idx:
        return abs(a.x_m - b.x_m)
    return a.x_m + b.x_m + abs(a.y_m - b.y_m)


def slot_order(skus: list[str], pop: dict[str, float], need: dict[str, float], aff: dict) -> list[str]:
    """Ordem de escolha dos endereços: popularidade por bin (muito procurado e pequeno primeiro), com os grupos de
    itens que saem juntos (força >= AFFINITY_GROUP, até 6 SKUs) em sequência."""
    dens = {s: pop.get(s, 0.0) / max(need.get(s, 1.0), 1e-6) for s in skus}
    ranked = sorted(skus, key=lambda s: (-dens[s], s))
    if not aff:
        return ranked
    parent = {s: s for s in skus}
    size = {s: 1 for s in skus}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), st in sorted(aff.items(), key=lambda kv: -kv[1]):
        if st >= AFFINITY_GROUP and a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb and size[ra] + size[rb] <= 6:
                parent[rb] = ra
                size[ra] += size[rb]
    groups: dict[str, list] = {}
    for s in ranked:
        groups.setdefault(find(s), []).append(s)
    gd = {r: sum(pop.get(s, 0.0) for s in g) / max(1e-6, sum(need.get(s, 1.0) for s in g)) for r, g in groups.items()}
    return [s for r in sorted(groups, key=lambda r: (-gd[r], groups[r][0])) for s in groups[r]]


def kpis(assign: dict[str, list[tuple[int, float]]], pop: dict[str, float], geo: dict[int, Geo],
         counts: dict, aff: dict) -> dict:
    """Indicadores de uma distribuição (SKU -> [(endereço, peso)]): caminhada média por saída, a de uma posição ao
    acaso, parcela das saídas na zona de ouro e distância entre os pares que mais saem juntos."""
    out: dict = {}
    used = [(l, w) for lst in assign.values() for l, w in lst if l in geo and w > 0]
    tw = sum(w for _l, w in used)
    out["random_dist"] = sum(geo[l].walk_m * w for l, w in used) / tw if tw else None
    has_gold = any(geo[l].golden is not None for l, _w in used)
    out["golden_positions"] = (sum(w for l, w in used if geo[l].golden) / tw * 100) if tw and has_gold else None
    per = {}
    for s, lst in assign.items():
        lst = [(l, w) for l, w in lst if l in geo and w > 0]
        sw = sum(w for _l, w in lst)
        if sw:
            per[s] = (sum(geo[l].walk_m * w for l, w in lst) / sw, sum(w for l, w in lst if geo[l].golden) / sw)
    pp = {s: pop[s] for s in per if pop.get(s)}
    tot = sum(pp.values())
    walk = {}
    for s, lst in assign.items():
        lst = [(l, w) for l, w in lst if l in geo and w > 0]
        sw = sum(w for _l, w in lst)
        if sw:
            walk[s] = (sum(w for l, w in lst if geo[l].walk_golden) / sw,
                       sum(w for l, w in lst if geo[l].walk_golden and geo[l].golden) / sw)
    out["walk_positions"] = sum(w for l, w in used if geo[l].walk_golden) / tw * 100 if tw else None
    if tot:
        out["avg_dist"] = sum(pp[s] * per[s][0] for s in pp) / tot
        out["golden_picks"] = sum(pp[s] * per[s][1] for s in pp) / tot * 100 if has_gold else None
        out["walk_picks"] = sum(pp[s] * walk.get(s, (0, 0))[0] for s in pp) / tot * 100
        out["both_picks"] = sum(pp[s] * walk.get(s, (0, 0))[1] for s in pp) / tot * 100 if has_gold else None
    pairs = []
    for (a, b), c in sorted(counts.items(), key=lambda kv: -kv[1])[:30]:
        la = [l for l, w in assign.get(a, []) if l in geo and w > 0]
        lb = [l for l, w in assign.get(b, []) if l in geo and w > 0]
        if la and lb:
            pairs.append({"a": a, "b": b, "count": c, "strength": aff.get((a, b), 0.0),
                          "dist": min(geo_dist(geo[x], geo[y]) for x in la for y in lb)})
    out["pairs"] = pairs[:15]
    if pairs:
        out["pairs_avg"] = sum(p["dist"] for p in pairs) / len(pairs)
        pts = list({l for l, _w in used})
        rg = random.Random(7)
        smp = [(geo[rg.choice(pts)], geo[rg.choice(pts)]) for _ in range(2000)] if len(pts) > 1 else []
        out["pairs_random"] = sum(geo_dist(p, q) for p, q in smp) / len(smp) if smp else None
    return out


def summary_text(data: Optional[dict]) -> str:
    if not data:
        return ""
    if data.get("kind") == "column":
        return f"coluna Saída ({len(data.get('skus') or {})} SKUs)"
    txt = f"{data.get('orders', 0)} pedidos, {data.get('lines', 0)} linhas, {len(data.get('skus') or {})} SKUs"
    if data.get("dates"):
        txt += f", {data['dates'][0]} a {data['dates'][1]}"
    return txt



# --------------------------------------------------------------------------- #
# Relatório de SKUs: popularidade e afinidade
# --------------------------------------------------------------------------- #
def sku_report(data: Optional[dict], names: Optional[dict] = None, where: Optional[dict] = None,
               metric: str = "lines", top_partners: int = 5) -> dict:
    """Popularidade (linhas de pedido, unidades, % e % acumulado das saídas, curva ABC) e afinidade (com quem cada
    SKU sai junto) a partir do resumo do histórico. `where` = SKU -> [{"address", "dist", "golden", "walk"}].
    Afinidade de um par: pedidos juntos, força (fração dos pedidos do menos vendido em que saem juntos) e lift
    (quantas vezes saem juntos mais do que sairiam por acaso: juntos × pedidos ÷ (pedidos de A × pedidos de B))."""
    names, where = names or {}, where or {}
    skus = (data or {}).get("skus") or {}
    n_orders = int((data or {}).get("orders") or 0)
    pop = popularity(data, metric)
    abc = classify_abc(pop)
    total = sum(pop.values()) or 1.0
    partners: dict[str, list] = {}
    pairs = []
    for a, b, c in (data or {}).get("pairs") or []:
        la, lb = skus.get(a, [0])[0], skus.get(b, [0])[0]
        st = c / max(1, min(la, lb))
        lift = c * n_orders / (la * lb) if n_orders and la and lb else None
        pr = {"a": a, "b": b, "count": c, "strength": min(1.0, st), "lift": lift,
              "name_a": names.get(a, ""), "name_b": names.get(b, "")}
        pairs.append(pr)
        partners.setdefault(a, []).append({"sku": b, "count": c, "strength": min(1.0, st), "lift": lift})
        partners.setdefault(b, []).append({"sku": a, "count": c, "strength": min(1.0, st), "lift": lift})
    rows, acc = [], 0.0
    for s, v in sorted(pop.items(), key=lambda kv: (-kv[1], kv[0])):
        acc += v
        lines, units = skus.get(s, [0, 0])[:2]
        ps = sorted(partners.get(s, []), key=lambda x: (-x["count"], x["sku"]))
        rows.append({"sku": s, "name": names.get(s, ""), "lines": lines, "units": units, "share": v / total * 100,
                     "cum": acc / total * 100, "abc": abc.get(s, ""), "partners": ps[:top_partners],
                     "n_partners": len(ps), "per_order": (lines / n_orders * 100) if n_orders else None,
                     "where": where.get(s, [])})
    pairs.sort(key=lambda x: (-x["count"], x["a"], x["b"]))
    lines_total = sum(v[0] for v in skus.values())
    return {"rows": rows, "pairs": pairs, "orders": n_orders, "lines": lines_total, "skus": len(skus),
            "abc": {c: sum(1 for r in rows if r["abc"] == c) for c in "ABC"},
            "lines_per_order": (lines_total / n_orders) if n_orders else None, "metric": metric,
            "kind": (data or {}).get("kind") or "orders", "dates": (data or {}).get("dates")}


def sku_report_xlsx(rep: dict, title: str) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F2A2A"))

    def sheet(ws, cols, rows, widths=None):
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head
        for r in rows:
            ws.append(list(r))
        for i, w in (widths or {}).items():
            ws.column_dimensions[chr(64 + i)].width = w
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "SKUs"
    rnd = lambda v, n=1: round(v, n) if v is not None else None  # noqa: E731
    sheet(ws, ["SKU", "Produto", "Linhas de pedido", "Unidades", "% das saídas", "% acumulado", "Curva ABC",
               "% dos pedidos", "Endereço(s)", "Caminhada (m)", "Zona de ouro da pega", "Zona de ouro da caminhada",
               "Sai junto com (pedidos juntos · força · lift)"],
          [(r["sku"], r["name"], r["lines"], r["units"], rnd(r["share"], 2), rnd(r["cum"], 2), r["abc"],
            rnd(r["per_order"], 2), ", ".join(w["address"] for w in r["where"][:5]) or None,
            rnd(min((w["dist"] for w in r["where"] if w.get("dist") is not None), default=None)),
            "sim" if any(w.get("golden") for w in r["where"]) else "",
            "sim" if any(w.get("walk") for w in r["where"]) else "",
            "; ".join(f"{p['sku']} ({p['count']} · {p['strength'] * 100:.0f}%"
                      + (f" · {p['lift']:.1f}x" if p["lift"] else "") + ")" for p in r["partners"]) or None)
           for r in rep["rows"]], {1: 16, 2: 36, 9: 30, 13: 60})
    sheet(wb.create_sheet("Pares"), ["SKU", "Produto", "SKU", "Produto", "Pedidos juntos", "Força (%)", "Lift"],
          [(p["a"], p["name_a"], p["b"], p["name_b"], p["count"], rnd(p["strength"] * 100), rnd(p["lift"], 2))
           for p in rep["pairs"]], {1: 16, 2: 32, 3: 16, 4: 32})
    info = wb.create_sheet("Resumo")
    for k, v in (("Relatório", title), ("Pedidos", rep["orders"]), ("Linhas de pedido", rep["lines"]),
                 ("SKUs com saída", rep["skus"]), ("Linhas por pedido", rnd(rep["lines_per_order"], 2)),
                 ("Curva ABC (A/B/C)", f"{rep['abc']['A']} / {rep['abc']['B']} / {rep['abc']['C']}"),
                 ("Período", " a ".join(rep["dates"]) if rep.get("dates") else None),
                 ("Força", "em que fração dos pedidos do menos vendido do par os dois saem juntos"),
                 ("Lift", "quantas vezes saem juntos mais do que sairiam por acaso (1 = acaso)")):
        info.append([k, v])
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 22, 80
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
