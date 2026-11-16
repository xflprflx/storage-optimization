"""Separação em lote (vários pedidos por trajeto): no simulador de trajeto (4ª rota) e no hoje × proposta."""
from services.route_compare import compare
from services.route_sim import Node, add_times, simulate, time_params


def _aisle():
    ends = (0.0, 3000.0)
    to_g = lambda e, c, a, b: (e, c, 0, 0)  # noqa: E731
    mk = lambda key, x: Node(key=key, gx=0, gy=0, zone=0, aisle=0, along=x, cross=0.0, ends=ends, to_global=to_g)  # noqa: E731
    nodes = {f"S{i}": [mk(f"A-{i:02d}", i * 100.0)] for i in range(1, 30)}
    return nodes, mk("REF", 0.0)


def test_batch_groups_orders_of_the_same_day_and_cuts_walking():
    nodes, start = _aisle()
    # dois dias; pedidos vizinhos (endereços próximos) devem cair no mesmo trajeto
    sample = [[f"D1-{j}", [f"S{j * 2 + 1}", f"S{j * 2 + 2}"], [1, 1], "2026-11-01"] for j in range(8)] + \
             [[f"D2-{j}", [f"S{j + 20}"], [3], "2026-11-02"] for j in range(3)]
    sim = add_times(simulate(sample, nodes, start, batch=4), time_params({"t_batch": "4"}))
    assert sim["batch"] == 4 and sim["trips_n"] == 3  # 8 pedidos -> 2 trajetos; 3 pedidos do dia 2 -> 1 trajeto
    for t in sim["trips"]:
        assert len({o.split("-")[0] for o in t["orders"]}) == 1  # nunca mistura dias
        assert len(t["orders"]) <= 4
    first = next(t for t in sim["trips"] if "D1-0" in t["orders"])
    assert set(first["orders"]) == {"D1-0", "D1-1", "D1-2", "D1-3"}  # os vizinhos juntos
    assert sim["bopt_total"] < sim["opt_total"] and sim["batch_gain_pct"] > 0
    # cada pedido leva a sua parte do trajeto; peças continuam dele
    rows = {r["order"]: r for r in sim["rows"]}
    assert abs(sum(r["bopt_m"] for r in sim["rows"]) - sum(t["bopt_m"] for t in sim["trips"])) < 1e-6
    assert rows["D2-0"]["trip"] == rows["D2-2"]["trip"]
    assert sim["times"]["bopt_h"] < sim["times"]["opt_h"]
    day1 = next(d for d in sim["days"] if d["date"] == "2026-11-01")
    assert day1["trips"] == 2 and day1["bopt_m"] < day1["opt_m"]
    # sem lote: nada muda
    plain = simulate(sample, nodes, start)
    assert "bopt_total" not in plain and abs(plain["opt_total"] - sim["opt_total"]) < 1e-9


def test_time_params_batch_limits():
    assert time_params({"t_batch": "1"})["t_batch"] == 2
    assert time_params({"t_batch": "99"})["t_batch"] == 20
    assert time_params({"t_batch": "6"})["t_batch"] == 6


def test_compare_has_batch_on_both_sides():
    from services.slotting import Geo

    geo = {i: Geo(0, i * 1.0, 0.0, None, i * 1.0, i * 1.0) for i in range(1, 21)}
    addr = {i: f"A-{i:02d}" for i in geo}
    before = {f"S{i}": [(21 - i, 5.0)] for i in range(1, 21)}  # hoje: os SKUs longe
    after = {f"S{i}": [(i, 5.0)] for i in range(1, 21)}
    sample = [[f"P{j}", [f"S{j + 1}", f"S{j + 2}"], [1, 1], "2026-11-01"] for j in range(10)]
    res = compare(sample, before, after, geo, addr, time_params({"t_batch": "3"}))
    assert res["batch"] == 3
    for side in ("cur", "new"):
        assert res[side]["trips"] == 4 and res[side]["bopt_km"] < res[side]["opt_km"]
    assert "bopt_m_cur" in res["orders"][0] and "bopt_h_new" in res["days"][0]


def test_routes_page_and_compare_card_show_batch(client, seeded):
    page = client.get(f"/optimization/slotting/routes?branch_id={seeded['b']}&warehouse_id={seeded['w']}&t_batch=3").text
    assert 'name="t_batch"' in page and 'value="3"' in page and "Lote (m)" in page
    opt = client.get(f"/optimization/pool?branch_id={seeded['b']}&warehouse_id={seeded['w']}&run=1&slot=1").text
    if 'id="rcBox"' in opt:  # com amostra de pedidos no escopo
        assert 'id="rcBc"' in opt and 'id="rcBn"' in opt
