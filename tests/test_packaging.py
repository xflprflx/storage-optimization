"""Unitização: caixa do produto e palete fechado — capacidade por tipo de bin (Armazena em), sugestões pelas medidas,
cadastros (produto, modelos de embalagem, tipo de bin) e importações."""
from decimal import Decimal

from services.bin_optimizer import BinSpec, Pack, Params, ProductUnit, _cap_units, at_vao, capacity

PRM = Params(fill_limit=1.0)
CASE = Pack(12, 20, 30, 20, 7.0)  # 12 un. numa caixa 20 (alt.) × 30 × 20
UNIT = ProductUnit("P", 0, 10, 10, 10, 0.5, CASE)


def test_capacity_by_store_mode():
    units = BinSpec(1, "U", 40, 40, 40)
    cases = BinSpec(2, "C", 40, 40, 40, store_as="cases")
    both = BinSpec(3, "B", 40, 40, 40, store_as="both")
    n_units = _cap_units(UNIT, units, PRM)
    n_cases = _cap_units(ProductUnit("", 0, 20, 30, 20, 7.0), cases, PRM)
    assert capacity(UNIT, units, PRM) == n_units == 64
    assert capacity(UNIT, cases, PRM) == n_cases * 12 and n_cases == 4  # caixas inteiras
    assert capacity(UNIT, both, PRM) == n_cases * 12  # caixas inteiras + o resto solto (a caixa aberta ocupa uma caixa)
    no_case = ProductUnit("Q", 0, 10, 10, 10, 0.5)
    assert capacity(no_case, cases, PRM) == n_units  # sem caixa cadastrada: unidades
    only_case = ProductUnit("P", 0, 10, 10, 10, 0.5, Pack(12, 20, 30, 20, 7.0, pref="cases"))
    assert capacity(only_case, units, PRM) == 0  # "sempre na caixa": não vai para bin de unidades
    loose = ProductUnit("P", 0, 10, 10, 10, 0.5, Pack(12, 20, 30, 20, 7.0, pref="units"))
    assert capacity(loose, both, PRM) == n_units


def test_closed_pallet_uses_layers_that_fit_the_level():
    pal = BinSpec(9, "PAL", 15, 100, 120, max_weight=1000, open_top=True, store_as="pallet")
    unit = ProductUnit("P", 0, 10, 10, 10, 0.5, Pack(12, 20, 30, 20, 7.0, layer=10, layers=8))
    assert capacity(unit, at_vao({9: pal}, 160)[9], PRM) == 10 * 7 * 12  # 155 cm úteis: 7 camadas de 20 cm
    assert capacity(unit, at_vao({9: pal}, 300)[9], PRM) == 10 * 8 * 12  # todas as 8 camadas
    heavy = ProductUnit("P", 0, 10, 10, 10, 0.5, Pack(12, 20, 30, 20, 20.0, layer=10, layers=8))
    assert capacity(heavy, at_vao({9: pal}, 300)[9], PRM) == 50 * 12  # 1000 kg ÷ 20 kg = 50 caixas


def test_suggestions_and_checks():
    from types import SimpleNamespace

    from services.packaging import check, fit_in_case, suggest_pallet

    assert fit_in_case((10, 10, 10), None, (20, 30, 20)) == 12
    pallet = SimpleNamespace(code="PAL", width=Decimal(100), length=Decimal(120), over_cm=None, max_weight=Decimal(1000))
    layer, layers = suggest_pallet((20, 30, 20), 7.0, pallet)
    assert layer == 20 and layers == 7  # 100 × 120 com caixas 30 × 20 = 20 por camada; 150 cm ÷ 20 cm = 7 camadas
    p = SimpleNamespace(case_qty=20, height=10, width=10, length=10, weight=None, case_height=20, case_width=30,
                        case_length=20, case_weight=None, case_model=None, pallet_layer=25, pallet_layers=None)
    txt = " ".join(check(p, pallet=pallet))
    assert "não cabem na caixa" in txt and "cabem até 12" in txt and "até 20" in txt


def test_product_form_bin_type_and_models(client, db):
    from models import BinType, PackagingModel, Product

    r = client.post("/packaging-models/new", data={"code": "PK-CX40", "name": "Caixa 40", "height": "20", "width": "30",
                                                     "length": "20", "tare": "0,5"}, follow_redirects=False)
    assert r.status_code == 303
    m = db.query(PackagingModel).filter_by(code="PK-CX40").one()
    r = client.post("/bin-types/new", data={"code": "PK-PAL", "name": "Palete", "height": "15", "width": "100",
                                             "length": "120", "max_weight": "1000", "store_as": "pallet", "open_top": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    pal = db.query(BinType).filter_by(code="PK-PAL").one()
    assert pal.store_as == "pallet"
    calc = client.post("/products/packaging-calc", data={"height": "10", "width": "10", "length": "10", "weight": "0,5",
                                                         "case_model_id": str(m.id), "pallet_type_id": str(pal.id)}).json()
    assert calc["case_qty"] == 12 and (calc["pallet_layer"], calc["pallet_layers"]) == (20, 7)
    # salvar sem unidades por caixa: calculadas pelas medidas do modelo; o palete também
    r = client.post("/products/new", data={"sku": "PK-SKU", "name": "Produto", "height": "10", "width": "10",
                                           "length": "10", "weight": "0,5", "case_model_id": str(m.id),
                                           "pallet_type_id": str(pal.id), "store_pref": "auto"}, follow_redirects=False)
    assert r.status_code == 303
    p = db.query(Product).filter_by(sku="PK-SKU").one()
    assert (p.case_qty, p.pallet_layer, p.pallet_layers) == (12, 20, 7)
    assert "12 un." in client.get(f"/products/{p.id}").text
    assert client.post(f"/packaging-models/{m.id}/delete", follow_redirects=False).status_code == 303
    assert db.query(PackagingModel).filter_by(id=m.id).count() == 1  # em uso: fica
    # unidades por caixa sem medidas de caixa: recusado
    r = client.post("/products/new", data={"sku": "PK-BAD", "name": "x", "case_qty": "5"})
    assert r.status_code == 422


def test_imports(db, tmp_path):
    from models import BinType, PackagingModel, Product
    from services.master_importer import MASTER_KINDS, MasterOptions
    from services.product_importer import ImportOptions, import_products
    from tests.test_structure_import import _map

    text = "Codigo;Nome;Altura (cm);Largura (cm);Comprimento (cm);Armazena em\nPKI-CX;Caixa;40;40;40;ambos\nPKI-PAL;Palete;15;100;120;palete\n"
    p = tmp_path / "b.csv"
    p.write_text(text, encoding="utf-8")
    k = MASTER_KINDS["bin_types"]
    res = k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions())
    assert not res.errors
    assert {b.code: b.store_as for b in db.query(BinType).filter(BinType.code.like("PKI-%"))} == {"PKI-CX": "both", "PKI-PAL": "pallet"}
    db.add(PackagingModel(code="PKI-M", name="M", height=Decimal(20), width=Decimal(30), length=Decimal(20)))
    db.commit()
    from services.product_importer import PRODUCT_FIELDS

    text = ("SKU;Descricao;Altura;Largura;Comprimento;Peso;Unidades por caixa;Modelo de embalagem;Palete;Caixas por camada;Camadas\n"
            "PKI-1;A;10;10;10;0,5;;PKI-M;PKI-PAL;;\nPKI-2;B;10;10;10;0,5;30;PKI-M;;;\n")
    p2 = tmp_path / "p.csv"
    p2.write_text(text, encoding="utf-8")
    r = import_products(db, p2, _map(text.splitlines()[0], PRODUCT_FIELDS), ImportOptions())
    a, b = db.query(Product).filter_by(sku="PKI-1").one(), db.query(Product).filter_by(sku="PKI-2").one()
    assert (a.case_qty, a.pallet_layer, a.pallet_layers) == (12, 20, 7)
    assert b.case_qty == 30 and any("não cabem na caixa" in w for _l, w in r.warnings)


def test_address_optimization_counts_whole_cases(db):
    """60 unidades: numa bin de unidades cabem 64 (1 bin); na mesma bin guardando caixas de 12, cabem 4 caixas = 48
    (2 bins)."""
    from models import BinType, Branch, LocationBin, Product, StockItem, StorageLocation
    from services.optimization_service import run

    br = Branch(code="PKE", name="Caixas")
    bu = BinType(code="PKE-U", name="U", height=Decimal(40), width=Decimal(40), length=Decimal(40))
    bc = BinType(code="PKE-C", name="C", height=Decimal(40), width=Decimal(40), length=Decimal(40), store_as="cases")
    pr = Product(sku="PKE-P", name="P", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.5"),
                 case_qty=12, case_height=Decimal(20), case_width=Decimal(30), case_length=Decimal(20))
    db.add_all([br, bu, bc, pr])
    db.flush()
    loc = StorageLocation(branch_id=br.id, aisle="K", bay="01", level="1", position="1", compartment="", address="K-01-1-1")
    db.add(loc)
    db.flush()
    db.add(LocationBin(location_id=loc.id, bin_type_id=bu.id, quantity=1))
    db.add(StockItem(product_id=pr.id, location_id=loc.id, quantity=Decimal(60)))
    db.commit()
    base = dict(fill_limit=1.0, objective="min_bins", respect_levels=False)
    r_units, _s, _t = run(db, [loc.id], Params(**base, allowed_types={bu.id}))
    r_cases, _s, _t = run(db, [loc.id], Params(**base, allowed_types={bc.id}))
    got = lambda r: (r[0].suggested if r[0].applicable else r[0].current)  # noqa: E731
    assert got(r_units) == {bu.id: 1} and got(r_cases) == {bc.id: 2}
