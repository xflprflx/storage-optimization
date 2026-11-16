"""Proposta de rearranjo: passagens transversais e troca de corredores na planta desenhada (filial existente) e as
variações de disposição do projeto de filial nova."""
import re
import time
from urllib.parse import urlencode

import pytest

from services.branch_layout import Bay, auto_layout, build, clean
from services.rearrange import _corridor_demand, add_passages, swap_corridors
from services.route_sim import dist


def _bays(aisles=("A", "B", "C"), n=10, w=100.0):
    return {a: [Bay(a, f"{i:02d}", w, 60.0, 200.0, 4) for i in range(1, n + 1)] for a in aisles}


def _rows_h(g):
    return [(x, x + w) for x, y, w, d, ri in g.row_rects if g.data["rows"][ri]["orient"] == "h"]


def test_passage_splits_rows_and_keeps_every_bay():
    bays = _bays()
    data = clean(auto_layout(bays))  # fileiras horizontais a partir de x = 400 (5 estantes de 100 por lado)
    data["L"] += 400  # espaço no fundo para as estantes andarem
    g = build(data, bays)
    cut = 400 + 250  # no meio da 3ª estante: ela vai junto para depois da passagem
    new, why = add_passages(data, g, "h", [cut], gap=200)
    assert new is not None, why
    g2 = build(new, bays)
    assert set(g2.placed) == set(g.placed) and not g2.dup  # nenhuma estante some nem se repete
    assert len(new["rows"]) == 2 * len(data["rows"])  # cada fileira virou duas
    for a, b in _rows_h(g2):  # o vão [cut, cut + 200] fica livre em todas as fileiras
        assert b <= cut + 1e-6 or a >= cut + 200 - 1e-6
    assert any(abs(a - (cut + 200)) < 1e-6 for a, b in _rows_h(g2))  # o que vem depois começa no fim da passagem
    # sem espaço no fundo do imóvel: recusada com o motivo
    tight = clean(auto_layout(bays))
    tight["L"] = 400 + 500 + 50
    none, why = add_passages(tight, build(tight, bays), "h", [cut], gap=200)
    assert none is None and "não cabe" in why
    # nenhuma fileira atravessa a posição
    none, why = add_passages(data, g, "h", [100], gap=200)
    assert none is None


def test_swap_moves_the_busiest_corridor_next_to_the_reference():
    bays = _bays()
    data = clean(auto_layout(bays))
    data["ref"] = {"x": 100.0, "y": 100.0, "label": "expedição"}  # referência no canto de cima: perto do corredor A
    g = build(data, bays, with_net=True)
    node_c = g.nodes[("C", "01")]
    sample = [[f"P{i}", ["SKU-C"], [1]] for i in range(5)]
    demand, dist_of = _corridor_demand(g, {"SKU-C": [node_c]}, sample, g.ref_node())
    assert demand == {node_c.corridor: 5.0}
    new, moves = swap_corridors(data, g, demand, dist_of)
    assert new is not None and moves
    g2 = build(new, bays, with_net=True)
    assert set(g2.placed) == set(g.placed)
    before = dist(g.ref_node(), node_c)
    after = dist(g2.ref_node(), g2.nodes[("C", "01")])
    assert after < before  # o corredor C (o único com saídas) ficou mais perto da referência
    _d2, dist2 = _corridor_demand(g2, {}, [], g2.ref_node())
    assert g2.nodes[("C", "01")].corridor == min(dist2, key=dist2.get)  # foi para o corredor mais perto
    # demanda já na ordem: nada a trocar
    same, none = swap_corridors(data, g, {min(dist_of, key=dist_of.get): 9.0}, dist_of)
    assert same is None and none == []


def _wait_job(client, base):
    for _i in range(240):
        st = client.get(base + "/status").json()
        if st.get("done") or st.get("error"):
            return st
        time.sleep(0.25)
    raise AssertionError("a busca não terminou")


def test_branch_rearrange_flow_apply_and_undo(client, db, seeded):
    from models import BranchLayout

    b, w = seeded["b"], seeded["w"]
    lay = db.query(BranchLayout).filter_by(branch_id=b, warehouse_id=w).one()
    original = dict(lay.data)
    page = client.get(f"/layout/plan?branch_id={b}&warehouse_id={w}").text
    assert "/layout/plan/rearrange" in page and "Desfazer rearranjo" not in page
    r = client.post("/layout/plan/rearrange", data={"branch_id": b, "warehouse_id": w})
    assert r.status_code == 200
    base = re.search(r"const base = '([^']+)'", r.text).group(1)
    assert base.startswith("/layout/plan/rearrange/job/")
    st = _wait_job(client, base)
    assert st["done"] and not st["error"], st
    res = client.get(base + "/result")
    assert res.status_code == 200 and "Planta atual" in res.text
    token = base.rsplit("/", 1)[1]
    from services import jobs

    variants = jobs.get(token, jobs.owner_of(token))["res"]["variants"]
    idx = next((i for i, v in enumerate(variants) if not v.get("current") and not v.get("error")), None)
    if idx is not None:  # aplica e desfaz
        r = client.post("/layout/plan/rearrange/apply", data={"token": token, "idx": idx}, follow_redirects=False)
        assert r.status_code == 303 and "level=danger" not in r.headers["location"]
        db.expire_all()
        lay = db.query(BranchLayout).filter_by(branch_id=b, warehouse_id=w).one()
        assert lay.data.get("_prev") and lay.data["rows"] != original["rows"]
        assert "Desfazer rearranjo" in client.get(f"/layout/plan?branch_id={b}&warehouse_id={w}").text
        client.post("/layout/plan/rearrange/undo", data={"branch_id": b, "warehouse_id": w})
        db.expire_all()
        lay = db.query(BranchLayout).filter_by(branch_id=b, warehouse_id=w).one()
        assert lay.data["rows"] == original["rows"] and not lay.data.get("_prev")
    # a planta atual não se aplica; token desconhecido: expirou
    cur = next(i for i, v in enumerate(variants) if v.get("current"))
    r = client.post("/layout/plan/rearrange/apply", data={"token": token, "idx": cur}, follow_redirects=False)
    assert "level=warning" in r.headers["location"]
    assert client.get("/layout/plan/rearrange/job/zzz/status").status_code == 404


def test_optimization_flag_offers_the_rearrange_proposal(client, seeded):
    b, w = seeded["b"], seeded["w"]
    page = client.get(f"/optimization?branch_id={b}&warehouse_id={w}").text
    assert 'name="slot_rearr"' in page
    form = dict(re.findall(r'<input[^>]*name="(slot_[a-z_]+)"[^>]*value="([^"]*)"', page))  # o formulário como a tela envia
    q = urlencode({**form, "branch_id": b, "warehouse_id": w, "run": 1, "objective": "min_volume", "slot": 1, "slot_rearr": 1})
    r = client.get(f"/optimization?{q}")
    assert r.status_code == 200 and 'id="rearrBox"' in r.text
    assert 'name="cmp"' in r.text  # com a planta e o histórico: o botão leva a distribuição da proposta
    off = client.get(f"/optimization?{q.replace('slot_rearr=1', 'slot_rearr=')}")
    assert 'id="rearrBox"' not in off.text


@pytest.fixture(scope="module")
def project(client):
    """Projeto de filial nova do exemplo "galpão médio" (com a aba Pedidos)."""
    from services.file_reader import token_path
    from services.site_import import auto_mapping, read_sheets

    r = client.post("/optimization/site/new", data={"sample": "galpao-medio", "name": "Rearranjo"})
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    action = re.search(r'action="(/optimization/site/(\d+)/upload/confirm)"', r.text)
    fields = {"token": token, "orders_abs": "1"}
    for kind, v in auto_mapping(read_sheets(token_path(token))).items():
        fields[f"sheet_{kind}"] = v["sheet"]
        for fld, i in v["cols"].items():
            fields[f"col_{kind}_{fld}"] = "" if i is None else str(i)
    client.post(action.group(1), content=urlencode(fields), headers={"Content-Type": "application/x-www-form-urlencoded"})
    return int(action.group(2))


def test_site_layout_proposal_compares_orientations_and_cross_aisles(client, db, project):
    import copy

    from models import SiteProject
    from services.rearrange import propose_site, site_variants
    from services.site_service import merged

    cfg = merged(copy.deepcopy(db.get(SiteProject, project).config))
    labels = [v["changes"] for v in site_variants(cfg)]
    assert labels[0] == {} and {"orientation": "width", "cross_aisles": 2} in labels
    res = propose_site(db, cfg, per_run_s=2.0)
    vs = res["variants"]
    assert res["key"] == "opt_avg" and res["orders"] > 0
    cur = next(v for v in vs if v.get("current"))
    best = [v for v in vs if v.get("best")]
    assert len(best) == 1 and best[0]["opt_avg"] <= cur["opt_avg"]
    assert all(v.get("slots") for v in vs if not v.get("error"))  # capacidade do piso em todas
    assert len(vs) >= 6
    # a flag mostra o botão no resultado; aplicar grava a variação no projeto
    client.post(f"/optimization/site/{project}", data={})  # (sem efeito: só garante que a tela abre)
    pr = db.get(SiteProject, project)
    pr.config = {**pr.config, "building": {**pr.config.get("building", {}), "propose": "1"}}
    db.commit()
    assert "layoutForm" in client.get(f"/optimization/site/{project}").text
    ch = next(v["changes"] for v in vs if not v.get("current") and not v.get("error"))
    import json

    r = client.post(f"/optimization/site/{project}/layout-apply", data={"changes": json.dumps(ch)}, follow_redirects=False)
    assert r.status_code == 303 and "level=danger" not in r.headers["location"]
    db.expire_all()
    b = db.get(SiteProject, project).config["building"]
    assert all(str(b[k if k in b else k]) == str(v) for k, v in ch.items() if k in ("orientation", "cross_aisles"))
    bad = client.post(f"/optimization/site/{project}/layout-apply", data={"changes": '{"orientation": "x"}'}, follow_redirects=False)
    assert "level=danger" in bad.headers["location"]


def test_site_layout_job_runs_in_background_and_can_be_cancelled(client, project):
    r = client.post(f"/optimization/site/{project}/layout-suggestion")
    assert r.status_code == 200 and "Proposta de disposição" in r.text
    base = re.search(r"const base = '([^']+)'", r.text).group(1)
    client.post(base + "/cancel")
    st = _wait_job(client, base)
    assert st["done"]
    res = client.get(base + "/result", follow_redirects=False)
    assert res.status_code in (200, 303)
