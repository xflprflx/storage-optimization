"""Planta da filial a partir da resposta de um chat de IA (Claude, Gemini, ChatGPT…).

O usuário manda a planta do depósito (foto, PDF, desenho) para o chat junto com o prompt de `prompt()`, que já traz
os corredores e as estantes cadastrados da filial. A IA devolve um JSON simples, em metros:

    {"building": {"length": 60, "width": 35},
     "rows": [{"aisle": "A", "bays": "odd", "x": 4, "y": 2, "direction": "horizontal", "length": 20, "depth": 1.1,
               "corridor_side": "below", "numbering_from": "start"}],
     "obstacles": [{"name": "Pilar", "x": 10, "y": 10, "length": 0.4, "width": 0.4}],
     "reference": {"x": 1, "y": 17, "label": "Expedição"}}

`parse()` converte para o formato do editor (cm, orient/face/rev) e conserta os erros comuns das IAs, com aviso:
texto e ``` em volta, comentários, vírgula sobrando, números em texto (vírgula decimal), medidas em cm ou mm,
chaves em português ou com outros nomes, posição em lista, corredor escrito de outro jeito ("07" = "7"). O app só lê
a descrição, nunca executa código. Nada é salvo: a planta vai para o editor, para revisar e salvar.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from services.i18n import _

MAX_ROWS = 2000
MAX_OBS = 500

_KEYS = {
    "building": ("building", "imovel", "imóvel", "warehouse", "deposito", "depósito", "galpao", "galpão", "site", "floor"),
    "rows": ("rows", "fileiras", "racks", "shelves", "estantes", "fileira", "row_list"),
    "obstacles": ("obstacles", "obstaculos", "obstáculos", "obstacle", "blocks", "walls"),
    "reference": ("reference", "referencia", "referência", "ref", "dock", "expedicao", "expedição", "start"),
    "length": ("length", "comprimento", "len", "l", "size_x", "dx", "w", "width_x"),
    "width": ("width", "largura", "w_y", "size_y", "dy", "d", "h", "height", "depth_y"),
    "aisle": ("aisle", "corredor", "rua", "street", "corridor", "name_aisle"),
    "bays": ("bays", "estantes", "side", "lado", "modules", "modulos", "módulos"),
    "direction": ("direction", "orient", "orientation", "orientacao", "orientação", "direcao", "direção"),
    "depth": ("depth", "profundidade", "deep"),
    "corridor_side": ("corridor_side", "lado_do_corredor", "aisle_side", "access", "access_side", "face", "facing"),
    "numbering_from": ("numbering_from", "numeracao", "numeração", "numbering", "start_from"),
    "name": ("name", "nome", "label", "rotulo", "rótulo", "descricao", "descrição"),
}
_DIR = {"horizontal": "h", "h": "h", "x": "h", "along_x": "h", "leste-oeste": "h", "east-west": "h",
        "vertical": "v", "v": "v", "y": "v", "along_y": "v", "norte-sul": "v", "north-south": "v"}
_SIDE_H = {"below": "+", "bottom": "+", "down": "+", "south": "+", "abaixo": "+", "baixo": "+", "embaixo": "+", "sul": "+",
           "above": "-", "top": "-", "up": "-", "north": "-", "acima": "-", "cima": "-", "norte": "-"}
_SIDE_V = {"right": "+", "direita": "+", "east": "+", "leste": "+",
           "left": "-", "esquerda": "-", "west": "-", "oeste": "-"}
_BAYS = {"odd": "odd", "impares": "odd", "ímpares": "odd", "impar": "odd", "ímpar": "odd",
         "even": "even", "pares": "even", "par": "even",
         "all": "all", "todas": "all", "both": "all", "ambos": "all", "ambas": "all", "tudo": "all"}


def _get(d: dict, key: str, default=None):
    """Valor de `key` pelo nome em inglês ou por um dos sinônimos (sem diferenciar maiúsculas)."""
    if not isinstance(d, dict):
        return default
    low = {str(k).strip().lower().replace(" ", "_"): v for k, v in d.items()}
    for k in _KEYS.get(key, (key,)):
        if k in low:
            return low[k]
    return default


def _f(v) -> Optional[float]:
    """Número (aceita texto com vírgula decimal e unidade: "12,5 m")."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", v.replace(" ", ""))
        if m:
            return float(m.group().replace(",", "."))
    return None


def _xy(d: dict) -> tuple[Optional[float], Optional[float]]:
    """Canto da peça: x/y soltos ou em "position"/"pos"/"corner" (lista [x, y] ou objeto)."""
    x, y = _f(d.get("x")), _f(d.get("y"))
    if x is None or y is None:
        p = next((d[k] for k in ("position", "pos", "corner", "origin", "posicao", "posição", "canto") if k in d), None)
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = _f(p[0]), _f(p[1])
        elif isinstance(p, dict):
            x, y = _f(p.get("x")), _f(p.get("y"))
    return x, y


def _load(text: str):
    from services.appearance import _strip_fences, _uncomment

    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    t = m.group(1).strip() if m else t
    if not t.startswith("{"):
        t = _strip_fences(t)
    try:
        return json.loads(_uncomment(t)), ""
    except json.JSONDecodeError as e:
        return None, _("A resposta não é um JSON válido (linha {line}, coluna {col}): {msg}.", line=e.lineno,
                       col=e.colno, msg=e.msg)


def summary(bays: dict) -> str:
    """Corredores e estantes cadastrados da filial, para a IA ligar os desenhos aos códigos."""
    from services.branch_layout import DEFAULT_BAY_W, DEFAULT_DEPTH, parity

    lines = []
    for a, bs in sorted(bays.items()):
        if not bs:
            continue
        odd = [b.bay for b in bs if parity(b.bay) == 1]
        even = [b.bay for b in bs if parity(b.bay) == 0]
        other = [b.bay for b in bs if parity(b.bay) is None]
        front = sum(b.w or DEFAULT_BAY_W for b in bs) / len(bs) / 100
        depth = max([b.d for b in bs if b.d] or [DEFAULT_DEPTH]) / 100
        def codes(lst):  # a lista (até 30, para a IA ver as faltas na numeração) ou a faixa
            return ", ".join(lst) if len(lst) <= 30 else f"{lst[0]}–{lst[-1]}"
        parts = []
        if odd:
            parts.append(_("ímpares ({n}): {list}", n=len(odd), list=codes(odd)))
        if even:
            parts.append(_("pares ({n}): {list}", n=len(even), list=codes(even)))
        if other:
            parts.append(_("outras ({n}): {list}", n=len(other), list=codes(other)))
        lines.append(_("- {aisle}: {n} estantes — {parts}; frente média {front} m; profundidade {depth} m",
                       aisle=a, n=len(bs), parts="; ".join(parts), front=f"{front:.2f}", depth=f"{depth:.2f}"))
    return "\n".join(lines) or _("(a filial ainda não tem endereços cadastrados)")


def prompt(bays: dict) -> str:
    """Prompt para colar no chat de IA junto com a planta (imagem ou PDF)."""
    return _(
        "Transforme a planta baixa anexada (depósito/armazém) numa descrição para um software de armazém. Responda "
        "SOMENTE com um JSON, sem texto antes ou depois, neste formato:\n"
        "{{\"building\": {{\"length\": 60, \"width\": 35}},\n"
        " \"rows\": [{{\"aisle\": \"A\", \"bays\": \"odd\", \"x\": 4, \"y\": 2, \"direction\": \"horizontal\", "
        "\"length\": 20, \"depth\": 1.1, \"corridor_side\": \"below\", \"numbering_from\": \"start\"}}],\n"
        " \"obstacles\": [{{\"name\": \"Pilar\", \"x\": 10, \"y\": 10, \"length\": 0.4, \"width\": 0.4}}],\n"
        " \"reference\": {{\"x\": 1, \"y\": 17, \"label\": \"Expedição\"}}}}\n\n"
        "Regras:\n"
        "- Todas as medidas em METROS. Use as cotas da planta; sem cotas, estime pela escala ou pelas medidas das "
        "estantes abaixo.\n"
        "- Origem no canto de cima à esquerda do imóvel, como a planta aparece: x cresce para a direita, y para "
        "baixo. building.length = tamanho em x; building.width = tamanho em y.\n"
        "- rows: cada fileira de estantes encostadas lado a lado. x, y = canto de cima à esquerda do retângulo da "
        "fileira. direction \"horizontal\" (a fileira corre ao longo de x: length em x, depth em y) ou \"vertical\" "
        "(length em y, depth em x).\n"
        "- corridor_side = de que lado fica o corredor de onde se pegam os produtos: \"above\" ou \"below\" nas "
        "fileiras horizontais, \"left\" ou \"right\" nas verticais. Duas fileiras costas com costas são DUAS rows, "
        "cada uma com o seu lado de corredor.\n"
        "- aisle = o código do corredor (lista abaixo). bays = \"odd\" (estantes ímpares), \"even\" (pares), \"all\" "
        "(todas do corredor) ou a lista dos códigos das estantes daquela fileira, ex.: [\"01\", \"03\", \"05\"]. "
        "Normalmente as ímpares ficam de um lado do corredor e as pares do outro.\n"
        "- numbering_from = \"start\" se os números das estantes crescem com x (horizontal) ou com y (vertical); "
        "\"end\" se crescem no sentido contrário.\n"
        "- obstacles: pilares, paredes internas, escritórios, rampas, docas, áreas sem estantes (retângulos: x, y do "
        "canto de cima à esquerda, length em x, width em y).\n"
        "- reference: o ponto de onde a separação sai e para onde volta (expedição, doca, mesa de conferência).\n"
        "- Use só os corredores da lista; o que não reconhecer na planta, deixe de fora.\n\n"
        "Corredores e estantes cadastrados:\n{aisles}", aisles=summary(bays))


def parse(text: str, bays: dict) -> tuple[Optional[dict], str, list[str]]:
    """Resposta da IA -> (planta no formato do editor, erro, avisos dos consertos)."""
    from services.app_settings import canon
    from services.branch_layout import DEFAULT_BAY_W, DEFAULT_DEPTH, clean, row_bays

    if not (text or "").strip():
        return None, _("Cole a resposta da IA."), []
    obj, err = _load(text)
    if err:
        return None, err, []
    notes: list[str] = []
    if isinstance(obj, list):  # só a lista de fileiras
        obj = {"rows": obj}
        notes.append(_("A resposta tinha só a lista de fileiras: o tamanho do imóvel foi calculado por elas."))
    if not isinstance(obj, dict):
        return None, _("A resposta não descreve uma planta (esperado um objeto com building, rows, obstacles e reference)."), []
    for k in ("building", "rows", "obstacles", "reference"):  # um nível abaixo ({"plan": {...}})
        if _get(obj, k) is None:
            inner = next((v for v in obj.values() if isinstance(v, dict) and _get(v, k) is not None), None)
            if inner is not None:
                obj = inner
                break
    rows_in = _get(obj, "rows") or []
    if not isinstance(rows_in, list) or not any(isinstance(r, dict) for r in rows_in):
        return None, _("A resposta não tem fileiras (rows)."), []
    b = _get(obj, "building") or {}
    L, W = _f(_get(b, "length")), _f(_get(b, "width"))

    # unidade: metros pedidos; detectada pelo tamanho do imóvel (ou de tudo) e pela chave "unit"
    unit = str(obj.get("unit") or obj.get("units") or b.get("unit") or "").strip().lower()
    big = max([v for v in (L, W) if v] or [0.0])
    if not big:
        big = max([(_f(r.get("x")) or 0) + (_f(_get(r, "length")) or 0) for r in rows_in if isinstance(r, dict)] or [0])
    k = 100.0
    if unit in ("cm", "centimeters", "centímetros", "centimetros") or (not unit and 400 < big <= 40000):
        k = 1.0
        notes.append(_("Medidas em centímetros na resposta: convertidas."))
    elif unit in ("mm", "millimeters", "milímetros", "milimetros") or (not unit and big > 40000):
        k = 0.1
        notes.append(_("Medidas em milímetros na resposta: convertidas."))
    cm = lambda v: None if v is None else v * k  # noqa: E731

    by_canon = {canon(a): a for a in bays}
    rows, skipped = [], []
    for i, r in enumerate([r for r in rows_in if isinstance(r, dict)][:MAX_ROWS]):
        a_raw = str(_get(r, "aisle") or "").strip()
        aisle = bays and (a_raw.upper() if a_raw.upper() in bays else by_canon.get(canon(a_raw)))
        if not aisle:
            skipped.append(a_raw or f"#{i + 1}")
            continue
        x, y = _xy(r)
        if x is None or y is None:
            notes.append(_("Fileira {n} ({aisle}) sem posição x, y: ficou de fora.", n=i + 1, aisle=aisle))
            continue
        d_raw = str(_get(r, "direction") or "").strip().lower().replace(" ", "_")
        orient = _DIR.get(d_raw)
        length, depth = _f(_get(r, "length")), _f(_get(r, "depth"))
        if orient is None:  # sem direção: pela forma do retângulo
            wd = _f(r.get("width")) if "width" in r else None
            orient = "v" if (wd and length and wd > length) else "h"
            if d_raw:
                notes.append(_("Fileira {n} ({aisle}): direção \"{v}\" desconhecida, usada {o}.", n=i + 1, aisle=aisle,
                               v=d_raw, o=_("horizontal") if orient == "h" else _("vertical")))
        s_raw = str(_get(r, "corridor_side") or "").strip().lower()
        face = (_SIDE_H if orient == "h" else _SIDE_V).get(s_raw)
        if face is None:
            other = (_SIDE_V if orient == "h" else _SIDE_H).get(s_raw)
            face = "+"
            if s_raw:
                notes.append(_("Fileira {n} ({aisle}): lado do corredor \"{v}\" não combina com a direção; ajuste no "
                               "editor (F troca o lado).", n=i + 1, aisle=aisle, v=s_raw) if other else
                             _("Fileira {n} ({aisle}): lado do corredor \"{v}\" desconhecido; ajuste no editor (F).",
                               n=i + 1, aisle=aisle, v=s_raw))
        bv = _get(r, "bays")
        side, blist = "all", None
        if isinstance(bv, list):
            blist = [str(c).strip().upper() for c in bv if str(c).strip()]
        elif isinstance(bv, str) and bv.strip():
            side = _BAYS.get(bv.strip().lower())
            if side is None:
                side = "all"
                notes.append(_("Fileira {n} ({aisle}): estantes \"{v}\" desconhecidas, usadas todas.", n=i + 1,
                               aisle=aisle, v=bv))
        rev = str(_get(r, "numbering_from") or "start").strip().lower() in ("end", "fim", "reverse", "reversed", "desc")
        row = {"aisle": aisle, "side": side, "x": cm(x), "y": cm(y), "orient": orient,
               "face": face, "rev": rev, "depth": cm(depth) if depth else None, "bay_w": DEFAULT_BAY_W}
        if blist:
            have = {canon(bb.bay): bb.bay for bb in bays[aisle]}
            found = [have[canon(c)] for c in blist if canon(c) in have]
            if len(found) < len(blist):
                notes.append(_("Fileira {n} ({aisle}): estante(s) não cadastrada(s) ignorada(s): {list}.", n=i + 1,
                               aisle=aisle, list=", ".join(c for c in blist if canon(c) not in have)[:200]))
            if found:
                row["bays"] = found
        bs = row_bays(row, bays)
        if not bs:
            notes.append(_("Fileira {n} ({aisle}): nenhuma estante cadastrada nesse lado; confira no editor.", n=i + 1,
                           aisle=aisle))
        if row["depth"] is None:
            row["depth"] = max([bb.d for bb in bs if bb.d] or [DEFAULT_DEPTH])
        free = [bb for bb in bs if not bb.w]
        if length and bs:  # frente das estantes sem estrutura: pelo comprimento desenhado
            fixed = sum(bb.w for bb in bs if bb.w)
            if free and cm(length) > fixed:
                row["bay_w"] = round((cm(length) - fixed) / len(free), 1)
            real = fixed + len(free) * row["bay_w"]
            if abs(real - cm(length)) > max(50.0, 0.15 * cm(length)):
                notes.append(_("Fileira {n} ({aisle}): na planta tem {a} m, mas as estantes cadastradas somam {b} m.",
                               n=i + 1, aisle=aisle, a=f"{cm(length) / 100:.1f}", b=f"{real / 100:.1f}"))
        rows.append(row)
    if skipped:
        notes.append(_("Corredor(es) que não existem nos endereços da filial ficaram de fora: {list}.",
                       list=", ".join(sorted(set(skipped)))[:300]))
    if not rows:
        return None, _("Nenhuma fileira da resposta é de um corredor cadastrado nesta filial."), notes

    obstacles = []
    obs_in = _get(obj, "obstacles") or []
    for o in (obs_in if isinstance(obs_in, list) else [])[:MAX_OBS]:
        if not isinstance(o, dict):
            continue
        x, y = _xy(o)
        w, d = _f(_get(o, "length")), _f(_get(o, "width"))
        if None in (x, y, w, d):
            notes.append(_("Obstáculo sem posição ou tamanho ignorado: {name}.", name=str(_get(o, "name") or "?")[:40]))
            continue
        obstacles.append({"x": cm(x), "y": cm(y), "w": cm(w), "d": cm(d), "name": str(_get(o, "name") or "")[:60]})

    # imóvel: o informado; sem ele (ou menor que o desenho), o que cobre fileiras e obstáculos
    def extent(r):
        n = len(row_bays(r, bays)) or 1
        ln = sum(bb.w or r["bay_w"] for bb in row_bays(r, bays)) or n * r["bay_w"]
        return (r["x"] + (ln if r["orient"] == "h" else r["depth"]), r["y"] + (r["depth"] if r["orient"] == "h" else ln))
    ex = [extent(r) for r in rows] + [(o["x"] + o["w"], o["y"] + o["d"]) for o in obstacles]
    need_L, need_W = max(e[0] for e in ex), max(e[1] for e in ex)
    L, W = cm(L), cm(W)
    if not L or not W:
        notes.append(_("Sem o tamanho do imóvel: calculado pelas fileiras e obstáculos."))
    elif L + 1 < need_L or W + 1 < need_W:
        notes.append(_("Fileiras ou obstáculos passavam do imóvel ({L} × {W} m): o imóvel foi aumentado.",
                       L=f"{L / 100:.1f}", W=f"{W / 100:.1f}"))
    L, W = max(L or 0, need_L + (0 if L else 200)), max(W or 0, need_W + (0 if W else 200))

    ref = _get(obj, "reference")
    rx, ry = _xy(ref) if isinstance(ref, dict) else (None, None)
    if rx is None or ry is None:
        notes.append(_("Sem o ponto de referência (expedição): ficou no meio da parede esquerda; marque no editor."))
        rx, ry = 100.0 / k, W / 2 / k
    label = str(_get(ref, "name") or _get(ref, "label") or "") if isinstance(ref, dict) else ""
    data = clean({"L": L, "W": W, "rows": rows, "obstacles": obstacles,
                  "ref": {"x": cm(rx), "y": cm(ry), "label": label or _("expedição")}})
    return data, "", notes
