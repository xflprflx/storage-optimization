"""Fracionar a bin pai na otimização: com bins existentes, uma bin pai que sobra no escopo pode virar compartimentos
do tipo filho marcado com "permitir fracionar a bin pai" (1 bin pai = k compartimentos)."""
from decimal import Decimal

import pytest

from services.bin_optimizer import AddressIn, BinSpec, Params, ProductUnit, greedy_addresses, split_report

P = BinSpec(1, "P", 30, 40, 40)
H = BinSpec(2, "1/2 P", 30, 20, 40, front_w=20)  # meia bin (2 × 1)
SMALL = ProductUnit("PEQ", 4, 10, 10, 10)  # cabe numa meia bin


def test_family_counts_parents():
    from services.bin_family import family_stock, over_families, report

    sp = {2: (1, 2)}
    cur = [{1: 1}, {2: 3}]  # 1 bin pai inteira + 3 meias (2 bins pai divididas, uma com meia vazia)
    assert family_stock(cur, sp) == {1: 3}
    assert over_families(cur, [{2: 2}, {2: 4}], sp) == []  # 1 + 2 = 3 bins pai: fraciona a inteira
    assert over_families(cur, [{2: 3}, {2: 3}], sp) == [1]  # 2 + 2 = 4 > 3
    rep = report(cur, [{1: 1}, {1: 1, 2: 1}], sp)  # junta uma das bins divididas
    assert rep == [{"parent": 1, "child": 2, "k": 2, "split": 0, "join": 1, "empty": 1, "before": 2, "after": 1}]


@pytest.mark.parametrize("engine", ["heuristic", "or"])
def test_existing_bins_split_a_parent_only_when_allowed(engine):
    types = {1: P, 2: H}

    def run(splits):
        prm = Params(fill_limit=1.0, objective="min_volume", limit_inventory=True, engine=engine, splits=splits,
                     time_limit=5)
        addrs = [AddressIn(10, "A-01-1-1", {1: 1}, [SMALL])]
        if engine == "or":
            from services.or_engine import solve_addresses

            res, _info = solve_addresses(addrs, types, prm, None)
        else:
            res = greedy_addresses(addrs, types, prm)
        return res, prm

    res, prm = run(None)
    assert (res[0].suggested or res[0].current) == {1: 1} and split_report(res, prm) == []  # sem meia no escopo
    res, prm = run({2: (1, 2)})
    assert res[0].applicable and res[0].suggested == {2: 1}
    assert split_report(res, prm) == [{"parent": 1, "child": 2, "k": 2, "split": 1, "join": 0, "empty": 1, "before": 0,
                                       "after": 1}]


BIG = ProductUnit("GRANDE", 1, 25, 30, 30)  # só cabe na bin inteira


@pytest.mark.parametrize("engine", ["heuristic", "or"])
def test_existing_bins_join_compartments_back_into_a_parent(engine):
    types = {1: P, 2: H}
    prm = Params(fill_limit=1.0, objective="min_volume", limit_inventory=True, engine=engine, splits={2: (1, 2)},
                 time_limit=5)
    addrs = [AddressIn(11, "A-01-1-1", {2: 2}, [BIG])]  # 2 meias = 1 bin pai dividida; o produto precisa da inteira
    if engine == "or":
        from services.or_engine import solve_addresses

        res, _info = solve_addresses(addrs, types, prm, None)
    else:
        res = greedy_addresses(addrs, types, prm)
    assert res[0].applicable and res[0].suggested == {1: 1}
    assert split_report(res, prm) == [{"parent": 1, "child": 2, "k": 2, "split": 0, "join": 1, "empty": 0, "before": 1,
                                       "after": 0}]
    prm.splits = None  # sem a opção: não há bin pai inteira no escopo
    res = greedy_addresses([AddressIn(11, "A-01-1-1", {2: 2}, [BIG])], types, prm)
    assert not (res[0].applicable and res[0].suggested == {1: 1})


def test_form_and_import_with_allow_split(client, db, tmp_path):
    from models import BinType
    from services.master_importer import MASTER_KINDS, MasterOptions
    from tests.test_structure_import import _map

    par = BinType(code="SPL-P", name="Pai", height=Decimal(30), width=Decimal(40), length=Decimal(40))
    db.add(par)
    db.commit()
    r = client.post("/bin-types/new", data={"code": "SPL-H", "name": "Meia", "parent_id": str(par.id), "div_w": "2",
                                             "div_l": "1", "allow_split": "1"}, follow_redirects=False)
    assert r.status_code == 303
    h = db.query(BinType).filter_by(code="SPL-H").one()
    assert h.allow_split and h.is_fraction
    r = client.post("/bin-types/new", data={"code": "SPL-X", "name": "Inteira", "allow_split": "1"}, follow_redirects=False)
    assert r.status_code == 303 and not db.query(BinType).filter_by(code="SPL-X").one().allow_split  # sem pai: ignora
    text = "Codigo;Nome;Bin pai;Divisoes largura;Divisoes comprimento;Permite fracionar a bin pai\nSPL-Q;Quarto;SPL-P;2;2;sim\nSPL-Z;Ruim;;;;sim\n"
    p = tmp_path / "b.csv"
    p.write_text(text, encoding="utf-8")
    k = MASTER_KINDS["bin_types"]
    res = k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions())
    assert db.query(BinType).filter_by(code="SPL-Q").one().allow_split
    assert len(res.errors) == 1  # sem bin pai: recusada


def test_address_run_reports_and_plans_the_split(db):
    from models import BinType, Branch, LocationBin, Product, ShelfLevel, StockItem, StorageLocation
    from services.action_plan import build_address_plan
    from services.optimization_service import run

    br = Branch(code="SPB", name="Fracionar")
    par = BinType(code="SPB-P", name="Pai", height=Decimal(30), width=Decimal(40), length=Decimal(40))
    db.add_all([br, par])
    db.flush()
    half = BinType(code="SPB-H", name="Meia", height=Decimal(30), width=Decimal(20), length=Decimal(40), parent_id=par.id,
                   div_w=2, div_l=1, fraction_num=1, fraction_den=2, split_axis="grid", allow_split=True)
    pr = Product(sku="SPB-PEQ", name="Pequeno", height=Decimal(10), width=Decimal(10), length=Decimal(10))
    db.add_all([half, pr])
    db.flush()
    db.add(ShelfLevel(branch_id=br.id, aisle="S", bay="01", level="1", height_cm=Decimal(40), notes=""))
    loc = StorageLocation(branch_id=br.id, aisle="S", bay="01", level="1", position="1", compartment="", address="S-01-1-1")
    db.add(loc)
    db.flush()
    db.add(LocationBin(location_id=loc.id, bin_type_id=par.id, quantity=1))
    db.add(StockItem(product_id=pr.id, location_id=loc.id, quantity=Decimal(4)))
    db.commit()
    prm = Params(fill_limit=1.0, objective="min_volume", limit_inventory=True, allowed_types={par.id, half.id})
    results, s, types = run(db, [loc.id], prm)
    assert results[0].suggested == {half.id: 1}
    assert s.splits == [{"parent": par.id, "child": half.id, "k": 2, "split": 1, "join": 0, "empty": 1, "before": 0,
                         "after": 1}]
    plan = build_address_plan(results, s, types, [], [])
    assert plan.steps[0].action == "Fracionar" and "SPB-P" in plan.steps[0].detail
    half.allow_split = False  # sem a opção: a bin pai fica (não há meia bin no escopo)
    db.commit()
    results, s, _t = run(db, [loc.id], Params(fill_limit=1.0, objective="min_volume", limit_inventory=True,
                                               allowed_types={par.id, half.id}))
    assert not s.splits and (results[0].suggested or results[0].current) == {par.id: 1}


# --------------------------------------------------------------------------- #
# Pool (bins fixas no lugar): irmãs no mesmo pai, juntar o que esvazia, fracionar para quem faltou
# --------------------------------------------------------------------------- #
def _pool_bins(spec):
    from services.pool_optimizer import PoolBin

    out, nid = [], 0
    for lid, addr, t, n, orig in spec:
        for i in range(n):
            nid += 1
            out.append(PoolBin(nid, lid, addr, t, i + 1, type_code={1: "P", 2: "1/2 P"}[t], orig=dict(orig.get(i, {}))))
    return out


def test_pool_consolidates_siblings_and_joins_the_empty_parent():
    from services.pool_optimizer import PoolParams, SkuDemand, optimize_pool

    types = {1: P, 2: H}
    bins = _pool_bins([(1, "L1", 2, 2, {0: {"X": 4}}), (2, "L2", 2, 2, {0: {"Y": 4}})])  # 2 bins pai divididas, meio usadas
    dem = [SkuDemand("X", 1, ProductUnit("X", 4, 10, 10, 10), 4, {1: 4}), SkuDemand("Y", 2, ProductUnit("Y", 4, 10, 10, 10), 4, {2: 4})]
    res = optimize_pool(bins, dem, types, PoolParams(objective="min_bins", fill_limit=1.0, splits={2: (1, 2)}))
    assert not res.shortage
    where = {p.sku: {b.location_id for b, _m in p.allocations} for p in res.plans}
    assert where["X"] == where["Y"]  # as irmãs ficam na mesma bin pai
    joins = [op for op in res.reshape if op["kind"] == "join"]
    assert len(joins) == 1 and joins[0]["location_id"] != next(iter(where["X"]))
    assert {b.type_id for b in res.bins if b.location_id == joins[0]["location_id"]} == {1}  # voltou a ser 1 bin pai
    # sem a opção: nada muda de forma
    bins = _pool_bins([(1, "L1", 2, 2, {0: {"X": 4}}), (2, "L2", 2, 2, {0: {"Y": 4}})])
    assert not optimize_pool(bins, dem, types, PoolParams(objective="min_bins", fill_limit=1.0)).reshape


def test_pool_splits_a_parent_for_a_sku_without_bin():
    from services.pool_optimizer import PoolParams, SkuDemand, optimize_pool

    types = {1: P, 2: H}
    bins = _pool_bins([(1, "L1", 1, 1, {0: {"A": 4}})])  # 1 bin pai com pouco (cabe numa meia)
    dem = [SkuDemand("A", 1, ProductUnit("A", 4, 10, 10, 10), 4, {1: 4}),
           SkuDemand("B", 2, ProductUnit("B", 4, 10, 10, 10), 4, {})]  # B não tem bin
    prm = PoolParams(objective="min_bins", fill_limit=1.0, allow_split=True)
    assert optimize_pool(_pool_bins([(1, "L1", 1, 1, {0: {"A": 4}})]), dem, types, prm).shortage
    prm.splits = {2: (1, 2)}
    res = optimize_pool(bins, dem, types, prm)
    assert not res.shortage
    assert [op["kind"] for op in res.reshape] == ["split"] and {b.type_id for b in res.bins} == {2}


def test_pool_apply_joins_compartments_in_the_database(client, db):
    from models import BinType, Branch, LocationBin, Product, StockItem, StorageLocation
    from services.bin_content_service import refresh_contents
    from services.pool_optimizer import PoolParams
    from services.pool_service import apply_pool

    br = Branch(code="SPJ", name="Juntar")
    par = BinType(code="SPJ-P", name="Pai", height=Decimal(30), width=Decimal(40), length=Decimal(40))
    db.add_all([br, par])
    db.flush()
    half = BinType(code="SPJ-H", name="Meia", height=Decimal(30), width=Decimal(20), length=Decimal(40), parent_id=par.id,
                   div_w=2, div_l=1, fraction_num=1, fraction_den=2, split_axis="grid", allow_split=True)
    db.add(half)
    db.flush()
    locs = []
    for i, sku in enumerate(("SPJ-X", "SPJ-Y")):
        pr = Product(sku=sku, name=sku, height=Decimal(10), width=Decimal(10), length=Decimal(10))
        loc = StorageLocation(branch_id=br.id, aisle="J", bay=f"0{i + 1}", level="1", position="1", compartment="",
                              address=f"J-0{i + 1}-1-1")
        db.add_all([pr, loc])
        db.flush()
        db.add(LocationBin(location_id=loc.id, bin_type_id=half.id, quantity=2))
        db.add(StockItem(product_id=pr.id, location_id=loc.id, quantity=Decimal(4)))
        locs.append(loc.id)
    db.commit()
    refresh_contents(db, locs, force=True)
    db.commit()
    q = (f"branch_id={br.id}&warehouse_id=&run=1&objective=min_bins&engine=heuristic&bins_mode=existing&basis=stock"
         f"&fill_limit=100&geometry=1&rotation=upright&weight=1&types={par.id}&types={half.id}")
    html = client.get(f"/optimization/pool?{q}").text
    assert "Bins pai fracionadas e juntadas" in html and "Juntar <b>2× SPJ-H</b> em <b>1× SPJ-P</b>" in html
    prm = PoolParams(objective="min_bins", fill_limit=1.0, allowed_types={par.id, half.id})
    apply_pool(db, locs, prm)
    rows = {(lb.location_id, lb.bin_type_id): lb.quantity for lb in db.query(LocationBin).filter(LocationBin.location_id.in_(locs))}
    stock = {si.location_id for si in db.query(StockItem).filter(StockItem.location_id.in_(locs), StockItem.quantity > 0)}
    assert len(stock) == 1  # os dois SKUs na mesma bin pai dividida
    kept, freed = (locs[0], locs[1]) if locs[0] in stock else (locs[1], locs[0])
    assert rows == {(kept, half.id): 2, (freed, par.id): 1}  # a outra voltou a ser uma bin pai inteira


def test_pool_keeps_siblings_apart_when_it_would_worsen_the_walk_or_the_golden_zone():
    from types import SimpleNamespace as G

    from services.pool_family import _not_worse

    geo = {1: G(walk_m=10, golden=True, ease=1), 2: G(walk_m=10.5, golden=True, ease=1),
           3: G(walk_m=40, golden=True, ease=5), 4: G(walk_m=9, golden=False, ease=1)}
    assert _not_worse(geo, 1, 2)  # até 1 m a mais: junta as irmãs
    assert not _not_worse(geo, 1, 3)  # 30 m a mais: fica onde está
    assert not _not_worse(geo, 1, 4)  # sairia da zona de ouro da pega
    assert _not_worse(None, 1, 3)  # sem a posição pela saída: só o agrupamento conta
