"""Simulação de um dia inteiro: a data de cada pedido no histórico e o dia completo no simulador de trajeto."""
from services.route_sim import Node, add_times, by_day, day_payload, simulate, time_params
from services.site_import import _iso_date, _orders, _map, ORDER_COLS


def _rows():
    rows = [["Pedido", "Data", "SKU", "Quantidade"]]
    for day in range(1, 6):
        for o in range(day * 2):  # dia d tem 2d pedidos
            oid = f"P{day}-{o}"
            rows.append([oid, f"0{day}/10/2026", "A", "2"])
            rows.append([oid, f"0{day}/10/2026", "B", "1"])
    return rows


def test_history_keeps_order_date_and_whole_days(monkeypatch):
    import services.site_import as si

    monkeypatch.setattr(si, "MAX_SAMPLE", 12)  # dia 5 (10 pedidos) cabe; 4, 3 e 2 passariam de 12; o dia 1 (2) cabe
    rows = _rows()
    res = _orders(rows, _map(rows[0], ORDER_COLS), False)
    assert res["days"] == {"2026-10-01": 2, "2026-10-02": 4, "2026-10-03": 6, "2026-10-04": 8, "2026-10-05": 10}
    kept = {}
    for o, skus, qty, day in res["sample"]:
        kept[day] = kept.get(day, 0) + 1
    # dias completos, do mais recente para trás, sem passar do limite
    assert kept == {"2026-10-05": 10, "2026-10-01": 2}
    assert all(day for *_x, day in res["sample"])


def test_iso_date_formats():
    assert _iso_date("2026-10-05 00:00:00") == "2026-10-05"
    assert _iso_date("05/10/2026") == "2026-10-05"
    assert _iso_date("10/25/2026") == "2026-10-25"  # 2º número > 12: mês/dia
    assert _iso_date("05.10.26") == "2026-10-05"
    assert _iso_date("sem data") == ""


def test_day_totals_and_day_payload_chain_orders():
    a = Node(key="A-01", gx=1000, gy=0)
    b = Node(key="B-01", gx=0, gy=2000)
    start = Node(key="REF", gx=0, gy=0)
    nodes = {"A": [a], "B": [b]}
    sample = [["O1", ["A"], [1], "2026-10-01"], ["O2", ["A", "B"], [1, 2], "2026-10-01"],
              ["O3", ["B"], [1], "2026-10-02"]]
    sim = add_times(simulate(sample, nodes, start), time_params({}))
    days = by_day(sim)
    assert [d["date"] for d in days] == ["2026-10-02", "2026-10-01"]
    d1 = days[1]
    assert d1["orders"] == 2 and d1["lines"] == 3
    assert abs(d1["list_m"] - sum(r["list_m"] for r in sim["rows"] if r["date"] == "2026-10-01")) < 1e-9
    assert d1["list_h"] > 0

    p = day_payload(sample, nodes, start, {"x": 0, "y": 0, "label": "ref"}, "2026-10-01")
    assert [o["order"] for o in p["orders"]] == ["O1", "O2"] and p["lines"] == 3
    r = p["routes"]["list"]
    assert r["ord"] == [0, 1, 1] and len(r["keys"]) == 3 and r["starts"][0] == 0
    assert all(r["pts"][i] for i in r["stops"])  # índices das paradas dentro dos pontos
    assert abs(r["m"] - d1["list_m"]) < 0.02
    assert "error" in day_payload(sample, nodes, start, {}, "2026-12-25")
