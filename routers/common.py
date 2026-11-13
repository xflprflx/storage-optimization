"""Utilidades compartilhadas pelos routers: templates, mensagens, paginação e parse de formulários."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import BASE_DIR, fold_text
from services.file_reader import parse_decimal
from services import i18n
from services.i18n import N_, _, _h, fmt_num, seps


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.finalize = i18n.finalize
templates.env.globals.update(_=_, _h=_h, fmt_datetime=i18n.fmt_datetime, tr=i18n.tr, LANGUAGES=i18n.LANGUAGES, get_lang=i18n.get_lang,
                             unit_label=i18n.unit_label, js_i18n=i18n.js_bootstrap, units_mode=i18n.get_units_mode,
                             i18n_version=i18n.catalog_version, fmt_money=i18n.fmt_money, currency=i18n.currency)


# --------------------------------------------------------------------------- #
# Filtros e globais Jinja
# --------------------------------------------------------------------------- #
def fmt_dec(value: Any, places: Optional[int] = None) -> str:
    """Formata Decimal no padrão do idioma atual (pt-BR: 1.234,5), removendo zeros à direita. Aceita também
    número em texto (como foi digitado no formulário, ex.: "7,5")."""
    if isinstance(value, str):
        from services.file_reader import parse_decimal
        try:
            value = parse_decimal(value)
        except ValueError:
            return value
    return fmt_num(value, places)


def dec_input(value: Any) -> str:
    """Valor para campos <input> (sem separador de milhar, com o separador decimal do idioma)."""
    if value is None:
        return ""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    normalized = dec.normalize()
    text = f"{normalized:f}"
    return text.replace(".", seps()[0])


def url_with(request: Request, **params) -> str:
    query = dict(request.query_params)
    for k, v in params.items():
        if v is None:
            query.pop(k, None)
        else:
            query[k] = v
    return f"{request.url.path}?{urlencode(query)}" if query else request.url.path


templates.env.filters["dec"] = fmt_dec


def to_date(value: Any):
    """'2026-10-05' -> date (para fmt_datetime); o que não for data volta igual."""
    from datetime import date

    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return value


templates.env.filters["todate"] = to_date
templates.env.filters["dec_input"] = dec_input
templates.env.globals["url_with"] = url_with


def _material_types_global():
    """Opções de tipo de material para os filtros (consulta leve, distinta e indexada)."""
    from database import SessionLocal
    from services.material_service import material_types

    with SessionLocal() as db:
        return material_types(db)


templates.env.globals["material_types"] = _material_types_global
templates.env.globals["NO_MATERIAL"] = "__none__"


def _help_version() -> str:
    """Versão do catálogo de ajuda dos campos: muda quando o catálogo ou os desenhos mudam (invalida o cache do navegador)."""
    import hashlib
    from pathlib import Path

    from services.field_help import RULES

    js = (Path(__file__).resolve().parent.parent / "static" / "field_help.js").read_bytes()
    return hashlib.sha1(repr(RULES).encode() + js).hexdigest()[:10]


HELP_VERSION = _help_version()


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("flash_msg", request.query_params.get("msg"))
    context.setdefault("flash_level", request.query_params.get("level", "success"))
    context.setdefault("help_version", HELP_VERSION)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str, msg: Optional[str] = None, level: str = "success") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'msg': msg, 'level': level})}"
    return RedirectResponse(url, status_code=303)


async def form_data(request: Request) -> FormData:
    """Dependência: lê o formulário (permite endpoints síncronos com o formulário completo)."""
    # o padrão do Starlette recusa formulários com mais de 1.000 campos (seleções grandes passam disso)
    return await request.form(max_fields=20000)


# --------------------------------------------------------------------------- #
# Paginação
# --------------------------------------------------------------------------- #
@dataclass
class Page:
    items: list
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def window(self) -> list[int]:
        start = max(1, self.page - 3)
        end = min(self.pages, self.page + 3)
        return list(range(start, end + 1))


def paginate(db: Session, stmt, page: int = 1, per_page: int = 50) -> Page:
    page = max(1, page)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pages = max(1, math.ceil(total / per_page))
    page = min(page, pages)
    items = list(db.scalars(stmt.limit(per_page).offset((page - 1) * per_page)).unique())
    return Page(items, total, page, per_page)


# --------------------------------------------------------------------------- #
# Busca textual (sem acento / maiúsculas, várias palavras)
# --------------------------------------------------------------------------- #
def _esc(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(term: str, mode: str = "contains") -> str:
    t = _esc(term)
    if mode == "glob":  # curingas: * = qualquer sequência, ? = um caractere; vale do início ao fim
        return t.replace("*", "%").replace("?", "_")
    return {"starts": f"{t}%", "ends": f"%{t}", "exact": t}.get(mode, f"%{t}%")


SEARCH_MODES = {"contains": N_("contém"), "starts": N_("começa com"), "ends": N_("termina com"), "exact": N_("igual a")}
_TOKEN = re.compile(r'(?:(\w+):)?("([^"]*)"|\S+)')
# apelidos aceitos no prefixo campo: (sku:10*, nome:parafuso, tipo:caixa, end:A-01)
FIELD_ALIASES = {"sku": "sku", "cod": "sku", "codigo": "sku", "nome": "name", "name": "name", "desc": "name",
                 "descricao": "name", "tipo": "material", "material": "material", "mat": "material",
                 "end": "address", "endereco": "address", "local": "address", "pos": "address", "posicao": "address"}


def parse_search(q: str, mode: str = "contains") -> list[tuple[Optional[str], str, str]]:
    """Termos da busca: [(campo|None, termo, modo)].

    Operadores por termo (valem em qualquer busca):  10*  começa com · *10  termina com · 14*87  começa com 14 e
    termina com 87 (* = qualquer sequência, ? = um caractere) · =10  igual ·
    "duas palavras"  frase inteira · campo:termo  só naquele campo (sku:, nome:, tipo:, end:)."""
    out = []
    for m in _TOKEN.finditer((q or "").strip()):
        field, raw = m.group(1), (m.group(3) if m.group(3) is not None else m.group(2))
        fkey = FIELD_ALIASES.get(fold_text(field)) if field else None
        if field and fkey is None:  # "x:y" que não é campo: termo literal
            raw = f"{field}:{raw}"
        md = mode if mode in SEARCH_MODES else "contains"
        if raw.startswith("=") and len(raw) > 1:
            raw, md = raw[1:], "exact"
        elif raw.startswith("^") and len(raw) > 1:
            raw, md = raw[1:], "starts"
        elif "*" in raw or "?" in raw:
            # curingas: 10* começa · *10 termina · 14*87 começa com 14 e termina com 87 · 1?3 um caractere no meio
            md = "glob"
            if not raw.strip("*?") and "?" not in raw:
                raw = ""
        if raw:
            out.append((fkey, raw, md))
    return out


def sku_name_fields(P) -> dict:
    """Campos de busca de produto (SKU ou nome) para `sku:`/`nome:`."""
    return {"sku": ((P.sku,), ()), "name": ((P.name,), ())}


def text_search(stmt, q: str, *columns, address_columns=(), fields: Optional[dict] = None, mode: str = "contains",
                only: Optional[str] = None):
    """Filtra `stmt` por `q` em várias colunas.

    - Ignora acentos e maiúsculas ('acucar' encontra 'Açúcar').
    - Cada termo de `q` precisa aparecer em ALGUMA das colunas (E entre termos, OU entre colunas).
    - Em `address_columns`, separadores são ignorados ('A01', 'A 01' e 'A.01' encontram 'A-01'); a coluna pode ser
      uma lista de endereços separados por espaço (começa/termina/igual valem para cada endereço).
    - `mode`: contains | starts | ends | exact (padrão dos termos sem operador; ver `parse_search`).
    - `fields` {campo: (colunas, colunas de endereço)}: permite `campo:termo`; `only` restringe a um campo.
    """
    terms = parse_search(q, mode)
    if not terms:
        return stmt
    fields = fields or {}
    for fkey, word, md in terms:
        key = fkey or only
        if key and key in fields:
            cols, addr = fields[key]
        else:
            cols, addr = columns, address_columns
        folded = fold_text(word)
        conds = [func.nk_fold(col).like(_like_pattern(folded, md), escape="\\") for col in cols if col is not None]
        compact = re.sub(r"[-./\s|\\]+", "", folded)
        if compact:
            pat = {"starts": f"% {_esc(compact)}%", "ends": f"%{_esc(compact)} %",
                   "exact": f"% {_esc(compact)} %", "glob": f"% {_like_pattern(compact, 'glob')} %"
                   }.get(md, f"%{_esc(compact)}%")
            for col in addr:
                conds.append(func.nk_fold(" " + func.replace(col, "-", "") + " ").like(pat, escape="\\"))
        stmt = stmt.where(or_(*conds) if conds else false())
    return stmt


# --------------------------------------------------------------------------- #
# Ordenação por coluna
# --------------------------------------------------------------------------- #
@dataclass
class Sort:
    key: str
    dir: str  # "asc" | "desc"

    def next_dir(self, key: str) -> str:
        return "desc" if (self.key == key and self.dir == "asc") else "asc"


def apply_sort(stmt, sort: Optional[str], direction: Optional[str], columns: dict, default: str,
               default_dir: str = "asc", tiebreak=None):
    """Aplica ORDER BY a partir de ?sort=<chave>&dir=asc|desc.

    `columns` mapeia chave -> expressão SQL (ou tupla de expressões, para ordenar por várias).
    Chaves desconhecidas caem no padrão (nunca usamos texto do usuário no SQL).
    """
    key = sort if sort in columns else default
    d = direction if direction in ("asc", "desc") else (default_dir if key == default else "asc")
    exprs = columns[key]
    if not isinstance(exprs, (tuple, list)):
        exprs = (exprs,)
    order = []
    for e in exprs:
        e = e.desc() if d == "desc" else e.asc()
        order.append(e.nulls_last())  # valores vazios sempre no fim, nas duas direções
    if tiebreak is not None:
        order.append(tiebreak)
    return stmt.order_by(None).order_by(*order), Sort(key, d)


def to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Parse de formulários
# --------------------------------------------------------------------------- #
class FormErrors(dict):
    def add(self, field: str, msg: str) -> None:
        self.setdefault(field, msg)


def f_str(form: FormData, key: str, max_len: Optional[int] = None) -> str:
    val = (form.get(key) or "").strip()
    return val[:max_len] if max_len else val


def f_bool(form: FormData, key: str) -> bool:
    return str(form.get(key, "")).lower() in {"1", "true", "on", "yes", "sim"}


def f_int(form: FormData, key: str) -> Optional[int]:
    return to_int(form.get(key))


def f_dec(form: FormData, key: str, errors: FormErrors, label: str, allow_negative: bool = False) -> Optional[Decimal]:
    raw = (form.get(key) or "").strip()
    try:
        val = parse_decimal(raw)
    except ValueError:
        errors.add(key, f"{label}: número inválido.")
        return None
    if val is not None and val < 0 and not allow_negative:
        errors.add(key, f"{label}: não pode ser negativo.")
        return None
    return val
