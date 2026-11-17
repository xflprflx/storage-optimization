"""Aparência 3D de bins e estruturas: biblioteca, código colado (gerado por IA), cadastro, prévia e o 3D."""
import json

import pytest

from services.appearance import (CUSTOM, LIBRARY_BINS, LIBRARY_STRUCTS, MAX_PARTS, SIMPLE, look_of, normalize, parse,
                                 prompt, suggest)

CODE = {"parts": [{"shape": "box", "x": 0, "y": 0, "z": 0, "w": 1, "h": 0.1, "d": 1, "color": "#8B5A2B"},
                  {"shape": "cylinder", "axis": "x", "x": 0.1, "y": 0.1, "z": 0.1, "w": 0.8, "h": 0.2, "d": 0.2,
                   "color": "#2f6db5", "tint": True, "repeat": {"count": 3, "dz": 0.25}},
                  {"shape": "box", "x": 0, "y": -0.04, "z": 0, "w": 1, "h": 0.04, "d": 0.05, "color": "#e8772e",
                   "each_level": True, "opacity": 0.5}]}


def test_parse_accepts_the_chat_answer_and_normalizes():
    obj, err, _notes = parse("Aqui está:\n```json\n" + json.dumps(CODE) + "\n```\nQualquer dúvida…")
    assert err == "" and obj["parts"][0]["color"] == "#8b5a2b" and len(obj["parts"]) == 3
    look, err = normalize(obj, structure=True)
    parts = look["parts"]
    assert len(parts) == 1 + 3 + 1  # o repeat vira 3 peças
    assert parts[0] == {"s": "b", "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0, "h": 0.1, "d": 1.0, "c": "#8b5a2b"}
    assert [p["z"] for p in parts[1:4]] == [0.1, 0.35, 0.6] and all(p["a"] == "x" and p["t"] == 1 for p in parts[1:4])
    assert parts[4]["l"] == 1 and parts[4]["o"] == 0.5 and look["tint"]
    # em bin não existe "por nível": a peça vale uma vez
    assert not any(p.get("l") for p in normalize(obj, structure=False)[0]["parts"])
    deep = {"parts": [{**CODE["parts"][2], "y": -0.2}]}
    assert normalize(deep, structure=True)[0] and normalize(deep, structure=False)[0] is None


@pytest.mark.parametrize("text, part", [
    ("não é json", "JSON válido"),
    ('{"foo": 1}', "parts"),
    ('{"parts": [{"x": 2}]}', "x fora do intervalo"),
    ('{"parts": [{"x": "abc"}]}', "x inválido"),
    ('{"parts": [{"w": 0.1, "repeat": {"count": 61}}]}', "repeat.count"),
    (json.dumps({"parts": [{"w": 0.01, "repeat": {"count": 31, "dx": 0.01}}] * 2}), str(MAX_PARTS)),
])
def test_invalid_codes_explain_why(text, part):
    obj, err, _notes = parse(text)
    assert obj is None and part in err


def test_parse_fixes_what_ai_chats_usually_get_wrong():
    """Comentários, vírgula sobrando, True do Python, forma e cor por nome, números em texto, medidas em cm, valores
    um pouco fora de 0–1 e a lista em outra chave: o código entra, com o aviso do que foi ajustado."""
    text = """Claro! Segue:
```json
{
  // base do palete
  "model": {"components": [
    {"shape": "cube", "x": 0, "y": 0, "z": 0, "w": 100, "h": 14, "d": 120, "color": "brown"},  /* madeira */
    {"type": "box", "x": 2, "y": 14, "z": 2, "w": 96, "h": 136, "d": 116, "color": "#36c", "tint": True,},
  ]},
}
```"""
    obj, err, notes = parse(text, (100, 150, 120), structure=False)
    assert err == "" and len(obj["parts"]) == 2
    p0, p1 = obj["parts"]
    assert p0["shape"] == "box" and p0["color"] == "#8b5a2b" and p0["h"] == pytest.approx(14 / 150)
    assert p1["color"] == "#3366cc" and p1["tint"] is True and p1["x"] == pytest.approx(0.02)
    assert any("cm" in n for n in notes) and any("cor" in n for n in notes)
    obj, err, notes = parse('[{"x": -0.1, "w": "1,2", "h": 0.5, "d": 1, "color": "vermelho"}]')
    assert err == "" and obj["parts"][0]["x"] == -0.05 and obj["parts"][0]["w"] == 1.1 and any("ajustado" in n for n in notes)
    # sem as medidas do item, cm não tem como virar fração: recusa explicando
    assert parse('{"parts": [{"w": 50, "h": 20, "d": 30}]}')[0] is None


def test_library_models_fit_any_size_and_suggestions():
    for key, (_lbl, fn) in LIBRARY_BINS.items():
        for dims in ((40, 30, 60), (100, 150, 120), (5, 5, 5)):
            look = look_of("bin", key, None, dims)
            assert look and look["model"] == key and len(look["parts"]) <= MAX_PARTS and look["tint"], key
    for key in LIBRARY_STRUCTS:
        for dims in ((100, 200, 60), (270, 500, 110), (30, 20, 40)):
            look = look_of("struct", key, None, dims)
            assert look and look["model"] == key, key
        assert any(p.get("l") for p in look_of("struct", key, None, (100, 200, 60))["parts"])  # algo em cada nível
    assert suggest("bin", "Palete PBR 1,0 × 1,2") == "pallet" and suggest("bin", "Caixa plástica") == "tote"
    assert suggest("bin", "Tambor 200 L") == "drum" and suggest("bin", "Sem pista") is None
    assert suggest("struct", "Porta-palete", "PP seletivo") == "rack"
    assert suggest("struct", "Estante", "Câmara fria de congelados") == "cold_room"  # o nome manda
    assert suggest("struct", "Palete no chão", "Blocado") is None  # palete no chão: desenho próprio do 3D
    # automático sem sugestão e "desenho simples": nenhuma peça (o 3D usa o desenho de sempre)
    assert look_of("bin", "", None, (40, 30, 60), "XPTO") is None
    assert look_of("bin", SIMPLE, None, (40, 30, 60), "Palete") is None
    assert look_of("bin", CUSTOM, CODE, (40, 30, 60))["parts"][0]["c"] == "#8b5a2b"
    assert "{w}" in prompt("bin") and "{levels}" in prompt("struct") and "{levels}" not in prompt("bin")


def test_structure_form_saves_library_and_pasted_code(client, db):
    from models import StructureType

    base = {"code": "AP-EST", "name": "Câmara fria teste", "kind": "Estante", "width": "120", "depth": "60",
            "height": "200", "levels": "3", "level_height": "60"}
    r = client.post("/structures/new", data={**base, "look_model": "", "look_code": ""}, follow_redirects=False)
    assert r.status_code == 303
    st = db.query(StructureType).filter_by(code="AP-EST").one()
    assert st.look_model == "" and st.appearance is None
    from services.appearance import struct_look

    assert struct_look(st)["model"] == "cold_room"  # automático pelo nome
    # código inválido: o formulário volta com o motivo e nada muda
    bad = client.post(f"/structures/{st.id}/edit", data={**base, "look_model": CUSTOM, "look_code": '{"parts": []}'})
    cm = client.post(f"/structures/{st.id}/edit", data={**base, "look_model": CUSTOM,
                                                         "look_code": '{"parts": [{"w": 120, "h": 200, "d": 60, "color": "#fff"}]}'},
                     follow_redirects=False)
    assert cm.status_code == 303  # em cm: convertido com as medidas do formulário
    db.expire_all()
    assert db.get(StructureType, st.id).appearance["parts"][0]["w"] == 1.0
    assert bad.status_code == 422 and "parts" in bad.text
    ok = client.post(f"/structures/{st.id}/edit", data={**base, "look_model": "", "look_code": json.dumps(CODE)},
                     follow_redirects=False)
    assert ok.status_code == 303
    db.expire_all()
    st = db.get(StructureType, st.id)
    assert st.look_model == CUSTOM and st.appearance["parts"][1]["repeat"] == CODE["parts"][1]["repeat"]  # vale o código
    page = client.get(f"/structures/{st.id}/edit").text
    assert 'value="custom" selected' in page and "#8b5a2b" in page and "Copiar prompt" in page
    client.post(f"/structures/{st.id}/edit", data={**base, "look_model": "rack", "look_code": ""})
    db.expire_all()
    assert db.get(StructureType, st.id).look_model == "rack" and db.get(StructureType, st.id).appearance is None


def test_bin_form_and_preview(client, db):
    from models import BinType

    r = client.post("/bin-types/new", data={"code": "AP-PAL", "name": "Palete com carga", "height": "150", "width": "100",
                                            "length": "120", "look_model": "simple"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.query(BinType).filter_by(code="AP-PAL").one().look_model == "simple"
    pv = client.post("/appearance/preview", json={"kind": "bin", "model": "", "w": 100, "h": 150, "d": 120,
                                                  "name": "Palete PBR"}).json()
    assert pv["auto"] == "pallet" and pv["look"]["model"] == "pallet" and not pv["error"]
    pv = client.post("/appearance/preview", json={"kind": "struct", "model": "custom", "code": "x", "w": 1, "h": 1, "d": 1}).json()
    assert pv["look"] is None and pv["error"]
    pv = client.post("/appearance/preview", json={"kind": "bin", "model": "custom", "w": 100, "h": 150, "d": 120,
                                                  "code": '{"parts": [{"w": 100, "h": 15, "d": 120, "color": "wood"}]}'}).json()
    assert pv["look"] and not pv["error"] and pv["notes"]
    pv = client.post("/appearance/preview", json={"kind": "struct", "model": "simple", "w": 100, "h": 200, "d": 60}).json()
    assert pv["look"] is None and not pv["error"]


def test_3d_payload_carries_the_appearance(client, seeded):
    d = client.get(f"/layout/plan/plan3d.json?branch_id={seeded['b']}&warehouse_id={seeded['w']}").json()
    st = next(s for s in d["structs"] if s["code"] == "H-EST")
    assert st["look"]["model"] == "shelving"  # tipo "Estante": estante de aço
    assert all("look" in t for t in d["types"]) and all("ff" in m for m in d["modules"])
    html = client.get(f"/layout/plan/3d?branch_id={seeded['b']}&warehouse_id={seeded['w']}").text
    assert 'id="nkLook"' in html and "/static/look3d.js" in html
