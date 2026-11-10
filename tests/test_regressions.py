"""Regressões encontradas na revisão: cache da rede de caminhada, exclusão em massa acima de 1.000 itens, ordem física
dos níveis com ordenação alfabética e validação das estruturas na planilha do projeto."""
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from models import (BranchLayout, Branch, Product, ShelfLevel, StorageLocation, StructureType)


def _branch(db, code: str, aisle: str, bays: int, levels=("1",)) -> int:
    br = Branch(code=code, name=code)
    db.add(br)
    db.flush()
    for bay in range(1, bays + 1):
        for lv in levels:
            db.add(StorageLocation(branch_id=br.id, aisle=aisle, bay=f"{bay:02d}", level=lv, position="1", compartment="",
                                   address=f"{aisle}-{bay:02d}-{lv}-1"))
    db.commit()
    return br.id


def test_walk_distance_follows_reversed_numbering(db):
    """Inverter a numeração muda o lugar das estantes sem mudar o desenho das fileiras: a distância não pode vir do
    cache da rede montada antes."""
    from services.branch_layout import auto_layout, clean, geometry, scope_bays
    from services.route_sim import dist

    bid = _branch(db, "T1", "A", 20)
    data = clean(auto_layout(scope_bays(db, bid, None)))
    lay = BranchLayout(branch_id=bid, warehouse_id=None, data=data)
    db.add(lay)
    db.commit()
    g = geometry(db, bid, None)
    before = dist(g.ref_node(), g.node("A", "01"))
    lay.data = {**data, "rows": [{**r, "rev": True} for r in data["rows"]]}
    db.commit()
    g2 = geometry(db, bid, None)
    assert g2.net is g.net  # mesma rede (cache): só as posições das estantes mudaram
    after = dist(g2.ref_node(), g2.node("A", "01"))
    assert g2.node("A", "01").along != g.node("A", "01").along
    assert after > before + 500  # a estante 01 foi para a outra ponta da fileira


def test_bulk_delete_all_filtered_over_1000(client, db):
    db.add_all([Product(sku=f"BULK{i:05d}", name="x") for i in range(1200)])
    db.commit()
    r = client.post("/bulk-delete/products/preview", data={"all": "1", "qs": "q=BULK", "back": "/products"})
    assert r.status_code == 200
    assert 'name="all" value="1"' in r.text and 'name="ids"' not in r.text  # o filtro, não um campo por item
    r = client.post("/bulk-delete/products/run", data={"all": "1", "qs": "q=BULK", "back": "/products"}, follow_redirects=False)
    assert r.status_code == 303 and "1200" in r.headers["location"]
    assert db.scalar(select(func.count()).select_from(Product).where(Product.sku.like("BULK%"))) == 0


def test_bulk_delete_many_selected_ids(client, db):
    db.add_all([Product(sku=f"SEL{i:05d}", name="x") for i in range(1100)])
    db.commit()
    ids = [str(i) for i in db.scalars(select(Product.id).where(Product.sku.like("SEL%")))]
    r = client.post("/bulk-delete/products/run", data={"ids": ids, "back": "/products"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.scalar(select(func.count()).select_from(Product).where(Product.sku.like("SEL%"))) == 0


def test_level_order_is_physical_with_alpha_sort(client, db, settings):
    """Com a ordenação alfabética, o nível 10 continua acima do 2: vão por nível e mapa do estoque."""
    bid = _branch(db, "T3", "B", 1, levels=[str(n) for n in range(1, 13)])
    st = StructureType(code="T3-N12", name="N12", kind="Estante", width=Decimal(100), depth=Decimal(60), height=Decimal(400),
                       levels=12, level_height=Decimal(20), level_heights=[60, 50, 40])
    db.add(st)
    db.commit()
    settings.save(db, {"addr_sort": "alpha"})
    r = client.post("/levels/structure", follow_redirects=False,
                    data={"f_branch_id": str(bid), "f_warehouse_id": "none", "s_aisle": "B", "s_bay": "01",
                          "structure_type_id": str(st.id), "top_gap": "5", "set_heights": "1"})
    assert r.status_code == 303
    hs = {s.level: float(s.height_cm) for s in db.scalars(select(ShelfLevel).where(ShelfLevel.branch_id == bid))}
    assert (hs["1"], hs["2"], hs["3"], hs["10"], hs["11"]) == (60.0, 50.0, 40.0, 20.0, 20.0)
    data = client.get("/layout/data", params={"branch_id": bid, "warehouse_id": "none"}).json()
    bay = data["aisles"][0]["children"][0]
    assert [n["code"] for n in bay["children"]] == [str(n) for n in range(1, 13)]


def test_project_sheet_rejects_levels_above_height(db, tmp_path):
    from openpyxl import Workbook

    from services.site_import import import_file
    from services.site_service import merged

    wb = Workbook()
    ws = wb.active
    ws.title = "Estruturas"
    ws.append(["Código", "Nome", "Tipo", "Frente (cm)", "Profundidade (cm)", "Altura (cm)", "Níveis", "Vão útil (cm)",
               "Altura do 1º nível (cm)", "Vãos por nível (cm)"])
    ws.append(["T4-ALTA", "zz", "Estante", 100, 60, 150, 4, 35, 50, "60; 60"])
    ws.append(["T4-OK", "ok", "Estante", 100, 60, 250, 4, 35, 20, "60; 40"])
    path = Path(tmp_path) / "estruturas.xlsx"
    wb.save(path)
    _cfg, _msgs, errs = import_file(db, path, merged({}))
    db.commit()
    bad = db.scalar(select(StructureType).where(StructureType.code == "T4-ALTA"))
    ok = db.scalar(select(StructureType).where(StructureType.code == "T4-OK"))
    assert any("T4-ALTA" in e and "passam da altura" in e for e in errs)
    assert bad.first_level_cm is None and not bad.level_heights
    assert float(ok.first_level_cm) == 20 and ok.vaos() == [60.0, 40.0, 35.0, 35.0]
    assert not re.search("T4-OK", " ".join(errs))
