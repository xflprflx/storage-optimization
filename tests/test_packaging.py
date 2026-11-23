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


def test_choice_default_first_then_alternatives_and_pallet_configs():
    """Embalagem padrão primeiro; as outras só onde a padrão não serve (embalagens diferentes por bin). No palete,
    a paletização cadastrada para aquele palete que mais guarda; sem paletização, calculada."""
    from services.bin_optimizer import choose

    big = Pack(48, 45, 33, 43, 30.0, name="Master", pallets={9: [(4, 3), (4, 5)]})
    small = BinSpec(3, "B", 40, 40, 40, store_as="both")
    pal = BinSpec(9, "PAL", 15, 100, 120, max_weight=1000, open_top=True, store_as="pallet")
    other_pal = BinSpec(8, "PAL2", 15, 100, 120, max_weight=1000, open_top=True, store_as="pallet")
    only_big = ProductUnit("P", 0, 10, 10, 10, 0.5, big)
    assert choose(only_big, small).how == "units"  # master não cabe e não há alternativa: unidades soltas
    mixed = ProductUnit("P", 0, 10, 10, 10, 0.5, big, (CASE,))
    ch = choose(mixed, small)
    assert ch.how == "cases" and ch.pack is CASE  # a caixa entra onde o master não cabe
    ch = choose(mixed, at_vao({9: pal}, 300)[9])
    assert (ch.pack is big, ch.layer, ch.layers) == (True, 4, 5)  # a paletização mais alta que cabe
    ch = choose(mixed, at_vao({9: pal}, 120)[9])
    assert capacity(mixed, at_vao({9: pal}, 120)[9], PRM) == 4 * 2 * 48  # 105 cm úteis: 2 camadas de 45 cm
    assert choose(mixed, other_pal).layer is None  # sem paletização para este palete: calculada
    from dataclasses import replace

    no_pal = ProductUnit("P", 0, 10, 10, 10, 0.5, replace(big, no_pallet=True))
    assert choose(no_pal, pal).how == "units"  # unitização "só caixas"


def test_suggestions_hierarchy_and_checks():
    from types import SimpleNamespace

    from services.packaging import _K, check, fill, fit_in_case, suggest_pallet

    assert fit_in_case((10, 10, 10), None, (20, 30, 20)) == 12
    pallet = SimpleNamespace(code="PAL", width=Decimal(100), length=Decimal(120), over_cm=None, max_weight=Decimal(1000))
    layer, layers = suggest_pallet((20, 30, 20), 7.0, pallet)
    assert layer == 20 and layers == 7  # 100 × 120 com caixas 30 × 20 = 20 por camada; 150 cm ÷ 20 cm = 7 camadas
    prod = SimpleNamespace(height=10, width=10, length=10, weight=0.5)
    disp = _K(name="Display", inner_qty=None, model=None, height=20, width=30, length=20, weight=None)
    fill(disp, prod)
    assert disp.inner_qty == 12 and disp.units() == 12
    case = _K(name="Caixa", inner=disp, inner_qty=None, model=None, height=40, width=60, length=20, weight=None)
    case.pallets = [SimpleNamespace(pallet_type_id=1, pallet_type=pallet, layer=None, layers=None)]
    fill(case, prod)
    assert case.inner_qty == 4 and case.units() == 48  # 4 displays de 12
    assert (case.pallets[0].layer, case.pallets[0].layers) == (10, 3)  # base 60 × 20: 10 por camada; 150 cm ÷ 40 cm
    bad = _K(name="Caixa", inner_qty=20, model=None, height=20, width=30, length=20, weight=None)
    bad.pallets = [SimpleNamespace(pallet_type_id=1, pallet_type=pallet, layer=25, layers=None)]
    txt = " ".join(check(bad, prod))
    assert "20 não cabem" in txt and "cabem até 12" in txt and "até 20" in txt


def test_unitization_switch_branch_run_and_sku(db):
    """Filial desligada (padrão) ignora as embalagens; a rodada pode escolher outro modo (simulação); SKU "nunca" fica
    em unidades; a embalagem só vale nas filiais em que está habilitada."""
    from models import BinType, Branch, LocationBin, PackagingModel, Product, ProductPackaging, StockItem, StorageLocation
    from services.optimization_service import run

    br = Branch(code="PKE", name="Caixas")
    bu = BinType(code="PKE-U", name="U", height=Decimal(40), width=Decimal(40), length=Decimal(40))
    bc = BinType(code="PKE-C", name="C", height=Decimal(40), width=Decimal(40), length=Decimal(40), store_as="cases")
    m = PackagingModel(code="PKE-CX", name="Caixa", height=Decimal(20), width=Decimal(30), length=Decimal(20))
    pr = Product(sku="PKE-P", name="P", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.5"))
    k = ProductPackaging(model=m, name="PKE-CX", inner_qty=12)
    pr.packagings.append(k)
    db.add_all([br, bu, bc, m, pr])
    db.flush()
    loc = StorageLocation(branch_id=br.id, aisle="K", bay="01", level="1", position="1", compartment="", address="K-01-1-1")
    db.add(loc)
    db.flush()
    db.add(LocationBin(location_id=loc.id, bin_type_id=bu.id, quantity=1))
    db.add(StockItem(product_id=pr.id, location_id=loc.id, quantity=Decimal(60)))
    db.commit()
    base = dict(fill_limit=1.0, objective="min_bins", respect_levels=False, allowed_types={bc.id})
    got = lambda r: (r[0].suggested if r[0].applicable else r[0].current)  # noqa: E731
    br.unitization = "full"
    db.commit()
    assert got(run(db, [loc.id], Params(**base))[0]) == {bc.id: 1}  # embalagem não habilitada na filial: unidades
    k.branches.append(br)
    db.commit()
    assert got(run(db, [loc.id], Params(**base))[0]) == {bc.id: 2}  # caixas de 12: 4 por bin = 48 → 2 bins
    assert got(run(db, [loc.id], Params(**base, unitization="off"))[0]) == {bc.id: 1}  # a rodada restringe
    br.unitization = "off"
    db.commit()
    assert got(run(db, [loc.id], Params(**base))[0]) == {bc.id: 1}  # filial desligada
    assert got(run(db, [loc.id], Params(**base, unitization="full"))[0]) == {bc.id: 2}  # a rodada simula com caixas
    br.unitization = "full"
    pr.unitization = "never"
    db.commit()
    assert got(run(db, [loc.id], Params(**base))[0]) == {bc.id: 1}  # SKU nunca


def test_screens_product_packagings_branch_and_models(client, db):
    from models import BinType, Branch, PackagingModel, Product, ProductPackaging

    for code, h, w, ln in (("PK-CX40", "20", "30", "20"), ("PK-MST", "40", "60", "40")):
        r = client.post("/packaging-models/new", data={"code": code, "name": code, "height": h, "width": w,
                                                         "length": ln, "tare": "0,5"}, follow_redirects=False)
        assert r.status_code == 303
    m = db.query(PackagingModel).filter_by(code="PK-CX40").one()
    mm = db.query(PackagingModel).filter_by(code="PK-MST").one()
    r = client.post("/bin-types/new", data={"code": "PK-PAL", "name": "Palete", "height": "15", "width": "100",
                                             "length": "120", "max_weight": "1000", "store_as": "pallet", "open_top": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    pal = db.query(BinType).filter_by(code="PK-PAL").one()
    r = client.post("/branches/new", data={"code": "PKB", "name": "Filial", "unitization": "cases"}, follow_redirects=False)
    assert r.status_code == 303
    fb = db.query(Branch).filter_by(code="PKB").one()
    assert fb.unitization == "cases"
    r = client.post("/products/new", data={"sku": "PK-SKU", "name": "Produto", "height": "10", "width": "10",
                                           "length": "10", "weight": "0,5", "store_pref": "auto",
                                           "unitization": "branch", "mixed_packs": "1"}, follow_redirects=False)
    assert r.status_code == 303
    p = db.query(Product).filter_by(sku="PK-SKU").one()
    assert p.mixed_packs and p.unitization == "branch"
    base = f"/products/{p.id}/packagings"
    assert "Nenhuma embalagem" in client.get(base).text
    assert f'value="{fb.id}" id="br{fb.id}" checked' in client.get(base + "/new").text  # filial com unitização: marcada
    calc = client.post(base + "/calc", data={"model_id": str(m.id), "pal_idx": "n0", "pal_type_n0": str(pal.id)}).json()
    assert calc["inner_qty"] == 12 and calc["pallets"]["n0"] == [20, 7]
    # salvar sem quantidade nem paletização: calculadas pelas medidas do modelo; habilitada na filial PKB
    r = client.post(base + "/new", data={"model_id": str(m.id), "pal_idx": "n0", "sort": "1",
                                         "pal_type_n0": str(pal.id), "branch_ids": str(fb.id)}, follow_redirects=False)
    assert r.status_code == 303
    cx = db.query(ProductPackaging).filter_by(product_id=p.id, model_id=m.id).one()
    db.refresh(cx)
    assert cx.branch_ids == {fb.id} and cx.inner_qty == 12 and (cx.pallets[0].layer, cx.pallets[0].layers) == (20, 7)
    assert client.post(base + "/new", data={"model_id": str(m.id)}).status_code == 422  # o mesmo modelo de novo
    r = client.post(base + "/new", data={"model_id": str(mm.id), "inner_id": str(cx.id), "inner_qty": "4", "sort": "2"},
                    follow_redirects=False)  # master sem filial habilitada
    assert r.status_code == 303
    db.expire_all()
    ms = db.query(ProductPackaging).filter_by(product_id=p.id, model_id=mm.id).one()
    assert not ms.branches and ms.units() == 48
    page = client.get(base).text
    assert "PK-MST" in page and "48" in page and "nenhuma (não entra na otimização)" in page
    from services.packaging import product_packs

    db.expire_all()
    prod = db.get(Product, p.id)
    pk, alts = product_packs(prod, "full", fb.id)
    assert pk.qty == 12 and alts == ()  # só a caixa (habilitada na filial) vai para o otimizador
    assert product_packs(prod, "full", fb.id + 999) == (None, ())  # outra filial: nenhuma habilitada
    # a caixa está dentro do master: não pode ser excluída; o modelo em uso também não
    client.post(f"{base}/{cx.id}/delete")
    assert db.query(ProductPackaging).filter_by(id=cx.id).count() == 1
    client.post(f"/packaging-models/{m.id}/delete")
    assert db.query(PackagingModel).filter_by(id=m.id).count() == 1
    assert "PK-MST" in client.get(f"/products/{p.id}").text


def test_imports(db, tmp_path):
    from models import BinType, Branch, PackagingModel, Product
    from services.master_importer import MASTER_KINDS, MasterOptions
    from services.product_importer import PRODUCT_FIELDS, ImportOptions, import_products
    from tests.test_structure_import import _map

    text = "Codigo;Nome;Altura (cm);Largura (cm);Comprimento (cm);Armazena em\nPKI-CX;Caixa;40;40;40;ambos\nPKI-PAL;Palete;15;100;120;palete\n"
    p = tmp_path / "b.csv"
    p.write_text(text, encoding="utf-8")
    k = MASTER_KINDS["bin_types"]
    res = k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions())
    assert not res.errors
    assert {b.code: b.store_as for b in db.query(BinType).filter(BinType.code.like("PKI-%"))} == {"PKI-CX": "both", "PKI-PAL": "pallet"}
    for code, h, w, ln in (("PKI-M", 20, 30, 20), ("PKI-D", 10, 30, 20), ("PKI-MS", 20, 60, 40)):
        db.add(PackagingModel(code=code, name=code, height=Decimal(h), width=Decimal(w), length=Decimal(ln)))
    if not db.query(Branch).filter_by(code="PKI").count():
        db.add(Branch(code="PKI", name="Filial PKI"))
    db.commit()
    # colunas do produto = a embalagem do modelo informado (ou de um modelo criado pelas medidas da caixa)
    text = ("SKU;Descricao;Altura;Largura;Comprimento;Peso;Unidades por caixa;Modelo de embalagem;Altura da caixa;"
            "Largura da caixa;Comprimento da caixa;Palete;Caixas por camada;Camadas;Unitizacao do SKU\n"
            "PKI-1;A;10;10;10;0,5;;PKI-M;;;;PKI-PAL;;;\nPKI-2;B;10;10;10;0,5;30;PKI-M;;;;;;;nunca\n"
            "PKI-3;C;10;10;10;0,5;8;;20;40;20;;;;\n")
    p2 = tmp_path / "p.csv"
    p2.write_text(text, encoding="utf-8")
    r = import_products(db, p2, _map(text.splitlines()[0], PRODUCT_FIELDS), ImportOptions())
    a, b, c = (db.query(Product).filter_by(sku=s).one() for s in ("PKI-1", "PKI-2", "PKI-3"))
    ka, kb, kc = a.packagings[0], b.packagings[0], c.packagings[0]
    assert ka.model.code == "PKI-M" and ka.inner_qty == 12 and (ka.pallets[0].layer, ka.pallets[0].layers) == (20, 7)
    assert {x.code for x in ka.branches} >= {"PKI"}  # nova: habilitada em todas as filiais
    assert kb.inner_qty == 30 and b.unitization == "never" and any("não cabem" in w for _l, w in r.warnings)
    assert kc.model.code == "EMB-20X40X20" and kc.inner_qty == 8  # modelo criado pelas medidas da caixa
    # importação de embalagens: hierarquia por modelo, filiais habilitadas e dois paletes para a mesma embalagem
    text = ("SKU;Modelo de embalagem;Quantidade;Feita de;Filiais habilitadas;Palete;Caixas por camada;Camadas\n"
            "PKI-1;PKI-D;6;;nenhuma;;;\nPKI-1;PKI-MS;4;PKI-D;PKI;PKI-PAL;5;3\n"
            "PKI-1;PKI-MS;4;PKI-D;PKI;PKI-PAL;5;6\nPKI-1;PKI-M;2;NAO-EXISTE;;;;\n")
    p3 = tmp_path / "k.csv"
    p3.write_text(text, encoding="utf-8")
    kk = MASTER_KINDS["packagings"]
    res = kk["run"](db, p3, _map(text.splitlines()[0], kk["fields"]), MasterOptions())
    assert len(res.errors) == 1 and "não encontrada" in res.errors[0][1]
    db.expire_all()
    a = db.query(Product).filter_by(sku="PKI-1").one()
    by = {x.model.code: x for x in a.packagings}
    assert not by["PKI-D"].branches and [b.code for b in by["PKI-MS"].branches] == ["PKI"]
    assert by["PKI-MS"].units() == 24 and sorted(pp.layers for pp in by["PKI-MS"].pallets) == [3, 6]
    assert len(a.packagings) == 3  # PKI-M (das colunas), PKI-D e PKI-MS


def test_migration_of_product_cases(db):
    """Migração: a caixa e o palete gravados no produto pela versão anterior viram uma embalagem do produto, com um
    modelo criado pelas medidas da caixa, habilitada em todas as filiais."""
    from sqlalchemy import text

    from database import _migrate_product_cases, engine
    from models import Branch, Product, ProductPackaging

    pr = Product(sku="PKM-1", name="M", height=Decimal(10), width=Decimal(10), length=Decimal(10))
    db.add(pr)
    if not db.query(Branch).filter_by(code="PKM").count():
        db.add(Branch(code="PKM", name="M"))
    db.commit()
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(products)"))}
        if "case_qty" not in cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN case_qty INTEGER"))
            for c in ("case_model_id", "case_height", "case_width", "case_length", "case_weight", "pallet_type_id",
                      "pallet_layer", "pallet_layers"):
                if c not in cols:
                    conn.execute(text(f"ALTER TABLE products ADD COLUMN {c} BIGINT"))
        conn.execute(text("UPDATE products SET case_qty = 6, case_model_id = NULL, case_height = 210000, "
                          "case_width = 310000, case_length = 410000 WHERE sku = 'PKM-1'"))
        _migrate_product_cases(conn)
    db.expire_all()
    k = db.query(ProductPackaging).filter_by(product_id=pr.id).one()
    assert (k.inner_qty, k.model.code, float(k.model.height)) == (6, "EMB-21X31X41", 21.0)
    assert db.query(Branch).count() == len(k.branches)


def test_project_sheet_and_examples_bring_packaging(db, tmp_path):
    """A planilha do projeto (exemplo CD paletes + picking) traz Embalagens, Armazena em e as colunas de embalagem
    dos SKUs: a importação cadastra os modelos e a demanda do projeto usa caixa e palete."""
    from models import BinType, PackagingModel
    from services.site_examples import BY_SLUG, write_workbook
    from services.site_import import import_file, template_xlsx
    from services.site_service import load_uploaded_demand, merged

    path = write_workbook(BY_SLUG["cd-paletes-picking"], tmp_path / "ex.xlsx")
    cfg, msgs, errs = import_file(db, path, merged(None))
    assert not [e for e in errs if not e.startswith("Pedidos")], errs  # pedidos: sem a conversão (outro teste)
    assert db.query(PackagingModel).filter_by(code="CX-P").one().height == 15
    assert db.query(BinType).filter_by(code="PALETE").one().store_as == "pallet"
    assert db.query(BinType).filter_by(code="BIN-8").one().store_as == "both"
    assert any("Embalagem:" in m for m in msgs)
    assert cfg["demand"]["unitization"] == "full"  # linha Unitização da aba Imóvel
    items = {it["sku"]: it for it in cfg["items"]}
    cim = next(it for it in items.values() if it["name"].startswith("Cimento"))
    assert (cim["case_qty"], cim["pallet"], cim["pallet_layer"], cim["pallet_layers"]) == (1, "PALETE", 2, 10)
    boxed = next(it for it in items.values() if it.get("case_model"))
    demands, _notes = load_uploaded_demand(db, cfg["items"], cfg["demand"])
    by = {d.sku: d for d in demands}
    pal = db.query(BinType).filter_by(code="PALETE").one()
    assert by[cim["sku"]].unit.pack.pallets[pal.id] == [(2, 10)]
    assert by[boxed["sku"]].unit.pack.qty == boxed["case_qty"] and by[boxed["sku"]].unit.pack.h
    # sem unidades por caixa: calculadas pelas medidas da caixa
    it = {"sku": "PKX-1", "h": 10, "w": 10, "l": 10, "wt": 0.5, "stock": 30, "case_model": "CX-P"}
    d, notes = load_uploaded_demand(db, [it], {"basis": "stock", "unitization": "off"})
    assert d[0].unit.pack is None  # projeto com a unitização desligada: unidades soltas
    d, notes = load_uploaded_demand(db, [it], {"basis": "stock", "unitization": "full"})
    assert d[0].unit.pack.qty == 1 * 2 * 3 and any("calculadas" in n for n in notes)
    # o modelo da planilha traz as colunas novas
    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(template_xlsx(db, cfg)), read_only=True)
    assert "Embalagens" in wb.sheetnames
    head = next(wb["SKUs"].iter_rows(values_only=True))
    assert "Unidades por caixa" in head and "Camadas" in head


def test_unitization_example_sku_packagings(db, tmp_path):
    """Exemplo de unitização: a aba Embalagens dos SKUs vira a lista de embalagens de cada SKU no projeto (hierarquia,
    habilitada ou não, várias paletizações); SKU "nunca" e embalagem não habilitada ficam em unidades."""
    from models import BinType
    from services.site_examples import BY_SLUG, write_workbook
    from services.site_import import import_file
    from services.site_service import load_uploaded_demand, merged

    cfg, msgs, errs = import_file(db, write_workbook(BY_SLUG["unitizacao"], tmp_path / "u.xlsx"), merged(None))
    assert not [e for e in errs if not e.startswith("Pedidos")], errs  # pedidos: saídas negativas (opção de valor absoluto)
    assert cfg["demand"]["unitization"] == "full" and len(cfg["sku_packs"]) == 11
    assert any("SACO-50" in m and "cabem até 3" in m for m in msgs)  # lastro maior que a base: aviso
    d = {x.sku: x.unit for x in load_uploaded_demand(db, cfg["items"], cfg["demand"], cfg["sku_packs"])[0]}
    pal = db.query(BinType).filter_by(code="PALETE").one()
    p1 = d["UN0001"]
    assert p1.pack.qty == 96 and p1.alts == ()  # só o master (4 caixas de 4 displays de 6) habilitado
    assert sorted(p1.pack.pallets[pal.id]) == [(6, 2), (6, 4)]  # palete baixo e alto
    assert d["UN0012"].pack.qty == 4 and [a.qty for a in d["UN0012"].alts] == [24]  # master = 6 caixas (do modelo)
    assert d["UN0002"].pack.qty == 12 and d["UN0002"].alts == ()  # sem embalagens diferentes por bin
    assert d["UN0005"].pack is None  # SKU "nunca"
    assert d["UN0009"].pack is None  # embalagem não habilitada
    assert d["UN0010"].pack.qty and d["UN0010"].pack.qty > 1  # quantidade calculada pelas medidas
    assert d["UN0006"].pack.pref == "cases" and d["UN0007"].pack.pref == "units"


def test_bulk_apply_skus_then_level_above_and_calc_weight(client, db):
    """Lote: marcar SKUs e aplicar a caixa (quantidade calculada por SKU); marcar as caixas e aplicar o master por
    cima; o Calcular devolve o peso da embalagem cheia."""
    from models import BinType, Branch, PackagingModel, Product, ProductPackaging

    br = db.query(Branch).filter_by(code="PKL").first() or Branch(code="PKL", name="Lote", unitization="full")
    cx = PackagingModel(code="PKL-CX", name="Caixa", height=Decimal(20), width=Decimal(30), length=Decimal(20),
                        tare=Decimal("0.5"))
    ms = PackagingModel(code="PKL-MS", name="Master", height=Decimal(40), width=Decimal(60), length=Decimal(40))
    pal = BinType(code="PKL-PAL", name="Palete", height=Decimal(15), width=Decimal(100), length=Decimal(120),
                  max_weight=Decimal(1000), store_as="pallet", open_top=True)
    a = Product(sku="PKL-A", name="A", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.5"))
    b = Product(sku="PKL-B", name="B", height=Decimal(5), width=Decimal(10), length=Decimal(10), weight=Decimal("0.2"))
    db.add_all([br, cx, ms, pal, a, b])
    db.commit()
    page = client.post("/packagings/apply", data={"ids": [str(a.id), str(b.id)], "back": "/products"})
    assert page.status_code == 200 and "2 SKU(s)" in page.text
    r = client.post("/packagings/apply/confirm", data={
        "mode": "skus", "ids": f"{a.id},{b.id}", "model_id": str(cx.id), "branch_ids": str(br.id),
        "pal_idx": "n0", "pal_type_n0": str(pal.id), "back": "/products"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    ka = next(k for k in db.get(Product, a.id).packagings if k.model_id == cx.id)
    kb = next(k for k in db.get(Product, b.id).packagings if k.model_id == cx.id)
    assert (ka.inner_qty, kb.inner_qty) == (12, 24)  # calculadas pelas medidas de cada SKU
    assert [x.code for x in ka.branches] == ["PKL"] and (ka.pallets[0].layer, ka.pallets[0].layers) == (20, 7)
    assert "PKL-A" in client.get(f"/packagings?model={cx.id}").text
    # nível acima: marcar as caixas e aplicar o master (quantidade calculada: 4 caixas 20 × 30 × 20 no 40 × 60 × 40)
    page = client.post("/packagings/apply", data={"mode": "level", "ids": [str(ka.id), str(kb.id)]})
    assert page.status_code == 200 and "PKL-CX: 2" in page.text
    r = client.post("/packagings/apply/confirm", data={"mode": "level", "ids": f"{ka.id},{kb.id}",
                                                      "model_id": str(ms.id), "branch_ids": str(br.id)},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    ma = next(k for k in db.get(Product, a.id).packagings if k.model_id == ms.id)
    assert ma.inner.model_id == cx.id and ma.inner_qty == 8 and ma.units() == 96
    # remover: a caixa de B está dentro do master de B (não marcado) e fica; a de A sai junto com o master de A
    ka_id, kb_id, ma_id = ka.id, kb.id, ma.id
    mb_id = next(k.id for k in db.get(Product, b.id).packagings if k.model_id == ms.id)
    r = client.post("/packagings/delete", data={"ids": [str(ka_id), str(ma_id), str(kb_id)]}, follow_redirects=False)
    assert "mantida" in r.headers["location"]
    db.expire_all()
    assert db.query(ProductPackaging).filter(ProductPackaging.id.in_([ka_id, ma_id])).count() == 0
    assert db.query(ProductPackaging).filter(ProductPackaging.id.in_([kb_id, mb_id])).count() == 2
    # Calcular: o peso da embalagem cheia = 24 × 0,2 + tara 0,5
    calc = client.post(f"/products/{b.id}/packagings/calc", data={"model_id": str(cx.id), "inner_qty": "24"}).json()
    assert calc["weight"] == 5.3


def test_model_hierarchy_imports_and_3d(client, db, tmp_path):
    """Hierarquia no modelo (independe dos produtos): aplicar o master cria a caixa no SKU; importação de modelos e de
    filiais com unitização; o 3D do exemplo traz como cada bin guarda e as caixas dos paletes."""
    from models import Branch, PackagingModel, Product, SiteProject
    from services.master_importer import MASTER_KINDS, MasterOptions
    from tests.test_structure_import import _map

    text = ("Codigo;Nome;Altura (cm);Largura (cm);Comprimento (cm);Tara (kg);Contem;Quantidade\n"
            "PKH-CX;Caixa;20;30;20;0,5;;\nPKH-MS;Master;40;60;40;1;PKH-CX;\n")
    p = tmp_path / "m.csv"
    p.write_text(text, encoding="utf-8")
    k = MASTER_KINDS["packaging_models"]
    res = k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions())
    assert not res.errors, res.errors
    ms = db.query(PackagingModel).filter_by(code="PKH-MS").one()
    assert [(c.inner_model.code, c.qty) for c in ms.contents] == [("PKH-CX", 8)]  # vazia: quantas cabem
    text = "Codigo;Nome;Unitizacao\nPKH;Filial H;caixas e paletes\n"
    p = tmp_path / "b.csv"
    p.write_text(text, encoding="utf-8")
    k = MASTER_KINDS["branches"]
    assert not k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions()).errors
    br = db.query(Branch).filter_by(code="PKH").one()
    assert br.unitization == "full"
    pr = Product(sku="PKH-1", name="H", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.5"))
    db.add(pr)
    db.commit()
    r = client.post("/packagings/apply/confirm", data={"mode": "skus", "ids": str(pr.id), "model_id": str(ms.id),
                                                      "branch_ids": str(br.id)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    by = {x.model.code: x for x in db.get(Product, pr.id).packagings}
    assert set(by) == {"PKH-CX", "PKH-MS"}  # a caixa foi criada pela hierarquia do modelo
    assert by["PKH-MS"].inner is by["PKH-CX"] and by["PKH-CX"].inner_qty == 12 and by["PKH-MS"].units() == 96
    assert float(by["PKH-CX"].weight) == 6.5 and float(by["PKH-MS"].weight) == 53.0  # peso cheia gravado
    assert [b.code for b in by["PKH-CX"].branches] == ["PKH"]
    # 3D do exemplo de unitização
    from services.site_examples import BY_SLUG, write_workbook
    from services.site_import import import_file
    from services.site_service import merged

    cfg, _m, errs = import_file(db, write_workbook(BY_SLUG["unitizacao"], tmp_path / "u.xlsx"), merged(None))
    sp = SiteProject(name="PKH-3D", config=cfg)
    db.add(sp)
    db.commit()
    js = client.get(f"/optimization/site/{sp.id}/plan3d.json").json()
    hows = {b[14] for b in js["bins"]}
    assert hows == {"u", "c", "p"} and js["loads"]
    master = [b for b in js["bins"] if b[10] == "UN0001"]
    assert master and all(b[14] == "p" and "MST-PAR" in b[15] and "cadastrada" in b[15] for b in master)
    assert any(b[10] == "UN0002" and b[14] == "c" for b in js["bins"])  # sempre na caixa: bandeja de caixas


def test_model_contents_list_calc_and_example_orders(client, db, tmp_path):
    """Contém é uma lista (vale o 1º que o produto já tem; senão o 1º); o Calcular da tela do modelo mostra quantos
    cabem e avisa o que não cabe; o exemplo de unitização traz pedidos."""
    from models import PackagingModel, Product, ProductPackaging

    for code, h, w, ln in (("PKL2-P", 20, 30, 20), ("PKL2-M", 20, 30, 40), ("PKL2-G", 50, 50, 50)):
        db.add(PackagingModel(code=code, name=code, height=Decimal(h), width=Decimal(w), length=Decimal(ln)))
    db.commit()
    ids = {m.code: m.id for m in db.query(PackagingModel).filter(PackagingModel.code.like("PKL2-%"))}
    calc = client.post("/packaging-models/calc", data={
        "height": "40", "width": "60", "length": "40", "cont_idx": ["a", "b", "c"],
        "cont_model_a": str(ids["PKL2-P"]), "cont_model_b": str(ids["PKL2-M"]), "cont_qty_b": "9",
        "cont_model_c": str(ids["PKL2-G"])}).json()
    assert calc["rows"] == {"a": 8, "b": 4, "c": 0}
    assert any("9 × PKL2-M" in w for w in calc["warnings"]) and any("PKL2-G" in w and "não cabe" in w for w in calc["warnings"])
    r = client.post("/packaging-models/new", data={"code": "PKL2-MS", "name": "Master", "height": "40", "width": "60",
                                                   "length": "40", "cont_idx": ["a", "b"],
                                                   "cont_model_a": str(ids["PKL2-P"]), "cont_model_b": str(ids["PKL2-M"])},
                    follow_redirects=False)
    assert r.status_code == 303
    ms = db.query(PackagingModel).filter_by(code="PKL2-MS").one()
    assert [(c.inner_model.code, c.qty) for c in ms.contents] == [("PKL2-P", 8), ("PKL2-M", 4)]
    assert client.post("/packaging-models/new", data={"code": "PKL2-X", "height": "40", "width": "60", "length": "40",
                                                       "cont_idx": "a", "cont_model_a": str(ids["PKL2-G"])}).status_code == 422
    # produto que já tem a PKL2-M: o master usa ela (2ª da lista); produto sem nenhuma: cria a 1ª (PKL2-P)
    a = Product(sku="PKL2-A", name="A", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.5"))
    b = Product(sku="PKL2-B", name="B", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.5"))
    a.packagings.append(ProductPackaging(model_id=ids["PKL2-M"], name="PKL2-M", inner_qty=24, sort=1))
    db.add_all([a, b])
    db.commit()
    r = client.post("/packagings/apply/confirm", data={"mode": "skus", "ids": f"{a.id},{b.id}", "model_id": str(ms.id)},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    ka = next(k for k in db.get(Product, a.id).packagings if k.model_id == ms.id)
    kb = next(k for k in db.get(Product, b.id).packagings if k.model_id == ms.id)
    assert (ka.inner.model.code, ka.inner_qty, ka.units()) == ("PKL2-M", 4, 96)
    assert (kb.inner.model.code, kb.inner_qty) == ("PKL2-P", 8)
    # exemplo de unitização: tem a aba de pedidos
    from openpyxl import load_workbook

    from services.site_examples import BY_SLUG, write_workbook

    wb = load_workbook(write_workbook(BY_SLUG["unitizacao"], tmp_path / "u.xlsx"), read_only=True)
    assert "Pedidos" in wb.sheetnames and wb["Pedidos"].max_row > 100


def test_branch_insights_page(client, db):
    """Raio-X da filial: indicadores e insights do estado atual (bins ociosas, ocupação baixa/acima de 100%, SKUs sem
    medida, fragmentados, acima do max, histórico com curva ABC)."""
    from models import (BinType, Branch, LocationBin, PickHistory, Product, StockItem, StockPolicy,
                        StorageLocation)
    from services.branch_insights import build

    br = Branch(code="PKX", name="Raio-X")
    bt = BinType(code="PKX-B", name="B", height=Decimal(20), width=Decimal(30), length=Decimal(40))
    db.add_all([br, bt])
    db.flush()
    mk = lambda a, n: StorageLocation(branch_id=br.id, aisle=a, bay="01", level="1", position=str(n), compartment="",  # noqa: E731
                                      address=f"{a}-01-1-{n}")
    locs = [mk("A", 1), mk("A", 2), mk("A", 3), mk("B", 1)]
    db.add_all(locs)
    db.flush()
    for l in locs:
        db.add(LocationBin(location_id=l.id, bin_type_id=bt.id, quantity=1))
    p1 = Product(sku="PKX-1", name="Pequeno", height=Decimal(5), width=Decimal(5), length=Decimal(5))  # 125 cm³
    p2 = Product(sku="PKX-2", name="Grande", height=Decimal(20), width=Decimal(30), length=Decimal(40))  # = a bin
    p3 = Product(sku="PKX-3", name="Sem medidas")
    db.add_all([p1, p2, p3])
    db.flush()
    db.add_all([StockItem(product_id=p1.id, location_id=locs[0].id, quantity=Decimal(10)),   # 5%: baixa ocupação
                 StockItem(product_id=p1.id, location_id=locs[3].id, quantity=Decimal(5)),    # fragmentado
                 StockItem(product_id=p2.id, location_id=locs[1].id, quantity=Decimal(2)),    # 200%
                 StockItem(product_id=p3.id, location_id=locs[1].id, quantity=Decimal(1)),
                 StockPolicy(branch_id=br.id, product_id=p1.id, min_level=Decimal(1), max_level=Decimal(10)),
                 PickHistory(branch_id=br.id, filename="h.xlsx", data={"skus": {"PKX-1": [50, 80], "PKX-2": [2, 2]}})])
    db.commit()
    d = build(db, br.id)
    assert d["locations"]["total"] == 4 and d["locations"]["idle"] == 1  # A-01-1-3: bin sem saldo
    assert d["fill"]["low_n"] == 2 and d["fill"]["over_n"] == 1
    assert d["skus"]["no_dims"] == 1 and d["skus"]["frag"] == 1 and d["skus"]["above"] == 1
    assert d["history"]["abc"]["A"] >= 1 and d["history"]["dead"] == 1  # PKX-3 não saiu
    texts = " ".join(i["text"] for i in d["insights"])
    assert "sem altura" in texts and "acima de 100%" in texts and "sem saldo ainda têm" in texts
    page = client.get(f"/insights?branch_id={br.id}")
    assert page.status_code == 200 and "Raio-X da filial" in page.text and "A-01-1-2" in page.text
    assert client.get("/insights").status_code == 200


def test_occupancy_plan(client, db):
    """Plano de ocupação X%: max level = o que cabe nas bins atuais do SKU a X% do volume (nunca abaixo do saldo; com
    "só aumentar", nunca abaixo do max atual; arredondado para embalagens inteiras); reposições por mês com o
    histórico; aplicar grava o max level na política da filial."""
    from models import (BinType, Branch, LocationBin, PackagingModel, PickHistory, Product, ProductPackaging,
                        StockItem, StockPolicy, StorageLocation)
    from services.occupancy_plan import build

    br = Branch(code="PKO", name="Ocupação")
    bt = BinType(code="PKO-B", name="B", height=Decimal(20), width=Decimal(30), length=Decimal(40))  # 24 L
    db.add_all([br, bt])
    db.flush()
    locs = [StorageLocation(branch_id=br.id, aisle="O", bay="01", level="1", position=str(n), compartment="",
                            address=f"O-01-1-{n}") for n in (1, 2)]
    db.add_all(locs)
    db.flush()
    for l in locs:
        db.add(LocationBin(location_id=l.id, bin_type_id=bt.id, quantity=1))
    a = Product(sku="PKO-A", name="A", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.1"))
    b = Product(sku="PKO-B", name="B", height=Decimal(10), width=Decimal(10), length=Decimal(10), weight=Decimal("0.1"))
    m = PackagingModel(code="PKO-CX", name="CX", height=Decimal(10), width=Decimal(10), length=Decimal(50))
    db.add_all([a, b, m])
    db.flush()
    kb = ProductPackaging(model=m, name="PKO-CX", inner_qty=5, sort=1)
    kb.branches.append(br)
    b.packagings.append(kb)
    db.add_all([StockItem(product_id=a.id, location_id=locs[0].id, quantity=Decimal(6)),
                StockItem(product_id=b.id, location_id=locs[1].id, quantity=Decimal(4)),
                StockPolicy(branch_id=br.id, product_id=a.id, min_level=Decimal(2), max_level=Decimal(10)),
                StockPolicy(branch_id=br.id, product_id=b.id, max_level=Decimal(30)),
                PickHistory(branch_id=br.id, filename="h", data={"skus": {"PKO-A": [30, 60]}, "dates": ["2026-01-01", "2026-01-30"]})])
    db.commit()
    plan = build(db, br.id, [l.id for l in locs], 80, only_increase=True, round_packs=True)
    rows = {r.sku: r for r in plan["rows"]}
    ra, rb = rows["PKO-A"], rows["PKO-B"]
    assert ra.capacity == 19 and ra.new_max == 19  # 24 cabem pela geometria; 80% do volume = 19
    assert rb.new_max == 30  # 15 com embalagens de 5 (19 → 15), mas "só aumentar" mantém o max 30
    plan2 = build(db, br.id, [l.id for l in locs], 80, only_increase=False, round_packs=True)
    assert {r.sku: r.new_max for r in plan2["rows"]}["PKO-B"] == 15 and plan2["summary"]["lowered"] == 1
    assert ra.daily == 2.0 and ra.per_month(10) == 7.5 and ra.per_month(19) == round(60 / 17, 2)
    S = plan["summary"]
    assert S["history"] and S["repl_new"] < S["repl_now"] and S["fill_new"] > S["fill_max_now"]
    db.rollback()
    q = f"branch_id={br.id}&target=80&only_increase=1&round_packs=1&days=30&unitization=branch&run=1"
    page = client.get(f"/optimization/occupancy?{q}")
    assert page.status_code == 200 and "PKO-A" in page.text and "Aplicar max levels" in page.text
    assert client.get(f"/optimization/occupancy/plan.xlsx?{q}").status_code == 200
    r = client.post("/optimization/occupancy/apply", data={"p_branch_id": str(br.id), "p_target": "80",
                                                          "p_only_increase": "1", "p_round_packs": "1",
                                                          "p_days": "30", "p_unitization": "branch",
                                                          "ids": str(a.id)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert float(db.query(StockPolicy).filter_by(branch_id=br.id, product_id=a.id).one().max_level) == 19
    assert float(db.query(StockPolicy).filter_by(branch_id=br.id, product_id=b.id).one().max_level) == 30


def test_sheet_packaging_inner_from_model_not_in_sku_list(db):
    """Aba Embalagens dos SKUs com uma embalagem cujo modelo contém outro que não está na lista do SKU (sem
    quantidade): a de dentro é calculada pelas medidas (como o Calcular) — antes quebrava a rodada do projeto."""
    from types import SimpleNamespace

    from models import PackagingModel, PackagingModelContent
    from services.packaging import _K, item_packs

    inner = PackagingModel(code="ZZ-IN", name="in", height=Decimal(10), width=Decimal(10), length=Decimal(10), notes="")
    outer = PackagingModel(code="ZZ-OUT", name="out", height=Decimal(30), width=Decimal(30), length=Decimal(30), notes="")
    db.add_all([inner, outer])
    db.flush()
    db.add(PackagingModelContent(model_id=outer.id, inner_model_id=inner.id, qty=None, sort=0))
    db.commit()
    db.refresh(outer)
    pk, _alts, _sug = item_packs({"h": 5, "w": 5, "l": 5, "wt": 0.1}, None, {"ZZ-IN": inner, "ZZ-OUT": outer}, {},
                                 "full", sheet=[{"model": "ZZ-OUT", "qty": 4, "enabled": True, "sort": 0}])
    assert pk is not None and pk.qty == 32  # 4 caixas de dentro × 8 unidades (2 × 2 × 2 de 5 cm em 10 cm)
    # hierarquia com um nível sem quantidade: sem total (None), nunca erro
    k = _K(name="X", inner=_K(name="Y", inner=None, inner_qty=None, model=SimpleNamespace()), inner_qty=4,
           model=SimpleNamespace())
    assert k.units() is None
    db.delete(outer)
    db.delete(inner)
    db.commit()
