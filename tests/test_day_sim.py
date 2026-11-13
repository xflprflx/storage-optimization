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


def test_old_example_projects_get_dated_orders(db):
    """Projeto criado de um exemplo antes da data por pedido: ao iniciar, os pedidos são reimportados do exemplo
    (com data); projeto com pedidos do próprio usuário não muda."""
    import copy

    from models import SiteProject
    from services import site_examples
    from services.site_import import ORDER_COLS, _map, _orders, read_sheets

    ex = site_examples.BY_SLUG["imovel-estreito"]
    rows = read_sheets(site_examples.path_of(ex["slug"]))["Pedidos"]
    res = _orders(rows, _map(rows[0], ORDER_COLS), True)
    old = copy.deepcopy(res)
    old["sample"] = [e[:3] for e in old["sample"]]  # formato antigo: sem a data
    old.pop("days")
    user = {"orders": 1, "lines": 1, "sample": [["X1", ["ABC"], [1]]], "skus": {"ABC": [1, 1]}, "pairs": []}
    a = SiteProject(name="ex antigo", config={"orders": old})
    b = SiteProject(name="do usuário", config={"orders": user})
    db.add_all([a, b])
    db.commit()
    assert "ex antigo" in site_examples.refresh_example_orders(db)
    db.refresh(a)
    db.refresh(b)
    assert all(e[3] for e in a.config["orders"]["sample"]) and a.config["orders"]["days"]
    assert b.config["orders"] == user
    assert site_examples.refresh_example_orders(db) == []  # só uma vez


def test_compare_today_vs_proposal_by_day_and_order():
    """Resultado da otimização: os mesmos pedidos com os SKUs onde estão hoje × na proposta."""
    from services.route_compare import compare
    from services.slotting import Geo

    # endereço 1 perto da referência, 2 longe
    geo = {1: Geo(0, 1.0, 0.0, True, 1.0, 1.0), 2: Geo(0, 30.0, 0.0, True, 30.0, 30.0)}
    addr = {1: "A-01", 2: "A-30"}
    sample = [["O1", ["X"], [1], "2026-10-01"], ["O2", ["X"], [1], "2026-10-01"], ["O3", ["X"], [1], "2026-10-02"]]
    res = compare(sample, {"X": [(2, 5.0)]}, {"X": [(1, 5.0)]}, geo, addr)
    assert res["n_orders"] == 3
    assert abs(res["cur"]["opt_km"] - 3 * 60 / 1000) < 1e-9 and abs(res["new"]["opt_km"] - 3 * 2 / 1000) < 1e-9
    assert res["new"]["opt_h"] < res["cur"]["opt_h"]
    d1 = next(d for d in res["days"] if d["date"] == "2026-10-01")
    assert d1["orders"] == 2 and abs(d1["opt_m_cur"] - 120) < 1e-9 and abs(d1["opt_m_new"] - 4) < 1e-9
    o = res["orders"][0]
    assert o["opt_m_cur"] == 60 and o["opt_m_new"] == 2 and o["opt_t_new"] < o["opt_t_cur"]


def test_3d_payload_today_vs_proposal():
    """3D hoje × proposta: dois separadores (cur/new) na rota escolhida, por pedido e por dia."""
    from services import route_compare

    near, far, start = Node(key="A-01", gx=100, gy=0), Node(key="A-30", gx=3000, gy=0), Node(key="REF", gx=0, gy=0)
    sample = [["O1", ["X"], [1], "2026-10-01"], ["O2", ["X"], [2], "2026-10-01"]]
    cur, new = {"X": [far]}, {"X": [near]}
    ref = {"x": 0, "y": 0, "label": "ref"}
    lst = route_compare.payload_3d(sample, cur, new, start, ref)
    assert lst["compare"] and lst["orders"][0]["opt_cur"] > lst["orders"][0]["opt_new"]
    assert lst["days"][0]["date"] == "2026-10-01" and lst["days"][0]["opt_cur"] > lst["days"][0]["opt_new"]
    one = route_compare.payload_3d(sample, cur, new, start, ref, order="O1", kind="list")
    assert set(one["routes"]) == {"cur", "new"} and one["kind"] == "list"
    assert one["routes"]["cur"]["keys"] == ["A-30"] and one["routes"]["new"]["keys"] == ["A-01"]
    assert one["routes"]["cur"]["m"] > one["routes"]["new"]["m"]
    day = route_compare.payload_3d(sample, cur, new, start, ref, day="2026-10-01")
    assert len(day["orders"]) == 2 and day["routes"]["new"]["ord"] == [0, 1]
    tok = route_compare.save({"branch_id": 1, "before": {}, "after": {}})
    assert route_compare.load(tok)["branch_id"] == 1 and route_compare.load("x") is None
