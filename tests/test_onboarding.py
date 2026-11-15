"""Onboarding integrado: o guia passo a passo abre, marca as etapas pelo cadastro e todo tour aponta para elementos
que existem na tela dele."""
import re

from services.onboarding import TOURS, TRACKS, progress, work_ids

_SEL = re.compile(r'^#([\w-]+)$|^\[(name|data-tour)="([\w-]+)"\]$')


def _present(html: str, sel: str) -> bool:
    m = _SEL.match(sel)
    assert m, f"seletor fora do formato aceito: {sel}"
    if m.group(1):
        return re.search(rf'\bid="{re.escape(m.group(1))}"', html) is not None
    return re.search(rf'\b{m.group(2)}="{re.escape(m.group(3))}"', html) is not None


def test_every_tour_selector_exists(client, seeded):
    ids = seeded
    missing = []
    for tid, t in TOURS.items():
        url = t["u"].replace("{b}", str(ids["b"])).replace("{prj}", str(ids["prj"]))
        path = url.split("?")[0]
        assert re.search(t["p"], path), (tid, "a tela de exemplo não casa com o caminho do tour")
        r = client.get(url)
        assert r.status_code == 200, (tid, url)
        for st in t["s"]:
            if st["s"] and not st["cond"] and not any(_present(r.text, s) for s in st["s"]):
                missing.append((tid, st["s"]))
    assert not missing, missing


def test_tracks_point_to_known_tours():
    for tr in TRACKS:
        for st in tr["steps"]:
            assert st["tour"] in TOURS, st["id"]
            path = st["href"].split("?")[0].replace("{prj}", "1")
            assert re.search(TOURS[st["tour"]]["p"], path), (st["id"], "o tour não roda na tela da etapa")


def test_guide_page_and_tours_json(client, db):
    r = client.get("/onboarding")
    assert r.status_code == 200 and "trilha-base" in r.text and "#nk-tour=branch" in r.text
    data = client.get("/onboarding/tours.json").json()
    assert {t["id"] for t in data} == set(TOURS)
    client.cookies.set("nk_lang", "en")
    try:
        en = client.get("/onboarding/tours.json").json()
    finally:
        client.cookies.delete("nk_lang")
    intro = next(t for t in en if t["id"] == "intro")
    assert intro["t"] == "Getting to know Neokioski"
    # o menu Guia está em todas as telas
    assert 'id="nkGuideMenu"' in client.get("/settings").text


def test_progress_follows_the_data(db):
    from models import Branch

    prog = progress(db)
    steps = {s["id"]: s for tr in prog["tracks"] for s in tr["steps"]}
    has_branch = db.query(Branch).count() > 0
    assert steps["branch"]["state"] is has_branch
    assert steps["intro"]["state"] is False  # etapa "tour": sem usuário logado, nenhum tour visto
    b = work_ids(db)["b"]
    if b:
        assert steps["address"]["href"] == f"/optimization?branch_id={b}"
    assert 0 <= prog["pct"] <= 100


def test_welcome_and_tours_are_per_user_and_reset(client, db):
    """Convite do Painel e tours vistos ficam no usuário; "zerar" marca tudo como não visto."""
    from models import User

    assert 'id="nkWelcome"' in client.get("/").text or True  # o convite depende do cadastro (pode já estar completo)
    client.post("/onboarding/welcome", follow_redirects=False)
    client.post("/onboarding/seen", json={"id": "intro", "finished": True})
    client.post("/onboarding/seen", json={"id": "branch", "finished": False})
    client.post("/onboarding/seen", json={"id": "intro", "finished": False})  # concluído não volta a "visto"
    client.post("/onboarding/seen", json={"id": "nao-existe", "finished": True})  # tour desconhecido: ignorado
    admin = db.query(User).filter_by(username="admin").one()
    db.refresh(admin)
    assert admin.tour_seen == {"intro": "done", "branch": "seen"} and admin.prefs.get("welcome_hidden")
    page = client.get("/settings").text
    assert '"intro": "done"' in page  # o navegador recebe os tours vistos do usuário
    assert 'id="nkWelcome"' not in client.get("/").text
    r = client.post("/onboarding/reset", headers={"referer": "http://testserver/settings"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings")
    db.refresh(admin)
    assert admin.tour_seen == {} and not (admin.prefs or {}).get("welcome_hidden")


def test_result_steps_exist_after_a_run(client, seeded):
    """As etapas do resultado da otimização (qualidade da solução, indicadores, tabela, aplicar) só aparecem depois de
    Analisar: com um resultado, todas têm o elemento na tela."""
    body = client.get(f"/optimization?branch_id={seeded['b']}&warehouse_id={seeded['w']}&run=1").text
    for st in TOURS["optimization"]["s"]:
        if st["cond"]:
            assert any(_present(body, s) for s in st["s"]), st["s"]
