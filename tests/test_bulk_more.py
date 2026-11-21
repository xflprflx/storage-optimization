"""Exclusão em massa nas telas que faltavam: usuários, históricos de saídas e níveis (Capacidade dos níveis)."""
from fastapi.testclient import TestClient

from models import Branch, LevelCapacity, LocationBin, PickHistory, ShelfLevel, StorageLocation, User


def test_bulk_delete_users_protects_self_and_last_admin(client, db):
    for name in ("bulk1", "bulk2"):
        client.post("/users/new", data={"username": name, "password": "senhaBoa123"})
    ids = [u.id for u in db.query(User).filter(User.username.in_(["bulk1", "bulk2", "admin"]))]
    me = db.query(User).filter_by(username="admin").one().id
    page = client.get("/users").text
    assert "bulk-users" in page and f'class="form-check-input bulk-users" value="{me}"' not in page  # sem caixa para si
    prev = client.post("/bulk-delete/users/preview", data={"ids": [str(i) for i in ids], "back": "/users"})
    assert prev.status_code == 200 and "é você" in prev.text and 'id="hardMsg"' in prev.text
    r = client.post("/bulk-delete/users/run", data={"ids": [str(i) for i in ids], "back": "/users", "cascade": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    left = {u.username for u in db.query(User)}
    assert "admin" in left and not {"bulk1", "bulk2"} & left  # o próprio usuário nunca sai, nem com "relacionados"


def test_bulk_delete_users_requires_admin(client):
    client.post("/users/new", data={"username": "comum", "password": "senhaBoa123"})
    c = TestClient(client.app)
    c.post("/login", data={"username": "comum", "password": "senhaBoa123"})
    r = c.post("/bulk-delete/users/preview", data={"ids": ["1"], "back": "/users"}, follow_redirects=False)
    assert r.status_code == 303 and "/users" not in r.headers["location"]


def test_bulk_delete_histories_and_levels(client, db, seeded):
    b = db.get(Branch, seeded["b"])
    h = db.query(PickHistory).filter_by(branch_id=b.id).one()
    keep = {"filename": h.filename, "data": h.data}  # os dados de exemplo são compartilhados: o histórico volta no fim
    assert "bulk-histories" in client.get("/imports/history").text
    client.post("/bulk-delete/histories/run", data={"ids": [str(h.id)], "back": "/imports/history"})
    db.expire_all()
    assert db.query(PickHistory).filter_by(branch_id=b.id).count() == 0
    db.add(PickHistory(branch_id=b.id, **keep))
    db.commit()
    # níveis: um reservado (sem endereços) some; um com endereços perde só as capacidades
    reserved = ShelfLevel(branch_id=b.id, warehouse_id=seeded["w"], aisle="A", bay="01", level="9", notes="")
    db.add(reserved)
    db.flush()
    used = db.query(ShelfLevel).filter_by(branch_id=b.id, aisle="A", bay="01", level="1").one()
    cap = db.query(LevelCapacity).filter_by(shelf_level_id=used.id, bin_type_id=seeded["bt"]).one_or_none()
    if cap is None:  # outro teste pode ter calculado a capacidade deste nível (banco compartilhado)
        db.add(LevelCapacity(shelf_level_id=used.id, bin_type_id=seeded["bt"], max_bins=4))
    else:
        cap.max_bins = 4
    db.commit()
    rid, uid, bid = reserved.id, used.id, b.id
    page = client.get(f"/levels?branch_id={bid}&warehouse_id={seeded['w']}").text
    assert "bulk-levels" in page
    prev = client.post("/bulk-delete/levels/preview", data={"ids": [str(rid), str(uid)], "back": "/levels"})
    assert "(reservado)" in prev.text
    client.post("/bulk-delete/levels/run", data={"ids": [str(rid), str(uid)], "back": "/levels"})
    db.expire_all()
    assert db.query(ShelfLevel).filter_by(branch_id=bid, aisle="A", bay="01", level="9").count() == 0  # reservado sai
    # o nível com endereços perde o limite manual; a estante tem estrutura: ele volta só com a capacidade calculada
    back = db.query(ShelfLevel).filter_by(branch_id=bid, aisle="A", bay="01", level="1").one_or_none()
    caps = db.query(LevelCapacity).filter_by(shelf_level_id=back.id).all() if back else []
    assert all(c.manual_bins is None and c.calc_bins == c.max_bins for c in caps)
    assert db.query(StorageLocation).filter_by(branch_id=bid, aisle="A", bay="01", level="1").count() == 1  # endereço fica


def test_level_with_bins_of_other_locations_is_never_deleted(client, db, seeded):
    lvl = ShelfLevel(branch_id=seeded["b"], warehouse_id=seeded["w"], aisle="B", bay="02", level="8", notes="")
    db.add(lvl)
    db.flush()
    lb = db.query(LocationBin).filter_by(location_id=seeded["loc"]).first()
    lb.shelf_level_id = lvl.id  # bins de outro endereço guardadas neste nível
    db.commit()
    lid = lvl.id
    r = client.post("/bulk-delete/levels/run", data={"ids": [str(lid)], "back": "/levels"}, follow_redirects=False)
    assert "level=warning" in r.headers["location"]
    db.expire_all()
    assert db.get(ShelfLevel, lid) is not None
    lb = db.get(LocationBin, lb.id)
    lb.shelf_level_id = None
    db.commit()
