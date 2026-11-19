"""Capacidade dos níveis calculada pela estrutura da estante (recalculada quando a estrutura, o vão ou os tipos de bin
mudam) × limite manual (vale o menor; sem estrutura, o limite é a capacidade)."""
from decimal import Decimal

import pytest


def _usage(db, b, bay, lv="1"):
    from services.level_service import usage_for

    db.expire_all()
    k = (b, None, "C", bay, lv)
    return usage_for(db, [k])[k]


@pytest.fixture(scope="module")
def cap(client):
    """Filial CP: estruturas CP-E1 (estante 01) e CP-E2 (estante 02), frente 100, prof. 60, 4 níveis de vão 45;
    bins CP-A (20 × 30 × 40: 3 por nível) e CP-T (50 de altura: não cabe no vão de 45)."""
    from database import SessionLocal
    from models import BinType, Branch, StorageLocation, StructureKind, StructureType
    from services.structure_service import apply_structure

    with SessionLocal() as db:
        br = Branch(code="CP", name="Capacidade")
        db.add(br)
        kind = db.query(StructureKind).order_by(StructureKind.id).first()
        sts = [StructureType(code=c, name=c, kind=kind.name, kind_id=kind.id, width=Decimal(100), depth=Decimal(60),
                             height=Decimal(200), levels=4, level_height=Decimal(45)) for c in ("CP-E1", "CP-E2")]
        a = BinType(code="CP-A", name="A", height=Decimal(20), width=Decimal(30), length=Decimal(40))
        t = BinType(code="CP-T", name="T", height=Decimal(50), width=Decimal(30), length=Decimal(40))
        db.add_all(sts + [a, t])
        db.flush()
        for bay in ("01", "02"):
            for lv in ("1", "2"):
                db.add(StorageLocation(branch_id=br.id, aisle="C", bay=bay, level=lv, position="1", compartment="",
                                       address=f"C-{bay}-{lv}-1"))
        db.flush()
        apply_structure(db, [(br.id, None, "C", "01")], sts[0])
        apply_structure(db, [(br.id, None, "C", "02")], sts[1])
        db.commit()
        ids = {"b": br.id, "e1": sts[0].id, "e2": sts[1].id, "kind": kind.id, "a": a.id, "t": t.id}
    yield ids
    # o banco dos testes é compartilhado: tira a filial, as estruturas e os tipos de bin (outros testes listam todos)
    from models import BayStructure, LevelCapacity, ShelfLevel

    with SessionLocal() as db:
        lv = [i for (i,) in db.query(ShelfLevel.id).filter_by(branch_id=ids["b"])]
        db.query(LevelCapacity).filter(LevelCapacity.shelf_level_id.in_(lv)).delete(synchronize_session=False)
        db.query(ShelfLevel).filter_by(branch_id=ids["b"]).delete()
        db.query(BayStructure).filter_by(branch_id=ids["b"]).delete()
        db.query(StorageLocation).filter_by(branch_id=ids["b"]).delete()
        db.query(StructureType).filter(StructureType.id.in_([ids["e1"], ids["e2"]])).delete(synchronize_session=False)
        db.query(LevelCapacity).filter(LevelCapacity.bin_type_id.in_(
            [i for (i,) in db.query(BinType.id).filter(BinType.code.in_(["CP-A", "CP-T", "CP-B"]))])).delete(
            synchronize_session=False)
        db.query(BinType).filter(BinType.code.in_(["CP-A", "CP-T", "CP-B"])).delete(synchronize_session=False)
        db.query(Branch).filter_by(id=ids["b"]).delete()
        db.commit()


def test_calculated_capacity_follows_structure_bin_types_and_vao(client, db, cap):
    b = cap["b"]
    u = _usage(db, b, "01")
    assert u.structured and u.capacity[cap["a"]] == 3 and u.calc[cap["a"]] == 3 and not u.manual
    assert cap["t"] not in u.capacity  # 50 + 5 de folga não cabe no vão de 45
    # estrutura mais larga: as estantes com ela são recalculadas (130 / 30 = 4)
    r = client.post(f"/structures/{cap['e1']}/edit", data={
        "code": "CP-E1", "name": "CP-E1", "kind_id": str(cap["kind"]), "width": "130", "depth": "60", "height": "200",
        "levels": "4", "level_height": "45"}, follow_redirects=False)
    assert r.status_code == 303 and "recalculada" in r.headers["location"]
    assert _usage(db, b, "01").capacity[cap["a"]] == 4
    assert _usage(db, b, "02").capacity[cap["a"]] == 3  # outra estrutura: não muda
    # tipo de bin novo: ganha a capacidade calculada (130 / 20 = 6 na frente × 60 / 30 = 2 na profundidade)
    client.post("/bin-types/new", data={"code": "CP-B", "name": "B", "height": "20", "width": "20", "length": "30"})
    from models import BinType

    bid = db.query(BinType).filter_by(code="CP-B").one().id
    assert _usage(db, b, "01").capacity[bid] == 12
    # vão maior: o tipo alto passa a caber (50 + 5 de folga <= 60)
    client.post("/levels/bulk", data={"f_branch_id": str(b), "f_warehouse_id": "none", "b_aisle": "C", "b_bay": "01",
                                      "b_level": "1", "height": "60"})
    u = _usage(db, b, "01")
    assert u.calc.get(cap["t"]) == 4 and _usage(db, b, "01", "2").calc.get(cap["t"]) is None


def test_manual_limit_is_the_smaller_and_removal_returns_to_calculated(client, db, cap):
    b = cap["b"]
    form = {"f_branch_id": str(b), "f_warehouse_id": "none", "b_aisle": "C", "b_bay": "02", "b_level": "1",
            "bin_type_id": str(cap["a"])}
    client.post("/levels/bulk", data={**form, "max_bins": "2"})
    u = _usage(db, b, "02")
    assert u.capacity[cap["a"]] == 2 and u.manual[cap["a"]] == 2 and u.calc[cap["a"]] == 3 and u.limited == [cap["a"]]
    page = client.get(f"/levels?branch_id={b}&warehouse_id=none&aisle=C&status=manual").text
    assert "C-02-1" in page and "C-01-1" not in page
    # o limite segue valendo quando a estrutura muda
    client.post(f"/structures/{cap['e2']}/edit", data={
        "code": "CP-E2", "name": "CP-E2", "kind_id": str(cap["kind"]), "width": "160", "depth": "60", "height": "200",
        "levels": "4", "level_height": "45"})
    u = _usage(db, b, "02")
    assert u.calc[cap["a"]] == 5 and u.capacity[cap["a"]] == 2
    # acima do calculado não limita: vale a calculada (e a tela avisa)
    r = client.post("/levels/bulk", data={**form, "max_bins": "9"}, follow_redirects=False)
    assert "comporta" in r.headers["location"]
    u = _usage(db, b, "02")
    assert u.capacity[cap["a"]] == 5 and not u.manual
    # na tabela: digitar menor cria o limite; apagar volta ao calculado
    lid = u.shelf_level_id
    client.post("/levels/save", data={f"cap_{lid}_{cap['a']}": "1"})
    assert _usage(db, b, "02").capacity[cap["a"]] == 1
    client.post("/levels/save", data={f"cap_{lid}_{cap['a']}": ""})
    assert _usage(db, b, "02").capacity[cap["a"]] == 5
    # aplicar a estrutura de novo mantém o limite, a não ser com "remover os limites manuais"
    client.post("/levels/bulk", data={**form, "max_bins": "2"})
    st = {"f_branch_id": str(b), "f_warehouse_id": "none", "s_aisle": "C", "s_bay": "02",
          "structure_type_id": str(cap["e2"]), "set_heights": "1"}
    client.post("/levels/structure", data=st)
    assert _usage(db, b, "02").capacity[cap["a"]] == 2
    client.post("/levels/structure", data={**st, "clear_manual": "1"})
    assert _usage(db, b, "02").capacity[cap["a"]] == 5
    # sem a estrutura, o que valia fica como limite manual
    client.post("/levels/structure", data={**st, "structure_type_id": "remove"})
    u = _usage(db, b, "02")
    assert not u.structured and u.capacity[cap["a"]] == 5 and u.manual[cap["a"]] == 5


def test_width_estimate_only_without_structure_and_within_the_vao(db, cap):
    from services.bin_optimizer import BinSpec
    from services.optimization_service import derive_caps

    types = {1: BinSpec(1, "P", 20, 20, 30), 2: BinSpec(2, "G", 60, 40, 30)}
    assert derive_caps({1: 8}, types) == ({1: 8, 2: 4}, {2})
    assert derive_caps({1: 8}, types, height=45) == ({1: 8}, set())  # G (60 cm) não cabe no vão de 45
    # estante com estrutura: o tipo que não cabe fica fora (não é estimado pela largura)
    u = _usage(db, cap["b"], "01", "2")
    assert u.structured and cap["t"] not in u.capacity


def test_migration_drops_limits_equal_or_above_calculated(db, cap):
    from models import LevelCapacity
    from services.structure_service import recalc_capacities

    u = _usage(db, cap["b"], "01", "2")
    rows = {c.bin_type_id: c for c in db.query(LevelCapacity).filter_by(shelf_level_id=u.shelf_level_id)}
    calc = rows[cap["a"]].calc_bins
    rows[cap["a"]].manual_bins = calc  # cópia antiga do calculado
    db.commit()
    recalc_capacities(db, [(cap["b"], None, "C", "01")], drop_redundant=True)
    db.commit()
    assert not _usage(db, cap["b"], "01", "2").manual
