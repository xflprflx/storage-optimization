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


def test_examples_have_release_and_old_example_projects_get_it(db):
    """As planilhas de exemplo têm a coluna Liberação; projeto criado de um exemplo antes dela (com data, sem horário)
    recebe os horários ao iniciar; projeto do usuário não muda."""
    import copy

    from models import SiteProject
    from services import site_examples
    from services.site_import import read_sheets

    ex = site_examples.BY_SLUG["imovel-estreito"]
    rows = read_sheets(site_examples.path_of(ex["slug"]))["Pedidos"]
    res = _orders(rows, _map(rows[0], ORDER_COLS), True)
    assert res["has_release"] and 0 < res["release_missing"] < res["orders"] * 0.2
    old = copy.deepcopy(res)
    old["sample"] = [e[:4] for e in old["sample"]]  # formato anterior: com data, sem liberação
    old["has_release"] = False
    user = {"orders": 1, "lines": 1, "sample": [["X1", ["ABC"], [1], "2026-10-01"]], "skus": {"ABC": [1, 1]}, "pairs": []}
    a = SiteProject(name="ex sem liberação", config={"orders": old})
    b = SiteProject(name="usuário sem liberação", config={"orders": user})
    db.add_all([a, b])
    db.commit()
    assert "ex sem liberação" in site_examples.refresh_example_orders(db)
    db.refresh(a)
    db.refresh(b)
    assert a.config["orders"]["has_release"] and all(len(e) > 4 for e in a.config["orders"]["sample"])
    assert b.config["orders"] == user
    assert site_examples.refresh_example_orders(db) == []


def test_3d_clock_and_compare_flag():
    from services.route_compare import payload_3d

    nodes, start = _aisle()
    ref = {"x": 0.0, "y": 0.0, "label": "ref"}
    sample = [["A", ["S1"], [1], "2026-11-01", 8 * 3600 + 15 * 60], ["B", ["S3"], [1], "2026-11-01", None]]
    d = day_payload(sample, nodes, start, ref, "2026-11-01")  # opção desligada: o relógio usa o 1º horário mesmo assim
    assert d["clock_s"] == 8 * 3600 + 15 * 60 and not d["clock_guess"]
    plain = day_payload([e[:4] for e in sample], nodes, start, ref, "2026-11-01")
    assert plain["clock_s"] == 8 * 3600 and plain["clock_guess"]  # sem horário: 08:00 presumido
    lst = payload_3d(sample, nodes, nodes, start, ref)
    assert lst["summary"]["has_release"]  # o 3D do hoje × proposta habilita a opção


def test_idle_insights_find_idle_hours_queue_and_pickers():
    from services.route_sim import idle_insights

    nodes, start = _aisle()
    # 8h: 25 pedidos liberados juntos (fila); depois nada até as 10h (ocioso); 10h: mais 2 pedidos
    sample = [[f"P{j:02d}", [f"S{j % 29 + 1}"], [3], "2026-11-01", 8 * 3600] for j in range(25)] + \
             [[f"Q{j}", ["S2"], [1], "2026-11-01", 10 * 3600] for j in range(2)]
    tp = time_params({"t_batch": "3"})
    sim = add_times(simulate(sample, nodes, start, batch=3, release=True, tp=tp), tp)
    ins = sim["insights"]["opt"]
    hours = {r["h"]: r for r in ins["per_hour"]}
    assert hours[8]["released"] == 25 and hours[8]["wait_m"] > 5  # a fila das 8h
    assert ins["worst_wait"]["h"] == 8
    assert hours[9]["idle_m"] > 30  # entre 9h e 10h o separador fica parado
    assert ins["longest_gap"]["to"] == 10 * 3600  # a maior pausa termina quando saem os pedidos das 10h
    kinds = {t["kind"] for t in ins["tips"]}
    assert {"idle", "queue"} <= kinds
    idle_tip = next(t for t in ins["tips"] if t["kind"] == "idle")
    assert idle_tip["windows"] == sorted(idle_tip["windows"])  # janelas em ordem
    assert "bopt" in sim["insights"]  # também em lote
    # sem a opção: sem insights
    assert "insights" not in add_times(simulate(sample, nodes, start, tp=tp), tp)


def test_batch_waits_the_window_from_the_first_release():
    nodes, start = _aisle()
    h8 = 8 * 3600
    # 8h00: 2 pedidos; 8h05: mais 2; 8h30: mais 2 (lote de 4)
    sample = [[f"A{j}", [f"S{j + 1}"], [1], "2026-11-01", h8] for j in range(2)] + \
             [[f"B{j}", [f"S{j + 5}"], [1], "2026-11-01", h8 + 300] for j in range(2)] + \
             [[f"C{j}", [f"S{j + 9}"], [1], "2026-11-01", h8 + 1800] for j in range(2)]

    def trips(wait):
        tp = time_params({"t_batch": "4", "t_bwait": str(wait)})
        sim = add_times(simulate(sample, nodes, start, batch=4, release=True, tp=tp), tp)
        return sim, [sorted(t["orders"]) for t in sim["trips"]]

    sim0, t0 = trips(0)
    assert t0[0] == ["A0", "A1"]  # sem espera: sai com os 2 já liberados
    sim10, t10 = trips(10)
    assert t10[0] == ["A0", "A1", "B0", "B1"]  # espera 5 min e completa o lote
    assert sim10["trips"][0]["start_s"] == 300  # sai na hora em que o lote completou (não aguarda os 10 min)
    assert sim10["insights"]["bopt"]["bwait_day_m"] == 15.0
    # os 2 das 8h30 nunca completam o lote: o separador (parado, esperando) não sai com o 1º — aguarda os 10 min
    # contados da liberação e sai às 8h40 com o que tem
    assert t10[1] == ["C0", "C1"] and sim10["trips"][1]["start_s"] == 1800 + 600
    assert abs(sim10["days"][0]["bopt_bwait"] - 900) < 1e-6  # 5 min + 10 min
    sim3, t3 = trips(3)
    assert t3[0] == ["A0", "A1"]  # os pedidos das 8h05 não cabem numa janela de 3 min...
    assert sim3["trips"][0]["start_s"] == 180  # ...mas ele espera os 3 min antes de sair
    # sem o horário de liberação, a espera não vale (o agrupamento é o de sempre)
    plain = [e[:4] for e in sample]
    tp = time_params({"t_batch": "4", "t_bwait": "30"})
    assert not simulate(plain, nodes, start, batch=4, release=True, tp=tp)["release_on"]
