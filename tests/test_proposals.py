"""Propostas de rearranjo e recomposição: aviso de proposta desatualizada e proposta combinada (planta + estruturas)."""


def _with_plan(db, code: str):
    from models import BranchLayout, PickHistory
    from services.branch_layout import auto_layout, clean, scope_bays
    from tests.test_shared_sides import _depot

    bid, pc = _depot(db, code, ("01", "03", "05", "07", "09", "11"), width=771 + len(code))
    data = clean(auto_layout(scope_bays(db, bid, None)))
    data["L"] += 1000
    db.add(BranchLayout(branch_id=bid, warehouse_id=None, data=data))
    db.add(PickHistory(branch_id=bid, filename="h.xlsx", data={
        "kind": "orders", "orders": 2, "lines": 2, "skus": {"X": [2, 2]}, "pairs": [],
        "sample": [["P1", ["X"], [1]], ["P2", ["X"], [1]]]}))
    db.commit()
    return bid


def test_stale_reason_follows_layout_and_recomposition(db):
    from services.branch_layout import get_layout
    from services.proposals import stale_reason, stamp

    bid = _with_plan(db, "PST")
    st = stamp(db, bid, None)
    assert stale_reason(db, bid, None, st, "recompose") == "" and stale_reason(db, bid, None, None, "combined") == ""
    lay = get_layout(db, bid, None)
    lay.data = {**lay.data, "L": lay.data["L"] + 1}
    db.commit()
    assert "planta" in stale_reason(db, bid, None, st, "recompose")
    assert stale_reason(db, bid, None, st, "rearrange") == ""  # o rearranjo só depende das estruturas


def test_combined_proposal_applies_plan_and_recomposition(db):
    from models import BayStructure, RecompositionLog, StructureType
    from services.branch_layout import get_layout
    from services.proposals import apply_combined, propose_combined, stale_reason

    bid = _with_plan(db, "PCB")
    from models import StorageLocation

    lid = db.query(StorageLocation).filter_by(branch_id=bid, address="S-11-1-1").one().id
    assign = {"X": [(lid, 1)]}  # sem saldo: só a proposta do otimizador
    res = propose_combined(db, bid, None, assign=assign, pieces="unlimited")
    assert not res.get("error") and res["options"]
    cur = next(o for o in res["options"] if o["variant"].get("current"))
    assert cur["feasible"] and len(cur["recomp"]["changes"]) == 6  # 6 estantes SA -> SB (+2 níveis cada)
    assert cur["gain"] > 0 and any(p["code"] == "PCB-COLB" for p in cur["bought"])
    idx = next((i for i, o in enumerate(res["options"]) if not o["variant"].get("current") and o["feasible"]),
               res["options"].index(cur))
    msg, level = apply_combined(db, res, idx)
    assert level == "success", msg
    assert db.query(RecompositionLog).filter_by(branch_id=bid).count() == 1
    sb = db.query(StructureType).filter_by(code="PCB-SB").one().id
    assert all(s.structure_type_id == sb for s in db.query(BayStructure).filter_by(branch_id=bid))
    if not res["options"][idx]["variant"].get("current"):
        assert "_prev" in get_layout(db, bid, None).data
    assert stale_reason(db, bid, None, res["stamp"], "combined")  # aplicada: as duas partes mudaram
    assert apply_combined(db, res, 99)[1] == "warning"
