"""Estantes travadas: motivos (cadastro), posição fixa (rearranjo, editor e planta gerada) e não recompor."""
from decimal import Decimal


def _plan(db, code):
    from models import BranchLayout
    from services.branch_layout import auto_layout, clean, scope_bays
    from tests.test_shared_sides import _depot

    bid, _pc = _depot(db, code, ("01", "03", "05", "07", "09", "11"), width=880 + len(code))
    data = clean(auto_layout(scope_bays(db, bid, None)))
    data["L"] += 1000
    db.add(BranchLayout(branch_id=bid, warehouse_id=None, data=data))
    db.commit()
    return bid, data


def test_reasons_crud(client, db):
    from models import BayLock, LockReason

    r = client.post("/lock-reasons/new", data={"name": "Cravada no chão", "notes": "parabolt"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/lock-reasons/new", data={"name": "cravada no chão"}).status_code == 422  # nome repetido
    rs = db.query(LockReason).filter_by(name="Cravada no chão").one()
    assert client.post(f"/lock-reasons/{rs.id}/edit", data={"name": "Cravada no piso", "notes": ""},
                       follow_redirects=False).status_code == 303
    db.refresh(rs)
    assert rs.name == "Cravada no piso" and "Cravada no piso" in client.get("/lock-reasons").text
    bid, _d = _plan(db, "LKR")
    db.add(BayLock(branch_id=bid, aisle="S", bay="01", fixed_position=True, reason_id=rs.id))
    db.commit()
    r = client.post(f"/lock-reasons/{rs.id}/delete", follow_redirects=False)
    assert "msg=" in r.headers["location"] and db.query(LockReason).filter_by(id=rs.id).count() == 1  # em uso: fica
    db.query(BayLock).filter_by(branch_id=bid).delete()
    db.commit()
    client.post(f"/lock-reasons/{rs.id}/delete")
    assert db.query(LockReason).filter_by(id=rs.id).count() == 0


def test_lock_and_unlock_bays_on_the_levels_page(client, db):
    from models import BayLock, LockReason

    bid, _d = _plan(db, "LKP")
    rs = LockReason(name="Câmara com tomadas LKP", notes="")
    db.add(rs)
    db.commit()
    base = {"f_branch_id": str(bid), "f_warehouse_id": "none", "f_level": ""}
    r = client.post("/levels/locks", data={**base, "k_aisle": "S", "k_bay": "01-03", "fixed": "1", "no_recompose": "1",
                                           "reason_id": str(rs.id), "action": "save"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "msg_level=success" in loc and "&level=" not in loc  # o aviso não vira filtro de nível
    got = {(x.bay, x.fixed_position, x.no_recompose, x.reason_id) for x in db.query(BayLock).filter_by(branch_id=bid)}
    assert got == {("01", True, True, rs.id), ("03", True, True, rs.id)}
    html = client.get(f"/levels?branch_id={bid}&warehouse_id=none").text
    assert "bi-lock-fill" in html and "Câmara com tomadas LKP" in html
    # o formulário abre com o estado das estantes filtradas
    one = client.get(f"/levels?branch_id={bid}&warehouse_id=none&aisle=S&bay=01").text
    assert 'id="lk_norec" checked' in one and f'value="{rs.id}" selected' in one
    # salvar grava exatamente o que está marcado: desmarcar "posição fixa" tira a posição fixa
    client.post("/levels/locks", data={**base, "k_aisle": "S", "k_bay": "01", "no_recompose": "1", "action": "save"})
    db.expire_all()
    assert {(x.bay, x.fixed_position, x.no_recompose) for x in db.query(BayLock).filter_by(branch_id=bid)} == \
        {("01", False, True), ("03", True, True)}
    client.post("/levels/locks", data={**base, "k_aisle": "S", "k_bay": "03", "action": "save"})  # nada marcado
    db.expire_all()
    assert {x.bay for x in db.query(BayLock).filter_by(branch_id=bid)} == {"01"}
    client.post("/levels/locks", data={**base, "k_aisle": "S", "k_bay": "", "action": "unlock"})
    assert db.query(BayLock).filter_by(branch_id=bid).count() == 0


def test_no_recompose_bays_stay_as_they_are(db):
    from services.bay_locks import set_locks
    from services.recompose import propose

    bid, _d = _plan(db, "LKC")
    assert {c["bay"] for c in propose(db, bid, None, pieces="unlimited")["changes"]} >= {"01", "03"}
    set_locks(db, [(bid, None, "S", "01")], None, True)
    db.commit()
    res = propose(db, bid, None, pieces="unlimited")
    assert "01" not in {c["bay"] for c in res["changes"]} and res["skipped"]["locked"] == 1


def test_fixed_bays_do_not_move(client, db):
    from services.bay_locks import keep_fixed, set_locks
    from services.branch_layout import build, clean, scope_bays
    from services.rearrange import add_passages

    bid, data = _plan(db, "LKF")
    bays = scope_bays(db, bid, None)
    g = build(data, bays)
    row = next(r for r in data["rows"] if r.get("side") in ("odd", "all"))
    rr = g.row_rects[data["rows"].index(row)]
    lo, hi = (rr[0], rr[0] + rr[2]) if row["orient"] == "h" else (rr[1], rr[1] + rr[3])
    cut = [lo + (hi - lo) / 2]
    assert add_passages(data, g, row["orient"], cut)[0] is not None
    last = max((p for p in g.placed.values() if p.bay.aisle == "S"), key=lambda p: p.x if row["orient"] == "h" else p.y)
    fixed = {("S", last.bay.bay): "cravada"}
    new, why = add_passages(data, g, row["orient"], cut, fixed=fixed)
    assert new is None and f"S-{last.bay.bay}" in why and "cravada" in why
    set_locks(db, [(bid, None, "S", last.bay.bay)], True, None)
    db.commit()
    # salvar a planta com a estante fixa em outro lugar: recusado
    moved = clean({**data, "rows": [{**r, "x": r["x"] + 100} for r in data["rows"]]})
    r = client.post("/layout/plan/save", json={"branch_id": bid, "warehouse_id": "none", "data": moved})
    assert r.status_code == 400 and f"S-{last.bay.bay}" in r.json()["error"]
    assert client.post("/layout/plan/save", json={"branch_id": bid, "warehouse_id": "none", "data": data}).status_code == 200
    # planta gerada (automática): a fileira da estante fixa fica onde estava
    gen = clean({**data, "rows": [{**r, "y": r["y"] + 500} for r in data["rows"]]})
    kept, note = keep_fixed(db, bid, None, gen)
    assert note and build(kept, bays).placed[("S", last.bay.bay)].y == last.y
    # o editor recebe o cadeado
    bj = client.get(f"/layout/plan/data?branch_id={bid}&warehouse_id=none").json()["bays"]["S"]
    assert any(e.get("lock") for e in bj if e["bay"] == last.bay.bay)


def test_import_locks_with_new_reason(db, tmp_path):
    from models import BayLock, LockReason
    from services.master_importer import MasterOptions
    from services.structure_importer import STRUCTURE_KINDS
    from tests.test_structure_import import _map

    bid, _d = _plan(db, "LKI")
    text = "Filial;Corredor;Estante;Estrutura;Posicao fixa;Nao recompor;Motivo da trava\nLKI;S;05;;sim;nao;Sprinkler LKI\n"
    p = tmp_path / "t.csv"
    p.write_text(text, encoding="utf-8")
    k = STRUCTURE_KINDS["bay_structures"]
    res = k["run"](db, p, _map(text.splitlines()[0], k["fields"]), MasterOptions())
    assert not res.errors
    lk = db.query(BayLock).filter_by(branch_id=bid).one()
    assert (lk.bay, lk.fixed_position, lk.no_recompose) == ("05", True, False)
    assert lk.reason_id == db.query(LockReason).filter_by(name="Sprinkler LKI").one().id
