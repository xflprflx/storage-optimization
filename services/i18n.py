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


_REVERSE: dict[str, str] = {}
_REVERSE_VER = ""


def _uninorm(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").casefold())


def source_text(text: str) -> str:
    """Texto em qualquer idioma -> o texto original em português, quando é uma tradução do catálogo
    ("Front (cm)" -> "Frente (cm)", "高度 (cm)" -> "Altura (cm)"). Usado na leitura de planilhas: títulos de
    colunas, nomes de abas e palavras-chave valem em todos os idiomas. Texto desconhecido volta igual."""
    global _REVERSE, _REVERSE_VER
    if not text:
        return text
    ver = catalog_version()
    if ver != _REVERSE_VER:
        rev: dict[str, str] = {}
        src = {_uninorm(k) for lang in LANGUAGES if lang != DEFAULT_LANG for k in catalog(lang)}
        # mesma tradução para mais de um original ("Não usar" e "Não utilizar" -> "Do not use"): ganha o texto
        # usado nas planilhas (código de importação/modelos) e, depois, o mais longo
        try:
            where = json.loads((LOCALES_DIR / "messages.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            where = {}

        def prio(k: str) -> tuple:
            files = " ".join((where.get(k) or {}).get("src") or [])
            return (bool(re.search(r"import|reader|examples", files)), len(k))

        for lang in LANGUAGES:
            if lang == DEFAULT_LANG:
                continue
            for k, v in catalog(lang).items():
                for t in (v, v[:31]):
                    key = _uninorm(t)
                    if key and key not in src and (key not in rev or prio(k) > prio(rev[key])):
                        rev[key] = k
        _REVERSE, _REVERSE_VER = rev, ver
    return _REVERSE.get(_uninorm(text), text)


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


# símbolo de moeda exibido por idioma (os custos são digitados pelo usuário na moeda dele; não há câmbio)
_CURRENCY = {"pt-BR": "R$", "en": "$", "es": "$", "fr": "€", "de": "€", "zh-CN": "¥"}


def currency() -> str:
    return _CURRENCY.get(get_lang(), "R$")


def fmt_money(value: Any, places: int = 2) -> str:
    """Valor com o símbolo de moeda do idioma: 'R$ 1.234,50', '$ 1,234.50', '1 234,50 €'."""
    if value is None or value == "":
        return "—"
    v = fmt_num(value, places)
    return f"{v} {currency()}" if currency() == "€" else f"{currency()} {v}"


def fmt_datetime(dt: Any, time: bool = True, year: bool = True) -> str:
    """Data (e hora) no formato do idioma: 31/12/2025 14:30, 12/31/2025 14:30, 31.12.2025 14:30, 2025-12-31 14:30."""
    if dt is None or dt == "":
        return ""
    fmt = (_DATE if year else _DAY_MONTH).get(get_lang(), _DATE[DEFAULT_LANG])
    return dt.strftime(fmt + (" %H:%M" if time else ""))


def translate_workbook(wb) -> None:
    """Planilha gerada: nomes das abas e textos fixos das células no idioma atual (textos do catálogo) e, no
    sistema imperial, as medidas: colunas com a unidade no título ("Altura (cm)") têm os números convertidos."""
    imperial = is_imperial()
    if get_lang() == DEFAULT_LANG and not imperial:
        return
    head_re = re.compile(rf"\(({_LABEL_ALT})(?=[\s,;)])")
    for ws in wb.worksheets:
        ws.title = lookup(ws.title)[:31]
        col_unit: dict[int, str] = {}
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str) and v:
                    if imperial:
                        hm = head_re.search(v)
                        if hm:
                            col_unit[c.column] = hm.group(1)
                        elif c.column in col_unit and not v.strip()[:1].isdigit():
                            col_unit.pop(c.column, None)  # outro bloco de dados na mesma coluna
                    t = units_in_text(lookup(v))
                    if t != v:
                        c.value = t
                elif imperial and c.column in col_unit and isinstance(v, (int, float, Decimal)) \
                        and not isinstance(v, bool):
                    unit = col_unit[c.column]
                    places = _IMP_PLACES.get(_UNIT_MAP[unit][0], 1) + 1
                    c.value = round(float(Decimal(str(v)) / _UNIT_MAP[unit][1]), places)


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


# unidades convertidas em textos (sem "mm" e "g": aparecem em nomes de produtos, ex.: "Luva 10 mm")
_TEXT_UNITS = ("kg/m²", "kg/m³", "m/min", "m/s", "cm²", "cm³", "m²", "m³", "cm", "km", "kg", "m")
_UNIT_ALT = "|".join(re.escape(u) for u in _TEXT_UNITS)
# rótulos "(unidade)" de colunas e campos: inclui litros ("Volume (L)"); nos textos soltos, "50 L" fica (nomes)
_LABEL_ALT = _UNIT_ALT + "|L"
_UNIT_END = r"(?![A-Za-zÀ-ÿ²³/\d])"
# casas decimais ao exibir cada unidade imperial
_IMP_PLACES = {"in": 1, "ft": 1, "mi": 2, "lb": 1, "ft²": 0, "in²": 1, "ft³": 1, "in³": 0, "ft/s": 1,
               "ft/min": 0, "lb/ft²": 1, "lb/ft³": 1}
_NUM_RE: dict[str, re.Pattern] = {}


def _num_pattern(lang: str) -> str:
    dec, th = seps(lang)
    th_re = "[ \u202f\u00a0]" if th == " " else re.escape(th)
    d = re.escape(dec)
    return rf"-?\d{{1,3}}(?:{th_re}\d{{3}})+(?:{d}\d+)?|-?\d+(?:{d}\d+)?"


def _chain_re(lang: str) -> re.Pattern:
    """Número(s) seguidos de unidade: '120 cm', '75–150 cm', '12 × 30 m', '3 a 5 m'."""
    if lang not in _NUM_RE:
        num = _num_pattern(lang)
        sep = r"\s*[–×-]\s*|\s+(?:a|to|à|bis|y|et|e|and|und|or|ou|o|oder)\s+|\s*到\s*"
        _NUM_RE[lang] = re.compile(rf"(?<![\w.,/])((?:{num})(?:(?:{sep})(?:{num}))*)(\s?)({_UNIT_ALT}){_UNIT_END}")
    return _NUM_RE[lang]


def parse_num(text: str, lang: Optional[str] = None) -> Optional[Decimal]:
    """'1.234,5' (pt) / '1,234.5' (en) -> Decimal."""
    dec, th = seps(lang)
    t = text.strip()
    for c in ({th, " ", "\u202f", "\u00a0"} if th == " " else {th}):
        t = t.replace(c, "")
    t = t.replace(dec, ".")
    try:
        return Decimal(t)
    except Exception:
        return None


def _imperial_num(text: str, unit: str) -> str:
    v = parse_num(text)
    if v is None:
        return text
    imp, factor = _UNIT_MAP[unit]
    places = _IMP_PLACES.get(imp, 1)
    out = v / factor
    if places and abs(out) < 1 and out != 0:
        places += 1
    q = out.quantize(Decimal(1).scaleb(-places))
    return fmt_num(q.normalize() if q == q.to_integral() else q)


def units_in_text(text: str) -> str:
    """No imperial, converte as medidas de um texto já traduzido: '120 cm' -> '47,2 in', '75–150 cm' ->
    '29,5–59,1 in' e rótulos 'Largura (cm)' -> 'Largura (in)'. No métrico, devolve o texto como está."""
    if not text or not is_imperial():
        return text
    num = re.compile(_num_pattern(get_lang()))

    def chain(m: re.Match) -> str:
        unit = m.group(3)
        nums = num.sub(lambda n: _imperial_num(n.group(0), unit), m.group(1))
        return f"{nums}{m.group(2)}{_UNIT_MAP[unit][0]}"

    text = _chain_re(get_lang()).sub(chain, text)
    return re.sub(rf"\(({_LABEL_ALT})(?=[\s,;)])", lambda m: f"({_UNIT_MAP[m.group(1)][0]}", text)


def localize_rows(rows: list) -> list:
    """Tabela de relatório (1ª linha = títulos): textos fixos no idioma atual e, no imperial, medidas nos
    textos e os números das colunas com unidade no título ("X (m)") convertidos."""
    out = [[units_in_text(tr(c)) if isinstance(c, str) else c for c in row] for row in rows]
    if not is_imperial() or not rows:
        return out
    head_re = re.compile(rf"\(({_LABEL_ALT})(?=[\s,;)])")
    units = {i: m.group(1) for i, c in enumerate(rows[0]) if isinstance(c, str) and (m := head_re.search(c))}
    num_re = re.compile(rf"^\s*(?:{_num_pattern(get_lang())})\s*$")
    for row in out[1:]:
        for i, unit in units.items():
            if i < len(row) and isinstance(row[i], str) and num_re.match(row[i]):
                row[i] = _imperial_num(row[i], unit)
    return out


# trechos que não convertem: scripts, estilos, textos editáveis e o que está entre <!--nk-metric--> e
# <!--/nk-metric--> (telas de importação: as planilhas são sempre métricas)
_HTML_SKIP = re.compile(r"(?is)<!--nk-metric-->.*?<!--/nk-metric-->|<(script|style|textarea)\b.*?</\1\s*>|<!--.*?-->")
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ATTR = re.compile(r'\b(title|placeholder|aria-label|data-bs-title|data-bs-content|alt)="([^"]*)"')
_UNIT_ONLY = re.compile(rf"^(\s*)({_UNIT_ALT})(\s*)$")


def units_in_html(html: str) -> str:
    """Página inteira no imperial: medidas nos textos e em title/placeholder (fora de script/style/textarea).
    Os valores dos campos <input> são convertidos no navegador (static/units.js)."""
    if not html or not is_imperial():
        return html
    num = _num_pattern(get_lang())
    # número num elemento e a unidade logo depois dele: "<b>1.200</b> m²"
    split_re = re.compile(rf"(<(b|strong|span|i|em)\b[^>]*>)((?:{num}))(</\2>)(\s?)({_UNIT_ALT}){_UNIT_END}")

    def split_fix(m: re.Match) -> str:
        unit = m.group(6)
        return f"{m.group(1)}{_imperial_num(m.group(3), unit)}{m.group(4)}{m.group(5)}{_UNIT_MAP[unit][0]}"

    def text_part(t: str) -> str:
        out, pos = [], 0
        for tm in _HTML_TAG.finditer(t):
            out.append(_text_node(t[pos:tm.start()]))
            tag = tm.group(0)
            if '="' in tag and not tag.startswith("</"):
                tag = _HTML_ATTR.sub(lambda a: f'{a.group(1)}="{units_in_text(a.group(2))}"', tag)
            out.append(tag)
            pos = tm.end()
        out.append(_text_node(t[pos:]))
        return "".join(out)

    def _text_node(t: str) -> str:
        if not t or t.isspace():
            return t
        u = _UNIT_ONLY.match(t)
        if u:
            return f"{u.group(1)}{_UNIT_MAP[u.group(2)][0]}{u.group(3)}"
        return units_in_text(t)

    parts, pos = [], 0
    for m in _HTML_SKIP.finditer(html):
        parts.append(text_part(split_re.sub(split_fix, html[pos:m.start()])))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(text_part(split_re.sub(split_fix, html[pos:])))
    return "".join(parts)


# fontes com chinês para embutir no PDF (a 1ª encontrada); NEOKIOSKI_CJK_FONT aponta outra (.ttf/.ttc)
_CJK_FONTS = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc", "/Library/Fonts/Arial Unicode.ttf",
)


def pdf_fonts() -> tuple[str, str]:
    """Fontes dos PDFs (normal, negrito). Helvetica não tem os caracteres chineses: em zh-CN embute uma fonte
    do sistema com chinês (lista acima ou NEOKIOSKI_CJK_FONT) ou, sem nenhuma, usa a fonte CID STSong-Light
    do reportlab (não embutida: o leitor de PDF precisa ter fontes chinesas)."""
    if get_lang() != "zh-CN":
        return "Helvetica", "Helvetica-Bold"
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    if "NK-CJK" in pdfmetrics.getRegisteredFontNames():
        return "NK-CJK", "NK-CJK"
    for path in filter(None, (os.environ.get("NEOKIOSKI_CJK_FONT"), *_CJK_FONTS)):
        if Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont("NK-CJK", path, subfontIndex=0))
                return "NK-CJK", "NK-CJK"
            except Exception:  # fonte ilegível: tenta a próxima
                continue
    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light"


def js_bootstrap() -> dict[str, Any]:
    """Dados do idioma e das unidades para o JavaScript (window.NK_I18N)."""
    dec, th = seps()
    return {"lang": get_lang(), "locale": locale_code(), "units": unit_system(), "dec": dec, "th": th}


reload_catalogs()
