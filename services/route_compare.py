"""Simulação de separação no resultado da otimização (modos Por endereço e Pool): os pedidos da amostra do
histórico percorridos com os SKUs onde estão hoje e onde ficariam com a proposta do solver — por dia (pedidos com
data) e por pedido, nas três rotas (lista, serpentina, otimizada)."""
from __future__ import annotations

from typing import Optional

from services.route_sim import Node, add_times, simulate, time_params

KINDS = ("list", "s", "opt")


def _nodes(assign: dict[str, list[tuple[int, float]]], geo: dict, addr: dict[int, str]) -> dict[str, list[Node]]:
    """SKU -> pontos de parada (um por endereço com unidades)."""
    out: dict[str, list[Node]] = {}
    for sku, lst in assign.items():
        seen = set()
        for lid, q in lst:
            g = geo.get(lid)
            if g is None or lid in seen or not q or q <= 0:
                continue
            seen.add(lid)
            if g.node is not None:  # planta desenhada: o ponto da planta (zona, corredor, posição)
                n = g.node
                out.setdefault(sku, []).append(Node(key=addr.get(lid, str(lid)), gx=n.gx, gy=n.gy, zone=n.zone,
                                                    aisle=n.aisle, along=n.along, cross=n.cross, gold=g.golden))
            else:
                out.setdefault(sku, []).append(Node(key=addr.get(lid, str(lid)), gx=0, gy=0, zone=0, aisle=g.aisle_idx,
                                                    along=g.x_m * 100, cross=g.y_m * 100, gold=g.golden))
    return out


def compare(sample: list, before: dict, after: dict, geo: dict, addr: dict[int, str], tp: Optional[dict] = None,
            max_orders: int = 400) -> Optional[dict]:
    """Simula a amostra nas duas situações. `before`/`after` = SKU -> [(endereço, unidades)]; `geo` = facilidade de
    cada endereço (slotting.address_geo). Retorna totais, dias e pedidos (os com maior diferença primeiro)."""
    from services.slotting import geo_dist

    if not sample or not geo:
        return None
    geo_by_key = {addr.get(lid, str(lid)): g for lid, g in geo.items()}

    memo: dict[tuple, float] = {}  # os mesmos pares de endereços se repetem muito entre pedidos e situações

    def d(a: Node, b: Node) -> float:  # cm
        k = (a.key, b.key)
        v = memo.get(k)
        if v is not None:
            return v
        if a.key == "REF" and b.key == "REF":
            v = 0.0
        elif a.key == "REF" or b.key == "REF":
            v = geo_by_key[(b if a.key == "REF" else a).key].walk_m * 100
        else:
            v = geo_dist(geo_by_key[a.key], geo_by_key[b.key]) * 100
        memo[k] = memo[(b.key, a.key)] = v
        return v

    start = Node(key="REF", gx=0, gy=0)
    tp = tp or time_params({})
    sims = {}
    for side, assign in (("cur", before), ("new", after)):
        sims[side] = add_times(simulate(sample, _nodes(assign, geo, addr), start, d), tp)
    cur, new = sims["cur"], sims["new"]
    if not cur.get("orders") or not new.get("orders"):
        return None

    def totals(sim: dict) -> dict:
        t = sim.get("times") or {}
        return {"orders": sim["orders"], "lines": sim.get("lines", 0),
                **{f"{k}_km": sim[f"{k}_total"] / 1000 for k in KINDS if f"{k}_total" in sim},
                **{f"{k}_h": t[f"{k}_h"] for k in KINDS if f"{k}_h" in t},
                **{f"{k}_avg": sim[f"{k}_avg"] for k in KINDS if f"{k}_avg" in sim}}

    days = []
    nd = {e["date"]: e for e in new.get("days") or []}
    for e in cur.get("days") or []:
        f = nd.get(e["date"])
        if f is None:
            continue
        row = {"date": e["date"], "orders": e["orders"], "lines": e["lines"], "units": e["units"]}
        for k in KINDS:
            for u in ("m", "h"):
                if f"{k}_{u}" in e and f"{k}_{u}" in f:
                    row[f"{k}_{u}_cur"], row[f"{k}_{u}_new"] = e[f"{k}_{u}"], f[f"{k}_{u}"]
        days.append(row)
    new_rows = {r["order"]: r for r in new["rows"]}
    orders = []
    for r in cur["rows"]:
        q = new_rows.get(r["order"])
        if q is None:
            continue
        row = {"order": r["order"], "date": r.get("date", ""), "lines": r["lines"], "stops_cur": r["stops"],
               "stops_new": q["stops"]}
        for k in KINDS:
            for u in ("m", "t"):
                if f"{k}_{u}" in r and f"{k}_{u}" in q:
                    row[f"{k}_{u}_cur"], row[f"{k}_{u}_new"] = r[f"{k}_{u}"], q[f"{k}_{u}"]
        orders.append(row)
    orders.sort(key=lambda x: -abs(x.get("opt_m_cur", 0) - x.get("opt_m_new", 0)))
    return {"cur": totals(cur), "new": totals(new), "days": days, "orders": orders[:max_orders],
            "n_orders": len(orders), "skipped_cur": cur.get("skipped_lines", 0), "skipped_new": new.get("skipped_lines", 0),
            "params": tp, "has_qty": cur.get("has_qty", False)}


# --------------------------------------------------------------------------- #
# Hoje × proposta em 3D (planta desenhada da filial)
# --------------------------------------------------------------------------- #
_STORE: dict[str, dict] = {}  # token -> distribuição hoje/proposta de uma otimização (as últimas, em memória)
_STORE_MAX = 30


def save(data: dict) -> str:
    """Guarda a distribuição hoje/proposta para a vista 3D e devolve o token do link."""
    import hashlib
    import json

    token = hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]
    _STORE.pop(token, None)
    _STORE[token] = data
    while len(_STORE) > _STORE_MAX:
        _STORE.pop(next(iter(_STORE)))
    return token


def load(token: str) -> Optional[dict]:
    return _STORE.get(token or "")


def payload_3d(sample: list, nodes_cur: dict, nodes_new: dict, start, ref: dict, order: str = "", day: str = "",
               kind: str = "opt") -> dict:
    """Dados da caminhada 3D comparando hoje × proposta: sem pedido/dia, a lista (com as distâncias das duas
    situações em cada rota); com pedido ou dia, dois separadores na rota `kind` ("cur" = hoje, "new" = proposta)."""
    from services.i18n import _
    from services.route_sim import ROUTE_KINDS, day_payload, order_payload, orders_payload

    kind = kind if kind in KINDS else "opt"
    if not order and not day:
        a = orders_payload(sample, nodes_cur, start, ref)
        b = orders_payload(sample, nodes_new, start, ref)
        bo = {o["order"]: o for o in b["orders"]}
        orders = []
        for o in a["orders"]:
            q = bo.get(o["order"])
            if q is None:
                continue
            orders.append({"order": o["order"], "lines": o["lines"], "stops": o["stops"], "date": o.get("date", ""),
                           **{f"{k}_cur": o.get(f"{k}_m") for k in KINDS}, **{f"{k}_new": q.get(f"{k}_m") for k in KINDS}})
        orders.sort(key=lambda x: -abs((x.get("opt_cur") or 0) - (x.get("opt_new") or 0)))
        bd = {e["date"]: e for e in b.get("days") or []}
        days = [{"date": e["date"], "orders": e["orders"], **{f"{k}_cur": e.get(f"{k}_m") for k in KINDS},
                 **{f"{k}_new": bd[e["date"]].get(f"{k}_m") for k in KINDS}} for e in a.get("days") or [] if e["date"] in bd]
        return {"compare": True, "ref": ref, "orders": orders, "days": days,
                "summary": {"orders": len(orders), "has_qty": a["summary"].get("has_qty", False),
                            **{f"{k}_cur": a["summary"].get(f"{k}_avg") for k in KINDS},
                            **{f"{k}_new": b["summary"].get(f"{k}_avg") for k in KINDS}}}
    one = (lambda n: day_payload(sample, n, start, ref, day)) if day else \
        (lambda n: order_payload(sample, n, start, ref, order))
    pc, pn = one(nodes_cur), one(nodes_new)
    if pc.get("error") or pn.get("error"):
        return {"error": pc.get("error") or pn.get("error")}
    if kind not in pc["routes"] or kind not in pn["routes"]:
        return {"error": _("Rota indisponível para este pedido.")}
    rname = next(str(_(n)) for k, n, _c in ROUTE_KINDS if k == kind)
    routes = {"cur": {**pc["routes"][kind], "name": _("Hoje — {route}", route=rname), "color": "#d6604d"},
              "new": {**pn["routes"][kind], "name": _("Proposta — {route}", route=rname), "color": "#1a9850"}}
    return {**pc, "compare": True, "kind": kind, "routes": routes}
