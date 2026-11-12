"""Idiomas e unidades de medida da interface.

Como funciona
-------------
* Os textos do código ficam em português (pt-BR), que é o idioma de origem. Cada texto exibido é marcado:
  ``_("Texto")`` (Python e telas), ``_h("Texto com <b>{x}</b>", x=valor)`` (texto com HTML; os valores são
  escapados), ``N_("Texto")`` (só marca; para constantes de módulo, traduzidas na hora de exibir) e, no
  JavaScript, ``_t('Texto {x}', {x: valor})``.
* As traduções ficam em ``locales/<idioma>.json`` no formato ``{"texto em português": "tradução"}``. Texto sem
  tradução aparece em português (nunca quebra a tela). ``scripts/i18n_extract.py`` lista todos os textos
  marcados em ``locales/messages.json`` e o teste ``tests/test_i18n.py`` exige tradução para todos.
* O idioma vem do cookie ``nk_lang`` (seletor no menu) ou, na primeira visita, do idioma do navegador.
* Unidades: os valores são sempre guardados no sistema métrico (cm, kg, m). O cookie ``nk_units`` escolhe a
  exibição: ``auto`` (imperial para inglês, métrico para os demais), ``metric`` ou ``imperial``. A conversão é
  feita na exibição e de volta na entrada (ver ``static/units.js`` e os filtros ``len``/``wt`` das telas).
"""
from __future__ import annotations

import contextvars
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from markupsafe import Markup, escape

BASE_DIR = Path(__file__).resolve().parent.parent
LOCALES_DIR = BASE_DIR / "locales"

DEFAULT_LANG = "pt-BR"
# código -> (nome no próprio idioma, código do navegador/Intl)
LANGUAGES: dict[str, tuple[str, str]] = {
    "pt-BR": ("Português (Brasil)", "pt-BR"),
    "en": ("English", "en-US"),
    "es": ("Español", "es-ES"),
    "zh-CN": ("中文（简体）", "zh-CN"),
    "de": ("Deutsch", "de-DE"),
    "fr": ("Français", "fr-FR"),
}
UNIT_MODES = ("auto", "metric", "imperial")

_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("nk_lang", default=DEFAULT_LANG)
_UNITS: contextvars.ContextVar[str] = contextvars.ContextVar("nk_units", default="auto")

_CATALOGS: dict[str, dict[str, str]] = {}
_LAZY: set[str] = set()  # textos marcados com N_ (constantes de módulo): traduzidos também ao exibir


# --------------------------------------------------------------------------- #
# Idioma atual
# --------------------------------------------------------------------------- #
def normalize_lang(code: Optional[str]) -> Optional[str]:
    """'pt', 'pt-br', 'zh', 'zh-Hans', 'en-GB'... -> código suportado (ou None)."""
    if not code:
        return None
    c = code.strip().replace("_", "-").lower()
    if not c:
        return None
    for k in LANGUAGES:
        if c == k.lower():
            return k
    base = c.split("-")[0]
    return {"pt": "pt-BR", "en": "en", "es": "es", "zh": "zh-CN", "de": "de", "fr": "fr"}.get(base)


def lang_from_accept(header: Optional[str]) -> str:
    """Primeiro idioma suportado do cabeçalho Accept-Language (por ordem de preferência)."""
    best: list[tuple[float, int, str]] = []
    for i, part in enumerate((header or "").split(",")):
        bits = part.strip().split(";")
        q = 1.0
        for b in bits[1:]:
            if b.strip().startswith("q="):
                try:
                    q = float(b.strip()[2:])
                except ValueError:
                    q = 0.0
        code = normalize_lang(bits[0])
        if code:
            best.append((-q, i, code))
    return sorted(best)[0][2] if best else DEFAULT_LANG


def get_lang() -> str:
    return _LANG.get()


def set_lang(code: Optional[str]) -> contextvars.Token:
    return _LANG.set(normalize_lang(code) or DEFAULT_LANG)


def get_units_mode() -> str:
    return _UNITS.get()


def set_units(mode: Optional[str]) -> contextvars.Token:
    return _UNITS.set(mode if mode in UNIT_MODES else "auto")


def unit_system(lang: Optional[str] = None, mode: Optional[str] = None) -> str:
    """'metric' ou 'imperial' para o idioma e a escolha de unidades."""
    mode = mode or get_units_mode()
    if mode in ("metric", "imperial"):
        return mode
    return "imperial" if (lang or get_lang()) == "en" else "metric"


def is_imperial() -> bool:
    return unit_system() == "imperial"


def locale_code(lang: Optional[str] = None) -> str:
    """Código para Intl/toLocaleString no navegador (ex.: 'pt-BR', 'en-US')."""
    return LANGUAGES.get(lang or get_lang(), LANGUAGES[DEFAULT_LANG])[1]


# --------------------------------------------------------------------------- #
# Catálogos
# --------------------------------------------------------------------------- #
def _load(lang: str) -> dict[str, str]:
    if lang not in _CATALOGS:
        p = LOCALES_DIR / f"{lang}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):  # catálogo corrompido: mostra o português
            data = {}
        _CATALOGS[lang] = {k: v for k, v in data.items() if isinstance(v, str) and v}
    return _CATALOGS[lang]


_VERSION = ""


def reload_catalogs() -> None:
    global _VERSION
    _CATALOGS.clear()
    _LAZY.clear()
    _LAZY.update(_lazy_ids())
    _VERSION = _compute_version()


def _lazy_ids() -> set[str]:
    p = LOCALES_DIR / "messages.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {k for k, v in data.items() if isinstance(v, dict) and v.get("lazy")}


def catalog(lang: Optional[str] = None) -> dict[str, str]:
    lang = lang or get_lang()
    return {} if lang == DEFAULT_LANG else _load(lang)


def catalog_version() -> str:
    """Muda quando algum catálogo muda (invalida o cache do navegador para /i18n/<idioma>.js)."""
    return _VERSION


def _compute_version() -> str:
    import hashlib

    h = hashlib.sha1()
    for p in sorted(LOCALES_DIR.glob("*.json")):
        try:
            h.update(p.name.encode() + p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


def lookup(msg: str, lang: Optional[str] = None) -> str:
    if not msg:
        return msg
    return catalog(lang).get(msg, msg)


def _arg(v: Any) -> Any:
    """Valores das variáveis: textos de constantes (N_) também são traduzidos."""
    if isinstance(v, str) and not isinstance(v, Markup) and v in _LAZY:
        return lookup(v)
    return v


def gettext(msg: str, /, **kw: Any) -> str:
    """Traduz ``msg`` para o idioma atual; ``{nome}`` no texto recebe ``nome=valor``."""
    s = lookup(msg)
    if not kw:
        return s
    try:
        return s.format(**{k: _arg(v) for k, v in kw.items()})
    except (KeyError, IndexError, ValueError):  # tradução com marcador errado: usa o original
        return msg.format(**{k: _arg(v) for k, v in kw.items()})


def gettext_html(msg: str, /, **kw: Any) -> Markup:
    """Como ``gettext``, para texto com marcação HTML: o texto é HTML; os valores são escapados."""
    s = lookup(msg)
    try:
        return Markup(s).format(**{k: _arg(v) for k, v in kw.items()}) if kw else Markup(s)
    except (KeyError, IndexError, ValueError):
        return Markup(msg).format(**{k: _arg(v) for k, v in kw.items()}) if kw else Markup(msg)


def N_(msg: str) -> str:
    """Marca um texto para tradução sem traduzir agora (constantes definidas na importação do módulo)."""
    return msg


def tr(value: Any) -> Any:
    """Traduz um texto de constante (marcado com N_) ou qualquer texto do catálogo; outros valores voltam iguais."""
    if isinstance(value, str) and not isinstance(value, Markup):
        return lookup(value)
    return value


def finalize(value: Any) -> Any:
    """Saída das telas ({{ valor }}): constantes marcadas com N_ aparecem traduzidas."""
    if isinstance(value, str) and not isinstance(value, Markup) and value in _LAZY:
        return lookup(value)
    return value


_ = gettext
_h = gettext_html

# --------------------------------------------------------------------------- #
# Números no formato do idioma
# --------------------------------------------------------------------------- #
# (separador decimal, separador de milhar)
_SEPS = {"pt-BR": (",", "."), "es": (",", "."), "de": (",", "."), "fr": (",", " "), "en": (".", ","), "zh-CN": (".", ",")}


def seps(lang: Optional[str] = None) -> tuple[str, str]:
    return _SEPS.get(lang or get_lang(), _SEPS[DEFAULT_LANG])


def localize_number_text(text: str, grouped: bool = True) -> str:
    """'1,234.5' (formato Python) -> formato do idioma atual."""
    dec, th = seps()
    return text.replace(",", "\x00").replace(".", dec).replace("\x00", th if grouped else "")


def fmt_num(value: Any, places: Optional[int] = None, grouped: bool = True) -> str:
    """Número no formato do idioma. ``places``: casas fixas; None: sem zeros à direita."""
    if value is None or value == "":
        return ""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if places is not None:
        text = f"{dec:,.{places}f}"
    else:
        normalized = dec.normalize()
        text = f"{normalized.to_integral():,f}" if normalized == normalized.to_integral() else f"{normalized:,f}"
    return localize_number_text(text, grouped)


_DATE = {"pt-BR": "%d/%m/%Y", "es": "%d/%m/%Y", "fr": "%d/%m/%Y", "de": "%d.%m.%Y", "en": "%m/%d/%Y", "zh-CN": "%Y-%m-%d"}
_DAY_MONTH = {"pt-BR": "%d/%m", "es": "%d/%m", "fr": "%d/%m", "de": "%d.%m.", "en": "%m/%d", "zh-CN": "%m-%d"}


def fmt_datetime(dt: Any, time: bool = True, year: bool = True) -> str:
    """Data (e hora) no formato do idioma: 31/12/2025 14:30, 12/31/2025 14:30, 31.12.2025 14:30, 2025-12-31 14:30."""
    if dt is None or dt == "":
        return ""
    fmt = (_DATE if year else _DAY_MONTH).get(get_lang(), _DATE[DEFAULT_LANG])
    return dt.strftime(fmt + (" %H:%M" if time else ""))


def translate_workbook(wb) -> None:
    """Planilha gerada: nomes das abas e textos fixos das células no idioma atual (textos do catálogo)."""
    if get_lang() == DEFAULT_LANG:
        return
    for ws in wb.worksheets:
        ws.title = lookup(ws.title)[:31]
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value:
                    t = lookup(c.value)
                    if t != c.value:
                        c.value = units_in_text(t)


# --------------------------------------------------------------------------- #
# Unidades
# --------------------------------------------------------------------------- #
CM_PER_IN = Decimal("2.54")
M_PER_FT = Decimal("0.3048")
KG_PER_LB = Decimal("0.45359237")

# unidade métrica -> (unidade imperial, fator: imperial = métrico / fator)
_UNIT_MAP: dict[str, tuple[str, Decimal]] = {
    "cm": ("in", CM_PER_IN),
    "mm": ("in", Decimal("25.4")),
    "m": ("ft", M_PER_FT),
    "km": ("mi", Decimal("1.609344")),
    "kg": ("lb", KG_PER_LB),
    "g": ("oz", Decimal("28.349523125")),
    "m²": ("ft²", M_PER_FT * M_PER_FT),
    "m2": ("ft²", M_PER_FT * M_PER_FT),
    "cm²": ("in²", CM_PER_IN * CM_PER_IN),
    "m³": ("ft³", M_PER_FT ** 3),
    "m3": ("ft³", M_PER_FT ** 3),
    "cm³": ("in³", CM_PER_IN ** 3),
    "L": ("ft³", Decimal("28.316846592")),
    "m/s": ("ft/s", M_PER_FT),
    "m/min": ("ft/min", M_PER_FT),
    "kg/m²": ("lb/ft²", KG_PER_LB / (M_PER_FT * M_PER_FT)),
    "kg/m³": ("lb/ft³", KG_PER_LB / (M_PER_FT ** 3)),
}


def unit_label(unit: str) -> str:
    """Rótulo da unidade no sistema atual ('cm' -> 'in' no imperial)."""
    if is_imperial() and unit in _UNIT_MAP:
        return _UNIT_MAP[unit][0]
    return unit


def to_display(value: Any, unit: str) -> Optional[Decimal]:
    """Valor guardado (métrico) -> valor exibido no sistema atual."""
    if value is None or value == "":
        return None
    v = value if isinstance(value, Decimal) else Decimal(str(value))
    if is_imperial() and unit in _UNIT_MAP:
        return v / _UNIT_MAP[unit][1]
    return v


def from_display(value: Any, unit: str) -> Optional[Decimal]:
    """Valor digitado no sistema atual -> valor métrico para guardar."""
    if value is None or value == "":
        return None
    v = value if isinstance(value, Decimal) else Decimal(str(value))
    if is_imperial() and unit in _UNIT_MAP:
        return v * _UNIT_MAP[unit][1]
    return v


def fmt_unit(value: Any, unit: str, places: Optional[int] = None, suffix: bool = True) -> str:
    """Valor métrico formatado no sistema e idioma atuais, com a unidade (ex.: '39,4 in')."""
    v = to_display(value, unit)
    if v is None:
        return ""
    if places is None:
        places = 1 if is_imperial() and unit in _UNIT_MAP else None
    if places is not None:
        v = v.quantize(Decimal(1).scaleb(-places))
        text = fmt_num(v.normalize() if v == v.to_integral() else v, None)
    else:
        text = fmt_num(v)
    return f"{text} {unit_label(unit)}" if suffix else text


def units_in_text(text: str) -> str:
    """Troca os rótulos de unidade num texto já traduzido ('Largura (cm)' -> 'Largura (in)') no imperial.

    Só troca rótulos isolados entre parênteses ou depois de número ('(cm)', '(m²)', '120 cm' não: números
    não são convertidos aqui, então só os parênteses)."""
    if not is_imperial() or not text:
        return text
    return re.sub(r"\((kg/m²|kg/m³|m/min|m/s|cm²|cm³|m²|m³|mm|cm|km|kg|m|g)\)",
                  lambda m: f"({_UNIT_MAP[m.group(1)][0]})", text)


def js_bootstrap() -> dict[str, Any]:
    """Dados do idioma e das unidades para o JavaScript (window.NK_I18N)."""
    dec, th = seps()
    return {"lang": get_lang(), "locale": locale_code(), "units": unit_system(), "dec": dec, "th": th}


reload_catalogs()
