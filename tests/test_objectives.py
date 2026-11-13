"""Objetivos de maximizar: maior ocupação das bins, maior capacidade do imóvel, menor tempo de separação e a folga de
crescimento do projeto de filial nova."""
import copy
import re
from urllib.parse import urlencode

import pytest

from models import SiteProject
from services.bin_optimizer import OBJECTIVES, canonical_objective
from services.pool_optimizer import POOL_OBJECTIVES, SLOT_OBJECTIVES
from services.site_planner import SITE_CRITERIA
from services.site_service import merged, run_project, suggest_growth


@pytest.fixture(scope="module")
def project(client):
    """Projeto de filial nova criado do exemplo "galpão médio" (importação com o mapeamento automático)."""
    from services.file_reader import token_path
    from services.site_import import auto_mapping, read_sheets

    r = client.post("/optimization/site/new", data={"sample": "galpao-medio", "name": "Objetivos"})
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    action = re.search(r'action="(/optimization/site/(\d+)/upload/confirm)"', r.text)
    fields = {"token": token, "orders_abs": "1"}
    for kind, v in auto_mapping(read_sheets(token_path(token))).items():
        fields[f"sheet_{kind}"] = v["sheet"]
        for fld, i in v["cols"].items():
            fields[f"col_{kind}_{fld}"] = "" if i is None else str(i)
    r = client.post(action.group(1), content=urlencode(fields), headers={"Content-Type": "application/x-www-form-urlencoded"},
                    follow_redirects=False)
    assert "level=danger" not in r.headers["location"], r.headers["location"]
    return int(action.group(2))


def _cfg(db, pid, **params):
    cfg = merged(copy.deepcopy(db.get(SiteProject, pid).config))
    cfg["params"].update({"time_limit": "10", "priority": ["", "", ""], **params})
    return cfg


def _levels(plan):
    return sum(s.struct.levels * s.qty for s in plan.structs)


def test_max_fill_is_the_same_calculation_as_min_volume():
    assert canonical_objective("max_fill") == "min_volume"
    assert canonical_objective("min_bins") == "min_bins"
    assert "max_fill" in OBJECTIVES and "max_fill" in POOL_OBJECTIVES
    assert {"time", "capacity"} <= set(SITE_CRITERIA) and "min_time" in SLOT_OBJECTIVES


def test_project_capacity_fills_the_building(db, project):
    base, errs = run_project(db, _cfg(db, project, objective="modules"))
    cap, errs2 = run_project(db, _cfg(db, project, objective="capacity"))
    assert not errs and not errs2 and base.shortage == 0 and cap.shortage == 0  # a demanda continua atendida
    assert _levels(cap) > 2 * _levels(base)  # enche o imóvel em vez de usar só o necessário
    stage = next(s for s in cap.solver["stages"] if s["criterion"] == "capacity")
    assert stage["display"] == _levels(cap) > 0 and stage["unit"] == "níveis"  # mostrado positivo (o solver minimiza o negativo)


def test_project_time_objective_reports_seconds_per_pick(db, project):
    plan, errs = run_project(db, _cfg(db, project, objective="time", t_v="1.2", t_gold="8"))
    assert not errs and plan.shortage == 0
    stages = {(s["criterion"], s["unit"]) for s in plan.solver["stages"]}
    assert ("time", "m·módulo") in stages and ("time", "s por saída") in stages
    placed = next(s for s in plan.solver["stages"] if s.get("placement"))
    # tempo por saída = caminhada ÷ velocidade + parcela fora da zona de ouro × s por item
    k = plan.slotting
    expected = k["avg_dist"] / 1.2 + (1 - (k["golden_picks"] or 100) / 100) * 8
    assert placed["value"] == pytest.approx(expected, rel=1e-6)


def test_growth_suggestion(client, db, project):
    cfg = _cfg(db, project, objective="modules")
    g = suggest_growth(db, cfg)
    assert not g.get("error") and g["shortage"] == 0 and g["max_factor"] >= g["factor"]
    cfg["demand"]["factor"] = f"{g['max_factor']:.4f}"
    cfg["params"]["time_limit"] = "6"
    plan, errs = run_project(db, cfg)
    assert not errs and plan.shortage == 0  # no limite sugerido ainda cabe


def _wait_job(client, project, page):
    """A busca roda em segundo plano: a tela de espera traz o token; consulta o status até terminar e abre o resultado."""
    import time

    token = re.search(rf"/optimization/site/{project}/job/(\w+)", page).group(1)
    base = f"/optimization/site/{project}/job/{token}"
    for _ in range(300):
        st = client.get(base + "/status").json()
        if st["done"]:
            break
        time.sleep(0.5)
    assert st["done"] and not st["error"], st
    return st, client.get(base + "/result")


def test_growth_page_runs_in_background_with_progress(client, project):
    r = client.post(f"/optimization/site/{project}/growth-suggestion")
    assert r.status_code == 200 and "pode levar de 1 a 2 minutos" in r.text and "/status" in r.text
    st, res = _wait_job(client, project, r.text)
    assert st["total"] >= 3 and st["step"] == st["total"]  # o progresso chegou ao fim
    assert res.status_code == 200 and "Quanto posso crescer" in res.text and "+" in res.text


def test_reduction_page_runs_in_background(client, project):
    r = client.post(f"/optimization/site/{project}/demand-suggestion")
    assert r.status_code == 200 and "/status" in r.text
    _st, res = _wait_job(client, project, r.text)
    assert res.status_code == 200 and "Sugestão de redução da demanda" in res.text


def test_cancel_stops_the_search(client, project):
    import time

    r = client.post(f"/optimization/site/{project}/demand-suggestion")
    token = re.search(rf"/optimization/site/{project}/job/(\w+)", r.text).group(1)
    base = f"/optimization/site/{project}/job/{token}"
    assert client.post(base + "/cancel").json()["ok"]
    for _ in range(120):
        if client.get(base + "/status").json()["done"]:
            break
        time.sleep(0.5)
    assert client.get(base + "/status").json()["done"]
    assert client.get("/optimization/site/999999/job/x/status").status_code == 404  # token de outro projeto / expirado


def test_probe_rounds_skip_the_second_weight_attempt(db, project, monkeypatch):
    """Com falta de espaço e "respeitar pesos", a otimização completa refaz o plano pelo peso máximo (2 solves); a rodada
    de teste das buscas de fator faz só o primeiro."""
    import services.site_planner as P
    from services.site_service import _probe_cfg

    calls = []
    orig = P._solve
    monkeypatch.setattr(P, "_solve", lambda *a, **k: (calls.append(1), orig(*a, **k))[1])
    cfg = _cfg(db, project, objective="modules", time_limit="3", weight="1")
    cfg["demand"]["factor"] = "8000"
    plan, _ = run_project(db, cfg)
    assert plan.shortage > 0 and len(calls) == 2
    calls.clear()
    plan, _ = run_project(db, _probe_cfg(cfg, 3.0))
    assert plan.shortage > 0 and len(calls) == 1
    cfg["params"]["tl_unlimited"] = "1"  # a busca nunca roda "sem limite de tempo"
    assert _probe_cfg(cfg, 4.0)["params"]["tl_unlimited"] == ""


@pytest.fixture(scope="module")
def branch_id(client, project):
    client.get(f"/optimization/site/{project}?run=1")
    r = client.post(f"/optimization/site/{project}/create-branch", follow_redirects=False,
                    data={"branch_code": "OBJ", "branch_name": "Objetivos", "wh_code": "CDO", "wh_name": "CD objetivos",
                          "link_skus": "1", "create_products": "1", "launch_stock": "1"})
    return int(re.search(r"branch_id=(\d+)", r.headers["location"]).group(1))


@pytest.mark.parametrize("path", ["/optimization/pool", "/optimization"])
def test_max_fill_gives_the_same_plan_as_min_volume(client, branch_id, path):
    base = {"branch_id": str(branch_id), "run": "1", "bins_mode": "existing", "basis": "stock"}
    a = client.get(path, params={**base, "objective": "min_volume"})
    b = client.get(path, params={**base, "objective": "max_fill"})
    assert a.status_code == b.status_code == 200 and "alert-danger" not in b.text
    pick = lambda t: re.findall(r"Ocupação[^<]*</div>\s*<div[^>]*>([^<]*)", t)  # noqa: E731
    assert pick(a.text) == pick(b.text)


def test_pool_min_time_shows_estimated_time(client, branch_id):
    r = client.get("/optimization/pool", params={"branch_id": str(branch_id), "run": "1", "bins_mode": "existing",
                                                 "basis": "stock", "objective": "min_time", "slot_v": "1.2",
                                                 "slot_gold_s": "8"})
    assert r.status_code == 200 and "alert-danger" not in r.text
    assert "Tempo estimado por saída" in r.text and "1,2 m/s" in r.text


def test_pool_tiebreak_options_include_walk_pick_time():
    """Pool: o desempate oferece os critérios de posição pela saída; o modo Por endereço não (os produtos não mudam)."""
    from routers.optimization import parse_engine
    from services.bin_optimizer import CRITERIA
    from services.pool_optimizer import POOL_CRITERIA, POOL_OBJECTIVES, OBJ_TB

    assert {"walk", "pick", "time"} <= set(POOL_CRITERIA) and not {"walk", "pick", "time"} & set(CRITERIA)
    assert {OBJ_TB[o] for o in POOL_OBJECTIVES} <= set(POOL_CRITERIA)  # todo objetivo tem o critério equivalente
    _e, _t, prio = parse_engine({"prio2": "walk", "prio3": "volume"}, [], POOL_CRITERIA)
    assert prio == ["walk", "volume"]
    _e, _t, prio = parse_engine({"prio2": "walk"}, [])
    assert prio is None
