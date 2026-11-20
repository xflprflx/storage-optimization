"""Autenticação: login obrigatório, senha com hash, sessão, senha provisória, bloqueio por tentativas e usuários."""
from fastapi.testclient import TestClient

from services import auth


def _anon():
    from main import app

    return TestClient(app)


def test_pages_require_login(client):
    c = _anon()
    r = c.get("/optimization?branch_id=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Foptimization%3Fbranch_id%3D1"
    assert c.get("/onboarding/tours.json").status_code == 401
    assert c.get("/api/products/search?q=x").status_code == 401
    assert c.post("/settings", data={}, follow_redirects=False).headers["location"].startswith("/login")
    assert c.get("/login").status_code == 200 and c.get("/static/onboarding.js").status_code == 200


def test_password_hash_and_admin_bootstrap(db):
    from models import User

    h = auth.hash_password("abc12345", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "abc12345" not in h
    assert auth.verify_password("abc12345", h) and not auth.verify_password("abc1234", h)
    admin = db.query(User).filter_by(username="admin").one()
    assert admin.is_admin and admin.active and not admin.must_change_password  # senha veio de SLOTTA_ADMIN_PASSWORD
    assert auth.INSTALL_ADMIN_HASH.startswith("pbkdf2_sha256$600000$")


def test_login_logout_and_next(client):
    from tests.conftest import ADMIN_PASSWORD

    c = _anon()
    bad = c.post("/login", data={"username": "admin", "password": "errada"}, follow_redirects=False)
    assert bad.status_code == 401 and "incorretos" in bad.text
    ok = c.post("/login", data={"username": "ADMIN", "password": ADMIN_PASSWORD, "next": "/settings"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/settings"
    cookie = ok.headers["set-cookie"]
    assert auth.COOKIE in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert c.get("/settings").status_code == 200
    evil = c.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD, "next": "https://evil.example"},
                  follow_redirects=False)
    assert evil.headers["location"] == "/"  # nunca redireciona para fora
    c.post("/logout")
    assert c.get("/settings", follow_redirects=False).status_code == 303


def test_lockout_after_failures(client):
    c = _anon()
    for _ in range(auth.MAX_FAILS):
        c.post("/login", data={"username": "ninguem", "password": "x"})
    assert auth.locked_for("ninguem") > 0
    r = c.post("/login", data={"username": "ninguem", "password": "x"})
    assert "tentativas" in r.text
    auth._fails.pop("ninguem", None)


def test_users_admin_flow(client, db):
    from models import User

    r = client.post("/users/new", data={"username": "ana", "name": "Ana", "password": "provisoria1", "must_change": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/users/new", data={"username": "Ana", "password": "outra1234"}).status_code == 400  # repetido
    assert client.post("/users/new", data={"username": "bia", "password": "curta"}).status_code == 400  # senha curta
    c = _anon()
    r = c.post("/login", data={"username": "ana", "password": "provisoria1"}, follow_redirects=False)
    assert r.headers["location"] == "/account?first=1"
    assert c.get("/", follow_redirects=False).headers["location"] == "/account?first=1"  # senha provisória: só a conta
    assert c.get("/users", follow_redirects=False).headers["location"] == "/account?first=1"
    r = c.post("/account", data={"current_password": "provisoria1", "new_password": "novaSenha9",
                                 "confirm_password": "novaSenha9"}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/").status_code == 200
    assert c.get("/users").status_code == 403  # não é administrador
    ana = db.query(User).filter_by(username="ana").one()
    db.refresh(ana)
    assert not ana.must_change_password and auth.verify_password("novaSenha9", ana.password_hash)
    # tours são por usuário: o que a Ana viu não aparece para o admin
    c.post("/onboarding/seen", json={"id": "stock", "finished": True})
    db.refresh(ana)
    admin = db.query(User).filter_by(username="admin").one()
    db.refresh(admin)
    assert (ana.tour_seen or {}).get("stock") == "done" and "stock" not in (admin.tour_seen or {})
    # o administrador zera o onboarding da Ana e a desativa (as sessões dela caem)
    client.post(f"/users/{ana.id}/reset-onboarding")
    client.post(f"/users/{ana.id}/edit", data={"name": "Ana", "active": ""})
    db.refresh(ana)
    assert ana.tour_seen == {} and not ana.active
    assert c.get("/", follow_redirects=False).status_code == 303
    # sempre sobra um administrador ativo
    r = client.post(f"/users/{admin.id}/edit", data={"name": "Administrador", "is_admin": "", "active": "1"})
    assert r.status_code == 400
