"""Laterais compartilhadas: módulos vizinhos do mesmo tipo (com "vizinhos compartilham a lateral") usam N + 1 laterais
em vez de 2N — no cadastro, na recomposição das estruturas e na importação."""
from decimal import Decimal

import pytest


def test_pieces_per_side_module_and_level():
    from services.structure_kinds import own_pieces, pieces_for, side_pieces

    comp = [(1, 1.0, "side"), (2, 2.0, "level"), (3, 4.0, "module")]
    assert pieces_for(comp, 3) == {1: 2.0, 2: 6.0, 3: 4.0}  # um módulo sozinho: 2 laterais
    assert pieces_for(comp, 3, modules=3, sides=4) == {1: 4.0, 2: 18.0, 3: 12.0}  # 3 seguidos: 4 laterais
    assert side_pieces(comp) == {1: 1.0} and own_pieces(comp, 3) == {2: 6.0, 3: 4.0}


def test_old_default_kinds_are_migrated(db):
    from models import StructureKind, StructureKindPiece, StructurePiece
    from services.structure_kinds import _migrate_sides

    pcs = {c: db.query(StructurePiece).filter_by(code=c).one() for c in ("MONT-PP", "LONG-PP")}
    k = StructureKind(name="PP antigo", notes="")
    k.pieces = [StructureKindPiece(piece_id=pcs["MONT-PP"].id, qty=Decimal(2), per="module"),
                StructureKindPiece(piece_id=pcs["LONG-PP"].id, qty=Decimal(2), per="level")]
    edited = StructureKind(name="PP mexido", notes="")
    edited.pieces = [StructureKindPiece(piece_id=pcs["MONT-PP"].id, qty=Decimal(3), per="module")]
    db.add_all([k, edited])
    db.flush()
    assert _migrate_sides(db)
    assert k.shared_sides and {(p.piece_id, float(p.qty), p.per) for p in k.pieces} == \
        {(pcs["MONT-PP"].id, 1.0, "side"), (pcs["LONG-PP"].id, 2.0, "level")}
    assert not edited.shared_sides  # composição alterada pelo usuário: fica como está
    db.rollback()
    # tipos padrão da instalação nova já vêm por lateral
    pp = db.query(StructureKind).filter_by(name="Porta-palete").one()
    assert pp.shared_sides and any(p.per == "side" for p in pp.pieces)


def _depot(db, code: str, bays: tuple, width: int = 100):
    """Filial com o tipo KA (lateral compartilhada, 1 coluna por lateral) na estrutura SA (4 níveis de 45) nas estantes
    `bays` e, longe, uma estante SB (tipo KB, também compartilhada, 6 níveis de 45): trocar SA por SB ganha 2 níveis."""
    from models import (BinType, Branch, LocationBin, StorageLocation, StructureKind, StructureKindPiece,
                        StructurePiece, StructureType)
    from services.structure_service import apply_structure

    br = Branch(code=code, name=code)
    db.add(br)
    pc = {}
    for c in ("COLA", "PRATA", "COLB", "PRATB"):
        pc[c] = StructurePiece(code=f"{code}-{c}", name=c, cost=Decimal(10), notes="")
        db.add(pc[c])
    db.flush()
    kinds = {}
    for kn, col, prat in (("KA", "COLA", "PRATA"), ("KB", "COLB", "PRATB")):
        k = StructureKind(name=f"{code}-{kn}", notes="", shared_sides=True)
        k.pieces = [StructureKindPiece(piece_id=pc[col].id, qty=Decimal(1), per="side"),
                    StructureKindPiece(piece_id=pc[prat].id, qty=Decimal(1), per="level")]
        db.add(k)
        kinds[kn] = k
    db.flush()
    sa = StructureType(code=f"{code}-SA", name="SA", kind=kinds["KA"].name, kind_id=kinds["KA"].id, width=Decimal(width),
                       depth=Decimal(60), height=Decimal(200), levels=4, level_height=Decimal(45))
    sb = StructureType(code=f"{code}-SB", name="SB", kind=kinds["KB"].name, kind_id=kinds["KB"].id, width=Decimal(width),
                       depth=Decimal(60), height=Decimal(300), levels=6, level_height=Decimal(45))
    bt = BinType(code=f"{code}-BX", name="bx", height=Decimal(40), width=Decimal(30), length=Decimal(40))
    db.add_all([sa, sb, bt])
    db.flush()
    for bay in (*bays, "99"):
        loc = StorageLocation(branch_id=br.id, aisle="S", bay=bay, level="1", position="1", compartment="",
                              address=f"S-{bay}-1-1")
        db.add(loc)
        db.flush()
        db.add(LocationBin(location_id=loc.id, bin_type_id=bt.id, quantity=1))
    db.flush()
    apply_structure(db, [(br.id, None, "S", b) for b in bays], sa, create_levels=True)
    apply_structure(db, [(br.id, None, "S", "99")], sb, create_levels=True)
    db.commit()
    return br.id, pc


@pytest.mark.parametrize("bays, laterals", [(("01", "03", "05"), 4), (("01", "11", "21"), 6)])
def test_recompose_buys_shared_laterals(db, bays, laterals):
    from services.recompose import propose
    from services.structure_service import bay_neighbors

    code = "SS" + bays[1]
    bid, pc = _depot(db, code, bays, width=100 + 10 * int(bays[1]))  # frente própria: troca só pelas estruturas daqui
    pairs, src = bay_neighbors(db, bid, None)
    assert src == ("numbering" if laterals == 4 else "")
    assert len([p for p in pairs if p[0][1] in bays and p[1][1] in bays]) == (2 if laterals == 4 else 0)
    res = propose(db, bid, None, pieces="unlimited")
    assert len(res["changes"]) == 3 and all(c["kind"] == "swap" for c in res["changes"])
    got = {p["code"]: p for p in res["pieces"]}
    # as 3 estantes viram SB (tipo KB): laterais KB compradas = 4 se vizinhas (compartilham), 6 se separadas
    assert got[f"{code}-COLB"]["bought"] == laterals and got[f"{code}-COLA"]["freed"] == laterals
    assert res["shared_pairs"] == (2 if laterals == 4 else 0)
    # só as peças existentes: sem lateral de KB sobrando, nenhuma troca é possível
    assert propose(db, bid, None, pieces="existing")["changes"] == []


def test_kind_form_and_import_with_shared_sides(client, db, tmp_path):
    from models import StructureKind
    from services.master_importer import MASTER_KINDS, MasterOptions
    from tests.test_structure_import import _map

    r = client.post("/structure-kinds/new", data={"name": "SS Form", "shared_sides": "1"}, follow_redirects=False)
    assert r.status_code == 303 and db.query(StructureKind).filter_by(name="SS Form").one().shared_sides
    text = ("Tipo de estrutura;Vizinhos compartilham a lateral;Peca;Qtd;Por\n"
            "SS Imp;sim;SS-COL;2;lateral\nSS Imp;;SS-PRAT;1;nível\n")
    p = tmp_path / "k.csv"
    p.write_text(text, encoding="utf-8")
    k = MASTER_KINDS["structure_kinds"]
    res = k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions())
    assert not res.errors
    kd = db.query(StructureKind).filter_by(name="SS Imp").one()
    assert kd.shared_sides and sorted(p.per for p in kd.pieces) == ["level", "side"]


def test_rearrange_passage_splitting_a_run_needs_one_more_lateral(db):
    from models import BranchLayout
    from services.branch_layout import auto_layout, build, clean, scope_bays
    from services.rearrange import add_passages, lateral_delta

    bid, pc = _depot(db, "RQ", ("01", "03", "05", "07", "09", "11"), width=777)
    bays = scope_bays(db, bid, None)
    data = clean(auto_layout(bays))
    data["L"] += 1000  # sobra no imóvel para as estantes andarem depois da passagem
    db.add(BranchLayout(branch_id=bid, warehouse_id=None, data=data))
    db.commit()
    g = build(data, bays)
    row = next(r for r in data["rows"] if r.get("side") in ("odd", "all"))
    rr = g.row_rects[data["rows"].index(row)]
    orient = row["orient"]
    lo, hi = (rr[0], rr[0] + rr[2]) if orient == "h" else (rr[1], rr[1] + rr[3])
    new, why = add_passages(data, g, orient, [lo + (hi - lo) / 2])
    assert new is not None, why
    d = lateral_delta(db, bid, None, g, build(new, bays))
    assert d is not None and d["sides"] >= 1
    assert {p["code"] for p in d["pieces"]} == {"RQ-COLA"} and d["cost"] == 10 * d["sides"]
