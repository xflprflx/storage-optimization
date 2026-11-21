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
    r0, r1 = data["rows"]
    assert (r0["x"], r0["y"], r0["orient"], r0["face"]) == (200, 300, "v", "+")
    assert r1["bays"] == ["02", "04"]


def test_parse_errors(db):
    from services.plan_ai import parse

    _bid, bays = _bays(db, "PA4")
    assert parse("", bays)[1]
    assert "JSON" in parse("{rows: [", bays)[1]
    assert parse('{"rows": [{"aisle": "XX", "x": 1, "y": 1}]}', bays)[1]


def test_routes(client, db):
    bid, _bays_ = _bays(db, "PA5")
    r = client.get(f"/layout/plan/ai/prompt?branch_id={bid}&warehouse_id=none").json()
    assert "S:" in r["prompt"]
    code = '{"rows": [{"aisle": "S", "bays": "all", "x": 1, "y": 1, "direction": "horizontal", "corridor_side": "below"}]}'
    r = client.post("/layout/plan/ai", json={"branch_id": bid, "warehouse_id": "none", "code": code}).json()
    assert not r.get("error") and r["placed"] == 7 and r["data"]["L"] > 700 and any("imóvel" in n for n in r["notes"])
