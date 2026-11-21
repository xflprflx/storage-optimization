"""Fracionar a bin pai na otimização: com bins existentes, uma bin pai que sobra no escopo pode virar compartimentos
do tipo filho marcado com "permitir fracionar a bin pai" (1 bin pai = k compartimentos)."""
from decimal import Decimal

import pytest

from services.bin_optimizer import AddressIn, BinSpec, Params, ProductUnit, greedy_addresses, split_budget, split_report

P = BinSpec(1, "P", 30, 40, 40)
H = BinSpec(2, "1/2 P", 30, 20, 40, front_w=20)  # meia bin (2 × 1)
SMALL = ProductUnit("PEQ", 4, 10, 10, 10)  # cabe numa meia bin


def test_split_budget_takes_spare_parents():
    eff, plan = split_budget({1: 3, 2: 0}, {1: 1, 2: 3}, {2: (1, 2)})
    assert plan == {2: 2} and eff == {1: 1, 2: 4}  # 3 meias pedem 2 bins pai; sobra 1 meia vazia
    eff, plan = split_budget({1: 1}, {1: 1, 2: 1}, {2: (1, 2)})
    assert plan == {} and eff.get(2, 0) == 0  # nenhuma bin pai sobrando
    assert split_budget({1: 3}, {2: 3}, None) == ({1: 3}, {})  # sem a opção: nada muda


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
    assert split_report(res, prm) == [{"parent": 1, "child": 2, "parents": 1, "made": 2, "spare": 1, "k": 2}]


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
    assert s.splits == [{"parent": par.id, "child": half.id, "parents": 1, "made": 2, "spare": 1, "k": 2}]
    plan = build_address_plan(results, s, types, [], [])
    assert plan.steps[0].action == "Fracionar" and "SPB-P" in plan.steps[0].detail
    half.allow_split = False  # sem a opção: a bin pai fica (não há meia bin no escopo)
    db.commit()
    results, s, _t = run(db, [loc.id], Params(fill_limit=1.0, objective="min_volume", limit_inventory=True,
                                               allowed_types={par.id, half.id}))
    assert not s.splits and (results[0].suggested or results[0].current) == {par.id: 1}
