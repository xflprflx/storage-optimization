"""Planta da filial pela IA: prompt com os corredores da filial e leitura da resposta (com os consertos comuns)."""
import json


def _bays(db, code):
    from services.branch_layout import scope_bays
    from tests.test_shared_sides import _depot

    bid, _pc = _depot(db, code, ("01", "02", "03", "04", "05", "06"), width=600 + len(code))
    return bid, scope_bays(db, bid, None)


def test_prompt_lists_branch_aisles(db):
    from services.plan_ai import prompt

    _bid, bays = _bays(db, "PA1")
    p = prompt(bays)
    assert '"rows"' in p and "- S: 7 estantes" in p and "ímpares (4): 01, 03, 05, 99" in p and "pares (3): 02, 04, 06" in p


def test_parse_clean_answer(db):
    from services.plan_ai import parse

    _bid, bays = _bays(db, "PA2")
    ans = {"building": {"length": 30, "width": 20},
           "rows": [{"aisle": "S", "bays": "odd", "x": 2, "y": 3, "direction": "horizontal", "length": 18,
                     "depth": 0.6, "corridor_side": "below"},
                    {"aisle": "s", "bays": "even", "x": 2, "y": 6.6, "direction": "horizontal", "depth": 0.6,
                     "corridor_side": "above", "numbering_from": "end"}],
           "obstacles": [{"name": "Pilar", "x": 25, "y": 10, "length": 0.4, "width": 0.4}],
           "reference": {"x": 0.5, "y": 10, "label": "Doca"}}
    data, err, notes = parse(json.dumps(ans), bays)
    assert not err
    assert data["L"] == 3000 and data["W"] == 2000 and data["ref"] == {"x": 50.0, "y": 1000.0, "label": "Doca"}
    r0, r1 = data["rows"]
    assert (r0["aisle"], r0["side"], r0["x"], r0["y"], r0["orient"], r0["face"], r0["rev"]) == ("S", "odd", 200, 300, "h", "+", False)
    assert (r1["side"], r1["face"], r1["rev"]) == ("even", "-", True)
    assert data["obstacles"] == [{"x": 2500, "y": 1000, "w": 40, "d": 40, "name": "Pilar"}]


def test_parse_repairs_common_mistakes(db):
    from services.plan_ai import parse

    _bid, bays = _bays(db, "PA3")
    text = """Aqui está a planta:
```json
{ // em centímetros
  "imovel": {"comprimento": "3000", "largura": 2000},
  "fileiras": [
    {"corredor": "S", "estantes": "ímpares", "position": [200, 300], "orientacao": "vertical", "lado_do_corredor": "direita"},
    {"corredor": "ZZ", "estantes": "pares", "x": 1, "y": 1},
    {"corredor": "S", "estantes": ["02", "04", "08"], "x": 900, "y": 300, "direction": "vertical", "corridor_side": "below"},
  ],
}
```"""
    data, err, notes = parse(text, bays)
    assert not err, err
    txt = " ".join(notes)
    assert "centímetros" in txt and "ZZ" in txt and "08" in txt and "referência" in txt and "não combina" in txt
    r0, rz, r1 = data["rows"]
    assert (r0["x"], r0["y"], r0["orient"], r0["face"]) == (200, 300, "v", "+")
    assert rz["aisle"] == "?ZZ" and "bays" not in rz  # corredor que a filial não tem: fileira sem corredor
    assert r1["bays"] == ["02", "04"]


def test_parse_errors(db):
    from services.plan_ai import parse

    _bid, bays = _bays(db, "PA4")
    assert parse("", bays)[1]
    assert "JSON" in parse("{rows: [", bays)[1]
    data, err, _n = parse('{"rows": [{"aisle": "XX", "x": 1, "y": 1}]}', bays)  # corredor desconhecido: entra sem
    assert not err and data["rows"][0]["aisle"] == "?XX"
    assert parse('{"rows": [{"aisle": "XX"}]}', bays)[1]  # sem posição nem imóvel: não há planta


def test_routes(client, db):
    bid, _bays_ = _bays(db, "PA5")
    r = client.get(f"/layout/plan/ai/prompt?branch_id={bid}&warehouse_id=none").json()
    assert "S:" in r["prompt"]
    code = '{"rows": [{"aisle": "S", "bays": "all", "x": 1, "y": 1, "direction": "horizontal", "corridor_side": "below"}]}'
    r = client.post("/layout/plan/ai", json={"branch_id": bid, "warehouse_id": "none", "code": code}).json()
    assert not r.get("error") and r["placed"] == 7 and r["data"]["L"] > 700 and any("imóvel" in n for n in r["notes"])


def test_plan_without_rows_is_accepted(client, db):
    """Planta só do imóvel (sem estantes desenhadas): vale, com aviso; as fileiras são desenhadas depois."""
    from services.plan_ai import parse

    bid, bays = _bays(db, "PA6")
    code = ('{"building": {"length": 40, "width": 25}, "rows": [], '
            '"obstacles": [{"name": "Escritório", "x": 0, "y": 0, "length": 6, "width": 4}], '
            '"reference": {"x": 1, "y": 12, "label": "Doca"}}')
    data, err, notes = parse(code, bays)
    assert not err and data["rows"] == [] and (data["L"], data["W"]) == (4000, 2500) and len(data["obstacles"]) == 1
    assert any("Sem fileiras" in n for n in notes)
    assert not parse('{"building": {"length": 40, "width": 25}}', bays)[1]  # sem a chave rows
    r = client.post("/layout/plan/ai", json={"branch_id": bid, "warehouse_id": "none", "code": code}).json()
    assert not r.get("error") and r["placed"] == 0 and r["data"]["rows"] == []
    r = client.post("/layout/plan/save", json={"branch_id": bid, "warehouse_id": "none", "data": r["data"]}).json()
    assert not r.get("error")
    for url in (f"/layout/plan?branch_id={bid}&warehouse_id=none", f"/layout/plan/3d?branch_id={bid}&warehouse_id=none",
                f"/optimization/slotting/routes?branch_id={bid}&warehouse_id=none", f"/layout?branch_id={bid}",
                f"/layout/plan/data?branch_id={bid}&warehouse_id=none"):
        assert client.get(url).status_code == 200, url  # telas que usam a planta continuam abrindo


def test_unknown_aisles_become_rows_without_aisle_with_suggestions(client, db):
    """Planta sem os códigos dos corredores (o Gemini inventou A, B, J, K): as fileiras entram sem corredor, desenhadas,
    com a sugestão de corredor pela ordem da leitura; os pilares em cima das fileiras e o corredor estreito são avisados."""
    from pathlib import Path

    from services.plan_ai import parse

    bid, bays = _bays(db, "PA7")  # a filial só tem o corredor S
    code = (Path(__file__).parent / "data" / "gemini_plan.json").read_text(encoding="utf-8")
    data, err, notes = parse(code, bays)
    assert not err
    un = data.pop("_unmatched")
    assert len(data["rows"]) == 6 and all(r["aisle"].startswith("?") for r in data["rows"])
    assert [u["label"] for u in un] == ["A", "A", "B", "B", "J", "K"]
    first = data["rows"][0]
    assert first["bay_w"] == 850 and first["depth"] == 80  # desenhada no tamanho lido
    assert {u["suggest_aisle"] for u in un if u["label"] == "A"} == {"S"}  # o único corredor livre vai para o 1º grupo
    assert all(u["suggest_aisle"] == "" for u in un if u["label"] != "A")
    assert any("não existe nos endereços" in n for n in notes)
    r = client.post("/layout/plan/ai", json={"branch_id": bid, "warehouse_id": "none", "code": code}).json()
    assert len(r["unmatched"]) == 6 and r["aisles"] == ["S"]
    txt = " ".join(r["notes"])
    assert "passa por cima de: Pilar" in txt and "Corredor entre" in txt  # pilar na fileira e corredor de 0,9 m


def test_question_mark_rows_pair_up_as_odd_and_even(db):
    import json

    from services.plan_ai import parse, prompt

    bid, bays = _bays(db, "PA8")
    assert '"aisle": "?"' in prompt(bays)
    rows = [{"aisle": "?", "x": 2, "y": 1, "direction": "vertical", "length": 6, "depth": 0.6, "corridor_side": "right"},
            {"aisle": "?", "x": 4, "y": 1, "direction": "vertical", "length": 6, "depth": 0.6, "corridor_side": "left"}]
    data, err, _n = parse(json.dumps({"building": {"length": 20, "width": 10}, "rows": rows}), bays)
    un = data.pop("_unmatched")
    assert not err and [(u["suggest_aisle"], u["suggest_side"]) for u in un] == [("S", "odd"), ("S", "even")]


def test_conflicts():
    from services.branch_layout import build, clean, conflicts

    data = clean({"L": 2000, "W": 1000, "rows": [
        {"aisle": "A", "x": 100, "y": 100, "orient": "h", "face": "+", "depth": 60, "bay_w": 500},
        {"aisle": "B", "x": 100, "y": 220, "orient": "h", "face": "-", "depth": 60, "bay_w": 500},
        {"aisle": "C", "x": 300, "y": 120, "orient": "h", "face": "+", "depth": 60, "bay_w": 500},
        {"aisle": "D", "x": 1800, "y": 600, "orient": "h", "face": "+", "depth": 60, "bay_w": 500}],
        "obstacles": [{"name": "Pilar", "x": 150, "y": 110, "w": 40, "d": 40}]})
    txt = " ".join(conflicts(build(data, {})))
    assert "passa por cima de: Pilar" in txt and "se sobrepõem" in txt and "Corredor entre" in txt
    assert "D passa do imóvel" in txt
