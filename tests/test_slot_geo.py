"""Posição pela saída sem planta: a caminhada soma a frente real da estrutura de cada estante (o campo "Frente" só
vale para estantes sem estrutura) e a contagem de onde vem a zona de ouro da pega."""
import pytest

from models import BayStructure, Branch, StorageLocation, StructureType
from services.slotting import address_geo, gold_sources, slot_spec


@pytest.fixture()
def scope(db):
    br = Branch(code="GEO1", name="Filial geo")
    st = StructureType(code="GEO-ST", name="Estante 2,5 m", width=250, depth=60, height=200, levels=4, level_height=40,
                       golden_from=50, golden_to=150)
    db.add_all([br, st])
    db.flush()
    locs = {}
    for bay in ("01", "02", "03"):  # letras por rua: estantes 01, 02, 03 em sequência ao longo da rua A
        loc = StorageLocation(branch_id=br.id, aisle="A", bay=bay, level="2", address=f"A-{bay}-2")
        db.add(loc)
        locs[bay] = loc
    db.add(BayStructure(branch_id=br.id, aisle="A", bay="02", structure_type_id=st.id))  # só a 02 tem estrutura
    db.flush()
    yield {b: loc.id for b, loc in locs.items()}
    db.rollback()


def test_walk_sums_real_structure_fronts(db, scope):
    spec = slot_spec({"slot": "1", "slot_rows": "1", "slot_bay_m": "1", "slot_aisle_m": "3"})
    geo, _ref = address_geo(db, list(scope.values()), spec)
    # 01 sem estrutura (1 m): meio em 0,5; 02 com estrutura de 2,5 m: 1 + 1,25; 03 sem estrutura: 1 + 2,5 + 0,5
    assert geo[scope["01"]].x_m == pytest.approx(0.5)
    assert geo[scope["02"]].x_m == pytest.approx(2.25)
    assert geo[scope["03"]].x_m == pytest.approx(4.0)


def test_gold_sources_counts_structure_and_field(db, scope):
    assert gold_sources(db, list(scope.values())) == {"structure": 1, "field": 2}
