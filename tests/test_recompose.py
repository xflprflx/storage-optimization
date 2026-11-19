"""Tipos de estrutura com composição de peças, peças de estrutura e a recomposição das estruturas (reespaçar níveis,
trocar estrutura) com peças limitadas às existentes ou ilimitadas, aplicar e desfazer."""
import re
import time
from decimal import Decimal

import pytest


def test_pieces_and_kinds_crud_with_composition(client, db):
    from models import StructureKind, StructurePiece, StructureType
    from services.site_planner import is_floor
    from services.structure_kinds import composition, kind_of, pieces_for

    assert client.post("/structure-pieces/new", data={"code": "kt-long", "name": "Longarina KT", "cost": "120,50"},
                       follow_redirects=False).status_code == 303
    client.post("/structure-pieces/new", data={"code": "KT-MONT", "name": "Montante KT"})
    long_, mont = (db.query(StructurePiece).filter_by(code=c).one() for c in ("KT-LONG", "KT-MONT"))
    assert long_.cost == Decimal("120.5")
    dup = client.post("/structure-pieces/new", data={"code": "KT-LONG", "name": "x"})
    assert dup.status_code == 422
    r = client.post("/structure-kinds/new", data={"name": "Porta-palete KT", "pitch_cm": "5", "beam_cm": "10",
                                                 "cp_piece": [str(mont.id), str(long_.id), ""], "cp_qty": ["2", "2", ""],
                                                 "cp_per": ["module", "level", "module"]}, follow_redirects=False)
    assert r.status_code == 303
    k = db.query(StructureKind).filter_by(name="Porta-palete KT").one()
    assert composition(k) == [(mont.id, 2.0, "module"), (long_.id, 2.0, "level")]
    assert pieces_for(composition(k), levels=4, modules=3) == {mont.id: 6.0, long_.id: 24.0}
    bad = client.post("/structure-kinds/new", data={"name": "KT ruim", "cp_piece": [str(long_.id)], "cp_qty": ["0"],
                                                   "cp_per": ["level"]})
    assert bad.status_code == 422 and "maior que zero" in bad.text
    # estrutura escolhe o tipo cadastrado (o nome acompanha)
    r = client.post("/structures/new", data={"code": "KT-PP", "name": "PP KT", "kind_id": str(k.id), "width": "270",
                                             "depth": "110", "height": "500", "levels": "4", "level_height": "110"},
                    follow_redirects=False)
    assert r.status_code == 303
    st = db.query(StructureType).filter_by(code="KT-PP").one()
    assert st.kind_id == k.id and st.kind == "Porta-palete KT" and kind_of(db, st).id == k.id
    assert client.post("/structures/new", data={"code": "KT-X", "name": "x", "width": "1", "depth": "1", "height": "1",
                                                "levels": "1", "level_height": "1"}).status_code == 422  # sem tipo
    client.post(f"/structure-kinds/{k.id}/edit", data={"name": "Porta-palete KT2", "cp_piece": [str(long_.id)],
                                                       "cp_qty": ["2"], "cp_per": ["level"]})
    db.expire_all()
    assert db.get(StructureType, st.id).kind == "Porta-palete KT2"  # renomear o tipo renomeia nas estruturas
    # em uso: não sai (nem a peça da composição)
    assert "level=danger" in client.post(f"/structure-kinds/{k.id}/delete", follow_redirects=False).headers["location"]
    assert "level=danger" in client.post(f"/structure-pieces/{long_.id}/delete", follow_redirects=False).headers["location"]
    # tipo "paletes no piso": desenhado e calculado como palete no chão, pelo nome cadastrado
    client.post("/structure-kinds/new", data={"name": "Blocado KT", "floor": "1"})
    assert is_floor("Blocado KT") and not is_floor("Porta-palete KT2")
    for s_ in db.query(StructureType).filter_by(code="KT-PP"):
        db.delete(s_)
    db.commit()


@pytest.fixture(scope="module")
def depot(client):
    """Filial RC: estrutura RC-EST (frente 100, prof. 60, altura 200, 4 níveis de vão 45; tipo com espessura 2 cm e
    furação de 5 cm, prateleira a R$ 10) em 3 estantes; bin RC-BX (20 × 30 × 40 cm, 3 por nível) com 1 bin nos
    níveis 1 e 2 de cada estante."""
    from database import SessionLocal
    from models import (BinType, Branch, LocationBin, StorageLocation, StructureKind, StructureKindPiece,
                        StructurePiece, StructureType)
    from services.structure_service import apply_structure

    with SessionLocal() as db:
        br = Branch(code="RC", name="Recomposição")
        db.add(br)
        col = StructurePiece(code="RC-COL", name="Coluna", notes="")
        prat = StructurePiece(code="RC-PRAT", name="Prateleira", cost=Decimal("10"), notes="")
        db.add_all([col, prat])
        db.flush()
        k = StructureKind(name="Estante RC", pitch_cm=Decimal("5"), beam_cm=Decimal("2"), notes="")
        k.pieces = [StructureKindPiece(piece_id=col.id, qty=Decimal(4), per="module"),
                    StructureKindPiece(piece_id=prat.id, qty=Decimal(1), per="level")]
        db.add(k)
        db.flush()
        st = StructureType(code="RC-EST", name="Estante RC", kind=k.name, kind_id=k.id, width=Decimal(100),
                           depth=Decimal(60), height=Decimal(200), levels=4, level_height=Decimal(45))
        bt = BinType(code="RC-BX", name="Caixa RC", height=Decimal(20), width=Decimal(30), length=Decimal(40))
        db.add_all([st, bt])
        db.flush()
        for bay in ("01", "02", "03"):
            for lv in ("1", "2"):
                loc = StorageLocation(branch_id=br.id, aisle="R", bay=bay, level=lv, position="1", compartment="",
                                      address=f"R-{bay}-{lv}-1")
                db.add(loc)
                db.flush()
                db.add(LocationBin(location_id=loc.id, bin_type_id=bt.id, quantity=1))
        db.flush()
        apply_structure(db, [(br.id, None, "R", b) for b in ("01", "02", "03")], st)
        db.commit()
        return {"b": br.id, "st": st.id, "prat": prat.id, "bt": bt.id}


def test_recompose_respaces_levels_within_pitch_and_beam(client, db, depot):
    from services.recompose import propose

    # só as peças existentes: cada nível novo precisa de uma prateleira, e nenhuma estante libera prateleira
    res = propose(db, depot["b"], None, pieces="existing")
    assert res["bays"] == 3 and res["changes"] == [] and res["status"] == "optimal"
    res = propose(db, depot["b"], None, pieces="unlimited")
    assert len(res["changes"]) == 3 and res["gain"] == 18 and res["levels_added"] == 6
    c = res["changes"][0]
    # níveis 1 e 2 (com bins de 20 cm): vão 20 + 5 de folga = 25 -> com 2 cm de prateleira, base a cada 30 cm (furo
    # de 5 em 5) -> vão 28; com os 140 cm que sobram, mais 4 níveis de 28 (+ a sobra no nível de cima)
    assert c["kind"] == "respace" and c["levels_from"] == 4 and c["levels_to"] == 6
    assert c["vaos_to"] == [28.0, 28.0, 28.0, 28.0, 28.0, 48.0] and c["gain"] == 6 and c["ref"] == "RC-BX"
    assert c["pieces"] == {depot["prat"]: 2.0}
    prat = next(p for p in res["pieces"] if p["id"] == depot["prat"])
    assert prat["bought"] == 6 and prat["cost"] == 60 and res["cost"] == 60
    # ganho mínimo acima do que a estante ganha: nada a fazer
    assert propose(db, depot["b"], None, pieces="unlimited", min_gain=7)["changes"] == []


def test_recompose_swaps_structure_when_pieces_allow(client, db, depot):
    """Com só as peças existentes, trocar a estante por uma estrutura feita das peças que ela já tem (colunas, sem
    prateleira por nível) é possível e não compra nada; uma estrutura com uma peça que não existe no depósito, não."""
    from models import StructureKind, StructureKindPiece, StructurePiece, StructureType
    from services.recompose import propose

    bar = StructurePiece(code="RC-BAR", name="Barra", notes="")
    db.add(bar)
    db.flush()
    col = db.query(StructurePiece).filter_by(code="RC-COL").one()
    k = StructureKind(name="Gaveteiro RC", beam_cm=Decimal("2"), notes="")
    k.pieces = [StructureKindPiece(piece_id=col.id, qty=Decimal(2), per="module")]
    db.add(k)
    db.flush()
    alt = StructureType(code="RC-GAV", name="Gaveteiro RC", kind=k.name, kind_id=k.id, width=Decimal(100),
                        depth=Decimal(60), height=Decimal(200), levels=2, level_height=Decimal(30))
    db.add(alt)
    db.commit()
    try:
        res = propose(db, depot["b"], None, pieces="existing")
        assert any(c["kind"] == "swap" and c["to_code"] == "RC-GAV" for c in res["changes"]) and res["gain"] > 0
        assert not any(p["bought"] for p in res["pieces"])  # nada comprado
        assert all(p["need"] <= p["have"] + 1e-9 for p in res["pieces"])
        k.pieces = [StructureKindPiece(piece_id=bar.id, qty=Decimal(1), per="level")]  # peça que o depósito não tem
        db.commit()
        res = propose(db, depot["b"], None, pieces="existing")
        assert not any(c["kind"] == "swap" for c in res["changes"])
    finally:
        db.delete(db.get(StructureType, alt.id))
        db.delete(db.get(StructureKind, k.id))
        db.delete(db.get(StructurePiece, bar.id))
        db.commit()


def _wait(client, base):
    for _i in range(200):
        st = client.get(base + "/status").json()
        if st.get("done"):
            return st
        time.sleep(0.2)
    raise AssertionError("a busca não terminou")


def test_recompose_flow_apply_and_undo(client, db, depot):
    from models import ShelfLevel
    from services.level_service import usage_for

    b = depot["b"]
    page = client.get(f"/levels?branch_id={b}&warehouse_id=none").text
    assert "/levels/recompose" in page and "Desfazer a última recomposição" not in page
    r = client.post("/levels/recompose", data={"branch_id": b, "warehouse_id": "none", "pieces": "unlimited", "min_gain": "1"})
    base = re.search(r"const base = '([^']+)'", r.text).group(1)
    assert not _wait(client, base)["error"]
    res = client.get(base + "/result")
    assert res.status_code == 200 and "R-01" in res.text and "RC-PRAT" in res.text
    token = base.rsplit("/", 1)[1]
    r = client.post("/levels/recompose/apply", data={"token": token, "only": "1", "pick": ["0"]}, follow_redirects=False)
    assert "level=" not in r.headers["location"] or "level=danger" not in r.headers["location"]
    db.expire_all()

    def levels(bay):
        return sorted(((sl.level, float(sl.height_cm)) for sl in db.query(ShelfLevel).filter_by(branch_id=b, aisle="R", bay=bay)),
                      key=lambda x: int(x[0]))

    changed = [bay for bay in ("01", "02", "03") if len(levels(bay)) == 6]
    assert len(changed) == 1
    assert [h for _l, h in levels(changed[0])] == [28.0, 28.0, 28.0, 28.0, 28.0, 48.0]
    u = usage_for(db, [(b, None, "R", changed[0], "6")])[(b, None, "R", changed[0], "6")]
    assert u.capacity.get(depot["bt"]) == 3  # nível novo com capacidade
    assert "Desfazer a última recomposição" in client.get(f"/levels?branch_id={b}&warehouse_id=none").text
    r = client.post("/levels/recompose/undo", data={"branch_id": b, "warehouse_id": "none"}, follow_redirects=False)
    assert "level=secondary" in r.headers["location"]
    db.expire_all()
    assert levels(changed[0]) == [("1", 45.0), ("2", 45.0), ("3", 45.0), ("4", 45.0)]
    assert "level=warning" in client.post("/levels/recompose/undo", data={"branch_id": b, "warehouse_id": "none"},
                                          follow_redirects=False).headers["location"]  # nada mais a desfazer


def test_optimization_flag_offers_recomposition(client, seeded):
    b, w = seeded["b"], seeded["w"]
    page = client.get(f"/optimization?branch_id={b}&warehouse_id={w}").text
    assert 'name="recomp"' in page and 'name="recomp_pieces"' in page
    form = dict(re.findall(r'<input[^>]*name="(slot_[a-z_]+)"[^>]*value="([^"]*)"', page))
    from urllib.parse import urlencode

    q = urlencode({**form, "branch_id": b, "warehouse_id": w, "run": 1, "objective": "min_volume", "recomp": 1,
                   "recomp_pieces": "unlimited", "recomp_min": 2})
    html = client.get(f"/optimization?{q}").text
    assert 'id="recompBox"' in html and 'name="pieces" value="unlimited"' in html and 'name="min_gain" value="2"' in html
    assert 'id="recompBox"' not in client.get(f"/optimization?{q.replace('recomp=1', 'recomp=')}").text


def test_recompose_never_swaps_across_storage_conditions(client, db, depot):
    """A estrutura alternativa (feita das peças existentes) é refrigerada: a estante ambiente não troca por ela."""
    from models import StructureKind, StructureKindPiece, StructurePiece, StructureType
    from services.recompose import propose

    col = db.query(StructurePiece).filter_by(code="RC-COL").one()
    k = StructureKind(name="Câmara RC", beam_cm=Decimal("2"), condition="refrigerado", notes="")
    k.pieces = [StructureKindPiece(piece_id=col.id, qty=Decimal(2), per="module")]
    db.add(k)
    db.flush()
    alt = StructureType(code="RC-CAM", name="Câmara RC", kind=k.name, kind_id=k.id, width=Decimal(100),
                        depth=Decimal(60), height=Decimal(200), levels=2, level_height=Decimal(30))
    db.add(alt)
    db.commit()
    try:
        res = propose(db, depot["b"], None, pieces="existing")
        assert not any(c["to_code"] == "RC-CAM" for c in res["changes"])
        assert res["door_checked"] is False  # filial sem planta: a abertura das portas não é conferida
    finally:
        db.delete(db.get(StructureType, alt.id))
        db.delete(db.get(StructureKind, k.id))
        db.commit()
