"""Horário de liberação dos pedidos: coluna opcional do histórico e a opção de simular com ele."""
from services.route_sim import Node, add_times, day_payload, simulate, time_params
from services.site_import import ORDER_COLS, _map, _orders, _time_of_day


def test_time_of_day_formats():
    assert _time_of_day("08:30") == 30600 and _time_of_day("8:30:15") == 30615
    assert _time_of_day("2026-10-05 14:05") == 50700 and _time_of_day("2026-10-05T08:30:00") == 30600
    assert _time_of_day("05/10/2026 08:30") == 30600
    assert _time_of_day("0.354166666") == 30600  # fração do dia do Excel
    assert _time_of_day("") is None and _time_of_day("x") is None and _time_of_day("25:00") is None


def test_history_release_column_is_optional_and_missing_is_start():
    rows = [["Pedido", "Data", "SKU", "Quantidade", "Liberação"],
            ["P1", "05/10/2026", "A", "1", "09:00"], ["P1", "05/10/2026", "B", "1", "08:15"],  # o menor horário
            ["P2", "05/10/2026", "A", "2", ""], ["P2", "05/10/2026", "C", "1", ""],  # sem horário em nenhuma linha
            ["P3", "", "A", "1", "2026-10-05 10:00"]]  # sem a coluna Data preenchida: a data vem da liberação
    cm = _map(rows[0], ORDER_COLS)
    assert cm["release"] == 4
    res = _orders(rows, cm, False)
    assert res["has_release"] and res["release_missing"] == 1
    rel = {e[0]: e[4] for e in res["sample"]}
    assert rel == {"P1": 8 * 3600 + 15 * 60, "P2": None, "P3": 10 * 3600}
    assert {e[0]: e[3] for e in res["sample"]}["P3"] == "2026-10-05"
    # sem a coluna: a amostra não tem o 5º campo e a opção fica indisponível
    rows2 = [r[:4] for r in rows]
    res2 = _orders(rows2, _map(rows2[0], ORDER_COLS), False)
    assert not res2["has_release"] and all(len(e) == 4 for e in res2["sample"])


def _aisle():
    to_g = lambda e, c, a, b: (e, c, 0, 0)  # noqa: E731
    mk = lambda key, x: Node(key=key, gx=0, gy=0, zone=0, aisle=0, along=x, cross=0.0, ends=(0.0, 3000.0), to_global=to_g)  # noqa: E731
    return {f"S{i}": [mk(f"A-{i:02d}", i * 100.0)] for i in range(1, 30)}, mk("REF", 0.0)


def test_simulation_waits_for_released_orders():
    nodes, start = _aisle()
    h8, h10 = 8 * 3600, 10 * 3600
    sample = [[f"P{j}", [f"S{j + 1}"], [1], "2026-11-01", h8 if j < 3 else h10] for j in range(6)] + \
             [["PX", ["S20"], [1], "2026-11-01", None]]  # sem horário: início do dia
    tp = time_params({"t_batch": "4"})
    sim = add_times(simulate(sample, nodes, start, batch=4, release=True, tp=tp), tp)
    assert sim["release_on"]
    trips = [set(t["orders"]) for t in sim["trips"]]
    assert trips == [{"P0", "P1", "P2", "PX"}, {"P3", "P4", "P5"}]  # o lote só junta pedidos já liberados
    day = sim["days"][0]
    assert day["start_s"] == h8
    assert day["opt_idle"] > 7000 and day["opt_end"] > 7200  # espera as 10h e termina depois delas
    assert sim["times"]["opt_lead_avg"] < 120  # cada pedido fica pronto logo depois de liberado
    # sem a opção: o lote ignora o horário (pode juntar pedidos das 8h com os das 10h)
    off = add_times(simulate(sample, nodes, start, batch=4, tp=tp), tp)
    assert not off["release_on"] and "opt_end" not in off["days"][0]
    # amostra sem a coluna: a opção não tem efeito
    plain = [e[:4] for e in sample]
    assert not simulate(plain, nodes, start, release=True)["release_on"]


def test_3d_day_follows_release_order_and_waits():
    nodes, start = _aisle()
    ref = {"x": 0.0, "y": 0.0, "label": "ref"}
    sample = [["Z", ["S3"], [1], "2026-11-01", 9 * 3600], ["A", ["S1"], [1], "2026-11-01", 8 * 3600]]
    d = day_payload(sample, nodes, start, ref, "2026-11-01", batch=2, release=True)
    assert d["release"] and [o["order"] for o in d["orders"]] == ["A", "Z"]  # na ordem liberada, não pelo número
    assert d["routes"]["opt"]["rel"] == [0, 3600]
    assert d["routes"]["batch"]["rel"][0] == 0
    assert "rel" not in day_payload(sample, nodes, start, ref, "2026-11-01")["routes"]["opt"]


def test_routes_page_disables_flag_without_column(client, seeded):
    page = client.get(f"/optimization/slotting/routes?branch_id={seeded['b']}&warehouse_id={seeded['w']}&t_release=1").text
    assert 'id="t_release"' in page
    tag = page[page.index('id="t_release"') - 120:page.index('id="t_release"') + 120]
    assert "disabled" in tag  # o histórico de exemplo não tem a coluna Liberação


def test_3d_day_start_is_the_first_release():
    nodes, start = _aisle()
    ref = {"x": 0.0, "y": 0.0, "label": "ref"}
    sample = [["Z", ["S3"], [1], "2026-11-01", 9 * 3600], ["A", ["S1"], [1], "2026-11-01", 8 * 3600]]
    assert day_payload(sample, nodes, start, ref, "2026-11-01", batch=2, release=True)["start_s"] == 8 * 3600
