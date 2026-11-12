"""Lista todos os textos marcados para tradução em locales/messages.json.

Uso:  python scripts/i18n_extract.py          (atualiza locales/messages.json)
      python scripts/i18n_extract.py --check  (só confere: sai com erro se algum idioma tiver texto sem tradução)

Marcas reconhecidas:
  Python   _("…"), _h("…"), N_("…")  e, em services/field_help.py, o título e a explicação de cada R(…)
  Telas    {{ _("…") }}, {{ _h("…") }} (e _('…') dentro de expressões Jinja)
  JS       _t('…') / _t("…") nos <script> das telas e em static/*.js
Cada texto guarda onde aparece (src), se é constante de módulo (lazy: traduzida também ao exibir), se tem HTML
(html) e se é usado no JavaScript (js).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOCALES = BASE / "locales"
PY_DIRS = ["routers", "services", "models"]
PY_FILES = ["main.py"]

_JINJA = re.compile(r"""\b(_h|_)\(\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
_JS = re.compile(r"""\b_t\(\s*('(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")""")


def _unquote_jinja(lit: str) -> str:
    return ast.literal_eval(lit)


def _unquote_js(lit: str) -> str:
    body = lit[1:-1]
    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)",
                  lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), None)
                  or (chr(int(m.group(1)[1:], 16)) if m.group(1)[0] in "ux" and len(m.group(1)) > 1 else m.group(1)), body)


def extract() -> dict[str, dict]:
    out: dict[str, dict] = {}

    def add(msg: str, src: str, **flags) -> None:
        if not msg or not msg.strip():
            return
        e = out.setdefault(msg, {"src": []})
        if src not in e["src"] and len(e["src"]) < 6:
            e["src"].append(src)
        for k, v in flags.items():
            if v:
                e[k] = True

    # Python
    files = [p for d in PY_DIRS for p in sorted((BASE / d).glob("*.py"))] + [BASE / f for f in PY_FILES]
    for p in files:
        rel = p.relative_to(BASE).as_posix()
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "R" and rel == "services/field_help.py":
                for a in n.args[2:4]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        add(a.value, f"{rel}:{n.lineno}", help=True)
                continue
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                name = n.func.id
                if name in ("_", "_h", "gettext", "gettext_html"):
                    add(n.args[0].value, f"{rel}:{n.lineno}", html=name in ("_h", "gettext_html"))
                elif name == "N_":
                    add(n.args[0].value, f"{rel}:{n.lineno}", lazy=True)
        if rel == "services/field_help.py":  # LV = {"aisle": ("Corredor", "o corredor…"), …}
            for n in ast.walk(tree):
                if isinstance(n, ast.Tuple) and all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in n.elts) \
                        and len(n.elts) == 2 and " " in n.elts[1].value:
                    for x in n.elts:
                        add(x.value, f"{rel}:{n.lineno}", lazy=True)
    # telas
    for p in sorted((BASE / "templates").rglob("*.html")):
        rel = p.relative_to(BASE).as_posix()
        text = p.read_text(encoding="utf-8")
        for m in _JINJA.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            add(_unquote_jinja(m.group(2)), f"{rel}:{line}", html=m.group(1) == "_h")
        for m in _JS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            add(_unquote_js(m.group(1)), f"{rel}:{line}", js=True)
    for p in sorted((BASE / "static").glob("*.js")):
        rel = p.relative_to(BASE).as_posix()
        text = p.read_text(encoding="utf-8")
        for m in _JS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            add(_unquote_js(m.group(1)), f"{rel}:{line}", js=True)
    return dict(sorted(out.items()))


def missing(messages: dict, lang: str) -> list[str]:
    p = LOCALES / f"{lang}.json"
    cat = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return [m for m in messages if not cat.get(m)]


def main() -> int:
    msgs = extract()
    if "--check" in sys.argv:
        bad = 0
        for p in sorted(LOCALES.glob("*.json")):
            if p.name == "messages.json":
                continue
            miss = missing(msgs, p.stem)
            if miss:
                bad += 1
                print(f"{p.stem}: {len(miss)} sem tradução, ex.: {miss[:3]}")
        return 1 if bad else 0
    LOCALES.mkdir(exist_ok=True)
    (LOCALES / "messages.json").write_text(json.dumps(msgs, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    words = sum(len(re.findall(r"[^\W\d_]{2,}", m)) for m in msgs)
    print(f"{len(msgs)} textos, {words} palavras -> locales/messages.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
