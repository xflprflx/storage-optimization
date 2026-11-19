"""Portas das estruturas (área de abertura, colisão na planta, corredor no projeto de filial nova, tempo no
simulador, 3D) e condição de armazenagem (projeto de filial nova, pool, recomposição, cadastros)."""
import pytest

from services.doors import door_of, frame, hits, leaves, sample, sectors

SWING1 = {"type": "swing", "leaves": 1, "hinge": "left", "angle": 90, "time": 6.0}


def test_door_geometry_opens_to_the_aisle_side():
    # fileira horizontal com o corredor do lado maior (ff): a porta varre y > y + d
    pts = sample(100, 200, 100, 60, True, True, SWING1)
    assert pts and all(p[1] > 260 - 1e-6 for p in pts)
    assert max(p[1] for p in pts) <= 260 + 100 + 1e-6  # raio = a frente (1 folha)
    # corredor do lado menor: varre y < y
    assert all(p[1] < 200 for p in sample(100, 200, 100, 60, True, False, SWING1))
    # fileira vertical (frente ao longo de Y), corredor à direita
    assert all(p[0] > 160 - 1e-6 for p in sample(100, 200, 60, 100, False, True, SWING1))
    # 2 folhas: metade da frente cada, dobradiças nas duas pontas; 180° = meio círculo
    assert [r for _a, _s, r in leaves({**SWING1, "leaves": 2}, 100)] == [50, 50]
    two = sectors(100, 200, 100, 60, True, False, {**SWING1, "leaves": 2, "angle": 180})
    assert len(two) == 2 and all(len(pg) == 14 for pg in two)
    # dobradiça à direita (vista de frente): o arco começa na outra ponta
    o, eu, _et, W, _D = frame(100, 200, 100, 60, True, False)
    assert o == (100, 200) and eu == (1.0, 0.0) and W == 100
    assert sectors(100, 200, 100, 60, True, False, {**SWING1, "hinge": "right"})[0][0] == (200.0, 200.0)
    # porta de correr e sem porta: nada a varrer
    assert sample(0, 0, 100, 60, True, True, {"type": "sliding", "time": 0}) == []
    assert hits([(5, 5)], [(0, 0, 10, 10, "x")]) == "x" and hits([(50, 50)], [(0, 0, 10, 10, "x")], (40, 40)) == "@wall"

    class K:
        door_type, door_leaves, door_hinge, door_angle, door_time_s = "swing", 2, "left", 180, 4

    assert door_of(K()) == {"type": "swing", "leaves": 2, "hinge": "left", "angle": 180, "time": 4.0}


def test_drawn_plan_warns_when_a_door_hits_the_facing_row():
    from services.branch_layout import Bay, auto_layout, build, clean, svg

    def bays(door):
        return {a: [Bay(a, f"{i:02d}", 100.0, 60.0, 200.0, 4, door=door) for i in range(1, 5)] for a in ("A", "B")}

    data = clean(auto_layout(bays(None), aisle_w=90))  # corredor de 90 cm: a folha de 100 cm bate na fileira da frente
    g = build(data, bays(SWING1))
    assert g.doors and any(dr["hit"] for dr in g.doors)
    assert any("a porta bate em a fileira" in w for w in g.warn)
    assert "<polygon" in svg(g)
    wide = clean(auto_layout(bays(None), aisle_w=300))
    g2 = build(wide, bays(SWING1))
    assert g2.doors and not any(dr["hit"] for dr in g2.doors)
    sliding = build(wide, bays({"type": "sliding", "time": 3.0}))
    assert sliding.doors == [] and sliding.nodes[("A", "01")].door == ("A-01", 3.0)


def test_route_sim_adds_door_time_once_per_door_per_order_and_per_trip():
    from services.route_sim import Node, add_times, simulate, time_params

    a = Node("A-01-1", 0, 100, door=("A-01", 6.0))
    b = Node("A-01-2", 10, 100, door=("A-01", 6.0))  # a mesma estante (a mesma porta)
    c = Node("B-01-1", 300, 100)
    tp = time_params({"t_batch": "2"})
    sim = add_times(simulate([["P1", ["S1", "S2", "S3"], [1, 1, 1]], ["P2", ["S1"], [1]]],
                             {"S1": [a], "S2": [b], "S3": [c]}, Node("REF", 0, 0), batch=2, tp=tp), tp)
    rows = {r["order"]: r for r in sim["rows"]}
    assert rows["P1"]["door_s"] == 6.0 and rows["P2"]["door_s"] == 6.0
    assert rows["P1"]["batch_door_s"] == 3.0  # no lote: 6 s no trajeto, divididos entre os 2 pedidos
    plain = add_times(simulate([["P1", ["S3"], [1]]], {"S3": [c]}, Node("REF", 0, 0)), time_params({}))
    assert "door_s" not in plain["rows"][0] and plain["times"]["door_avg"] == 0


def _site(aisle_m, cold_demand=True):
    from services.bin_optimizer import BinSpec, ProductUnit
    from services.site_planner import Building, Demand, SiteParams, StructureSpec, plan_site

    amb = StructureSpec(1, "EST", "Estante", 100, 60, 200, 4, 45)
    cold = StructureSpec(2, "CAM", "Câmara", 100, 60, 200, 4, 45, condition="refrigerado", door=dict(SWING1))
    dem = [Demand(f"A{i}", "a", "", ProductUnit(f"A{i}", 20, 10, 10, 10)) for i in range(5)]
    if cold_demand:
        dem += [Demand(f"F{i}", "f", "", ProductUnit(f"F{i}", 20, 10, 10, 10), condition="refrigerado") for i in range(3)]
    return plan_site(Building(length_m=20, width_m=12, clear_height_m=4, aisle_m=aisle_m), [amb, cold],
                     {10: BinSpec(10, "CX", 20, 30, 40)}, dem, SiteParams(time_limit=5))


def test_new_site_keeps_conditions_apart_and_needs_room_for_the_door():
    plan = _site(2.0)
    where = {}
    for m in plan.modules:
        for lv in m.levels:
            for pos in lv.positions:
                where.setdefault(pos.sku[0], set()).add(m.struct.code)
    assert where == {"A": {"EST"}, "F": {"CAM"}} and plan.shortage == 0
    # corredor de 1,5 m < folha de 1 m + 80 cm: a câmara não entra; os refrigerados ficam sem lugar
    narrow = _site(1.5)
    assert any("a porta abre 100 cm" in n for n in narrow.notes)
    assert not any(m.struct.code == "CAM" for m in narrow.modules)
    assert {d.sku for d in narrow.demands if d.status == "falta"} == {"F0", "F1", "F2"}


def test_pool_runs_one_pool_per_condition(monkeypatch):
    from services import pool_service, structure_kinds
    from services.pool_optimizer import PoolBin, SkuDemand

    bins = [PoolBin(1, 10, "A-1", 1, 1), PoolBin(2, 20, "C-1", 1, 1)]
    dem = {"S1": SkuDemand("S1", 100, None, 1, {}), "S2": SkuDemand("S2", 200, None, 1, {})}
    monkeypatch.setattr(structure_kinds, "location_conditions", lambda db, ids: {10: "", 20: "refrigerado"})
    monkeypatch.setattr(structure_kinds, "product_conditions", lambda db, ids: {100: "", 200: "refrigerado"})
    out = pool_service._split_by_condition(None, {(1, None): {"bins": bins, "demands": dem, "reserved": set()}})
    assert set(out) == {(1, None), (1, None, "refrigerado")}
    assert [b.id for b in out[(1, None, "refrigerado")]["bins"]] == [2] and list(out[(1, None, "refrigerado")]["demands"]) == ["S2"]
    assert list(out[(1, None)]["demands"]) == ["S1"]
    monkeypatch.setattr(structure_kinds, "location_conditions", lambda db, ids: {10: "", 20: ""})
    monkeypatch.setattr(structure_kinds, "product_conditions", lambda db, ids: {100: "", 200: ""})
    same = {(1, None): {"bins": bins, "demands": dem, "reserved": set()}}
    assert pool_service._split_by_condition(None, same) is same  # sem condição: nada muda


def test_registers_save_door_and_condition(client, db):
    from models import Product, StructureKind
    from services.site_import import condition_code

    r = client.post("/structure-kinds/new", data={"name": "Câmara DR", "condition": "refrigerado", "door_type": "swing",
                                                  "door_leaves": "2", "door_angle": "180", "door_hinge": "right",
                                                  "door_time_s": "6"}, follow_redirects=False)
    assert r.status_code == 303
    k = db.query(StructureKind).filter_by(name="Câmara DR").one()
    assert (k.condition, k.door_type, k.door_leaves, k.door_angle, k.door_hinge, float(k.door_time_s)) == \
        ("refrigerado", "swing", 2, 180, "right", 6.0)
    page = client.get(f"/structure-kinds/{k.id}/edit").text
    assert 'value="refrigerado" selected' in page and 'value="swing" selected' in page
    assert "porta" in client.get("/structure-kinds").text
    r = client.post("/products/new", data={"sku": "DR-SKU", "name": "Iogurte", "storage_condition": "refrigerado"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.query(Product).filter_by(sku="DR-SKU").one().storage_condition == "refrigerado"
    assert [condition_code(t) for t in ("Refrigerado", "congelados", "Frio", "seguro", "seco", "")] == \
        ["refrigerado", "congelado", "refrigerado", "controlado", "", ""]
