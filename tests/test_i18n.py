"""Idiomas e unidades: catálogos completos, conversão de medidas e planilhas em qualquer idioma."""
import json
import re
from pathlib import Path

import pytest

from services import i18n

ROOT = Path(__file__).resolve().parent.parent
OTHER_LANGS = [k for k in i18n.LANGUAGES if k != i18n.DEFAULT_LANG]


@pytest.fixture()
def lang():
    """Troca idioma/unidades no teste e volta ao padrão no fim."""
    tokens = []

    def use(code, units="auto"):
        tokens.append((i18n.set_lang(code), i18n.set_units(units)))

    yield use
    for t1, t2 in reversed(tokens):
        i18n._UNITS.reset(t2)
        i18n._LANG.reset(t1)


def _fields(s):
    return sorted(re.findall(r"\{(\w+)\}", s)), sorted(re.findall(r"</?(\w+)", s))


def test_every_text_is_translated_with_same_placeholders():
    msgs = json.loads((ROOT / "locales" / "messages.json").read_text(encoding="utf-8"))
    for code in OTHER_LANGS:
        cat = json.loads((ROOT / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        missing = [m for m in msgs if m not in cat]
        assert not missing, f"{code}: {len(missing)} sem tradução, ex.: {missing[:5]}"
        bad = [m for m in msgs if _fields(m) != _fields(cat[m])]
        assert not bad, f"{code}: marcadores/tags diferentes em {bad[:5]}"


def test_units_follow_language_and_choice(lang):
    lang("en")
    assert i18n.is_imperial()
    assert i18n.units_in_text("Height (cm): 120 cm, 75–150 cm") == "Height (in): 47.2 in, 29.5–59.1 in"
    assert i18n.units_in_text("Glove 10 mm") == "Glove 10 mm"  # mm (nomes de produto) não muda
    lang("en", "metric")
    assert i18n.units_in_text("120 cm") == "120 cm"
    lang("pt-BR", "imperial")
    assert i18n.units_in_text("1.200 m² · 2,5 m/s") == "12.917 ft² · 8,2 ft/s"
    lang("de")
    assert not i18n.is_imperial()


def test_imperial_page_converts_texts_but_not_scripts(client):
    client.cookies.set("nk_lang", "en")
    try:
        html = client.get("/structures/new").text
    finally:
        client.cookies.clear()
    assert "(in)" in html and "Module dimensions (cm)" not in html
    assert 'data-u="cm"' in html  # campos convertidos no navegador (units.js)
    assert "units.js" in html


def test_template_sheet_round_trip_in_every_language(db, tmp_path, lang):
    """O modelo de planilha do projeto, baixado em qualquer idioma, volta a ser lido igual."""
    from services import site_examples
    from services.site_import import import_file, template_xlsx
    from services.site_service import merged

    ex = site_examples.EXAMPLES[0]
    base, _m, errs = import_file(db, site_examples.write_workbook(ex, tmp_path / "ex.xlsx"), merged({}))
    assert not [e for e in errs if not e.startswith("Pedidos")]  # pedidos: saídas negativas (opção de valor absoluto)
    pt_path = tmp_path / "modelo_pt.xlsx"
    pt_path.write_bytes(template_xlsx(db, base))
    _c, _m, pt_errs = import_file(db, pt_path, merged({}))  # o modelo traz a aba de pedidos vazia
    for code in OTHER_LANGS:
        lang(code, "metric")
        path = tmp_path / f"modelo_{code}.xlsx"
        path.write_bytes(template_xlsx(db, base))
        cfg, _m, errs = import_file(db, path, merged({}))
        assert len(errs) == len(pt_errs), (code, errs[:3], pt_errs[:3])
        b1, b2 = base["building"], cfg["building"]
        for k in ("length_m", "width_m", "clear_height_m", "aisle_m"):
            assert float(str(b1[k]).replace(",", ".")) == float(str(b2[k]).replace(",", ".")), (code, k)
        assert len(cfg.get("skus") or []) == len(base.get("skus") or []), code
        assert (cfg.get("structures") or {}) == (base.get("structures") or {}), code
