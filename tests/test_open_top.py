"""Bin aberta em cima: a carga pode passar da borda — até o vão do nível (menos a folga) ou até o máximo acima da
borda do tipo — no Por endereço, no Pool (a menor altura entre os níveis das bins do tipo) e no projeto de filial
nova (só com o máximo; o nível precisa comportar a bin + o máximo)."""
from decimal import Decimal

from services.bin_optimizer import BinSpec, Params, ProductUnit, at_vao, capacity, open_height

PAL = BinSpec(1, "PAL", 15, 100, 120, open_top=True)  # palete: 15 cm de altura
TALL = ProductUnit("ALTO", 8, 40, 50, 60)  # 40 cm de altura: não cabe nos 15 cm do palete


def test_usable_height_goes_up_to_the_level_or_the_maximum():
    prm = Params(fill_limit=1.0)
    assert capacity(TALL, PAL, prm) == 0  # sem o nível: só a altura da bin
    assert open_height(PAL, 100) == 95  # 100 de vão − 5 de folga
    assert capacity(TALL, at_vao({1: PAL}, 100)[1], prm) == 8  # 2 camadas × 2 × 2
    capped = BinSpec(1, "PAL", 15, 100, 120, open_top=True, over_cm=50)
    assert open_height(capped, 100) == 65 and capacity(TALL, at_vao({1: capped}, 100)[1], prm) == 4
    assert open_height(capped, None) == 65  # nível sem vão: vale o máximo do tipo
    closed = BinSpec(1, "CX", 15, 100, 120)
    assert open_height(closed, 100) is None and capacity(TALL, at_vao({1: closed}, 100)[1], prm) == 0
    assert open_height(PAL, 18) is None  # vão sem espaço acima da borda
    # o volume útil acompanha a altura útil (o limite de volume não corta a carga acima da borda)
    assert at_vao({1: PAL}, 100)[1].unit_volume_cm3 == 95 * 100 * 120


def test_address_mode_uses_the_vao_of_the_address_level():
    from services.bin_optimizer import AddressIn, optimize_address

    prm = Params(fill_limit=1.0, objective="min_bins")
    a = AddressIn(1, "A-01-1-1", {1: 1}, [TALL], vao=100)
    r = optimize_address(a, {1: PAL}, prm)
    assert r.suggested == {1: 1} and r.status != "inviavel"
    low = AddressIn(2, "A-01-2-1", {1: 1}, [TALL], vao=30)  # 30 − 5 = 25 cm: o produto de 40 cm não cabe
    assert optimize_address(low, {1: PAL}, prm).status == "inviavel"


def test_pool_uses_the_smallest_height_among_the_levels(db):
    from models import Branch, ShelfLevel, StorageLocation
    from services.pool_optimizer import PoolBin
    from services.pool_service import _open_types

    br = Branch(code="OT", name="Aberta em cima")
    db.add(br)
    db.flush()
    locs = []
    for lv, h in (("1", 60), ("2", 120)):
        db.add(ShelfLevel(branch_id=br.id, aisle="P", bay="01", level=lv, height_cm=Decimal(h), notes=""))
        loc = StorageLocation(branch_id=br.id, aisle="P", bay="01", level=lv, position="1", compartment="",
                              address=f"P-01-{lv}-1")
        db.add(loc)
        locs.append(loc)
    db.flush()
    bins = [PoolBin(i + 1, loc.id, loc.address, 1, 1) for i, loc in enumerate(locs)]
    types, note = _open_types(db, {1: PAL}, bins, unlimited=False)
    assert types[1].unit_h == 15 + (60 - 5 - 15) and "PAL" in note  # o nível de 60 cm limita
    types, _n = _open_types(db, {1: PAL}, bins[1:], unlimited=False)
    assert types[1].unit_h == 120 - 5
    types, _n = _open_types(db, {1: PAL}, bins, unlimited=True)  # bins ilimitadas: sem nível, só o máximo (nenhum)
    assert types[1].unit_h is None
    db.rollback()


def test_new_branch_project_needs_the_maximum_and_a_level_that_holds_it():
    from services.site_planner import SiteParams, StructureSpec, level_capacity
    from services.site_service import open_types_for_plan

    capped = BinSpec(1, "PAL", 15, 100, 120, open_top=True, over_cm=40)
    types, note = open_types_for_plan({1: capped, 2: PAL})
    assert types[1].unit_h == 55 and types[2].unit_h is None and "PAL +40 cm" in note and "PAL" in note
    prm = SiteParams()
    low = StructureSpec(1, "E", "E", 270, 120, 300, 3, 50)  # vão de 50: não comporta 15 + 40 + 5
    high = StructureSpec(2, "E2", "E2", 270, 120, 300, 3, 70)
    assert level_capacity(types[1], low, prm) == 0 and level_capacity(types[1], high, prm) == 2
    assert level_capacity(PAL, low, prm) == 2  # fechada (ou sem máximo): só a altura da bin


def test_bin_type_form_import_and_packing_drawing(client, db):
    from models import BinType
    from services.packing import arrange, svg_mini

    r = client.post("/bin-types/new", data={"code": "OT-PAL", "name": "Palete aberto", "height": "15", "width": "100",
                                            "length": "120", "open_top": "1", "over_cm": "60"}, follow_redirects=False)
    assert r.status_code == 303
    bt = db.query(BinType).filter_by(code="OT-PAL").one()
    assert bt.open_top and bt.over_cm == Decimal("60")
    bad = client.post("/bin-types/new", data={"code": "OT-X", "name": "x", "height": "10", "width": "10", "length": "10",
                                              "open_top": "1", "over_cm": "0"})
    assert bad.status_code == 422
    # sem a opção, o máximo é ignorado
    client.post(f"/bin-types/{bt.id}/edit", data={"code": "OT-PAL", "name": "Palete", "height": "15", "width": "100",
                                                  "length": "120", "over_cm": "60"})
    db.expire_all()
    assert not db.get(BinType, bt.id).open_top and db.get(BinType, bt.id).over_cm is None
    # desenho: a carga passa da borda (o produto de 40 cm num palete de 15)
    arr = arrange("PAL", (15, 100, 120), [("ALTO", 8, (40, 50, 60))], open_top=True)
    assert arr.items[0].overflow == 0 and arr.load_h == 80 and "borda da bin" in svg_mini(arr, 160)
    closed = arrange("CX", (15, 100, 120), [("ALTO", 8, (40, 50, 60))])
    assert closed.load_h is None and not closed.items[0].per_bin
    db.delete(db.get(BinType, bt.id))
    db.commit()


def test_address_optimization_end_to_end_reads_the_level_vao(db):
    """Endereço real: o vão do nível (cadastro) chega ao otimizador e o produto mais alto que o palete cabe."""
    from models import BinType, Branch, LocationBin, Product, ShelfLevel, StockItem, StorageLocation
    from services.optimization_service import run

    br = Branch(code="OT2", name="Aberta em cima 2")
    pal = BinType(code="OT2-PAL", name="Palete", height=Decimal(15), width=Decimal(100), length=Decimal(120), open_top=True)
    pr = Product(sku="OT2-ALTO", name="Produto alto", height=Decimal(40), width=Decimal(50), length=Decimal(60))
    db.add_all([br, pal, pr])
    db.flush()
    ids = {}
    for lv, h in (("1", 100), ("2", 30)):
        db.add(ShelfLevel(branch_id=br.id, aisle="Q", bay="01", level=lv, height_cm=Decimal(h), notes=""))
        loc = StorageLocation(branch_id=br.id, aisle="Q", bay="01", level=lv, position="1", compartment="",
                              address=f"Q-01-{lv}-1")
        db.add(loc)
        db.flush()
        db.add(LocationBin(location_id=loc.id, bin_type_id=pal.id, quantity=1))
        db.add(StockItem(product_id=pr.id, location_id=loc.id, quantity=Decimal(8)))
        ids[lv] = loc.id
    db.commit()
    prm = Params(fill_limit=1.0, objective="min_bins", allowed_types={pal.id})  # só o palete (o banco é compartilhado)
    results, _s, _t = run(db, [ids["1"], ids["2"]], prm)
    by = {r.id: r for r in results}
    assert by[ids["1"]].status != "inviavel" and by[ids["1"]].suggested.get(pal.id) == 1  # vão de 100: 8 cabem
    assert by[ids["2"]].status == "inviavel"  # vão de 30: o produto de 40 cm não cabe


def test_cd_example_uses_an_open_pallet(client, db):
    """Exemplo "CD com porta-paletes e picking": o palete é uma bin de 15 cm aberta em cima, com até 135 cm acima da
    borda — a planilha cadastra o tipo assim e o projeto põe os itens volumosos em paletes no porta-palete."""
    import copy
    import re
    from urllib.parse import unquote_plus, urlencode

    from models import BinType, SiteProject
    from services.file_reader import token_path
    from services.site_import import auto_mapping, read_sheets
    from services.site_service import merged, run_project

    r = client.post("/optimization/site/new", data={"sample": "cd-paletes-picking", "name": "CD aberto"})
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    action = re.search(r'action="(/optimization/site/(\d+)/upload/confirm)"', r.text)
    mapping = auto_mapping(read_sheets(token_path(token)))
    assert {"open_top", "over_cm"} <= set(mapping["bins"]["cols"])
    fields = {"token": token, "orders_abs": "1"}
    for kind, v in mapping.items():
        fields[f"sheet_{kind}"] = v["sheet"]
        for fld, i in v["cols"].items():
            fields[f"col_{kind}_{fld}"] = "" if i is None else str(i)
    r = client.post(action.group(1), content=urlencode(fields), headers={"Content-Type": "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert "level=danger" not in r.headers["location"], unquote_plus(r.headers["location"])
    pal = db.query(BinType).filter_by(code="PALETE").one()
    if float(pal.height) == 15:  # o banco dos testes pode já ter o palete antigo (150 cm, fechado): ele é mantido
        assert pal.open_top and float(pal.over_cm) == 135
    cfg = merged(copy.deepcopy(db.get(SiteProject, int(action.group(2))).config))
    cfg["params"].update({"time_limit": "10"})
    plan, errs = run_project(db, cfg)
    assert not errs and not plan.errors
    on_pallet = [p for p in plan.positions if p.type_id == pal.id]
    assert on_pallet and all(p.struct_code.startswith("PP") for p in on_pallet)
