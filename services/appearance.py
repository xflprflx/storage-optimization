"""
Aparência 3D de bins e estruturas: como cada item é desenhado no 3D (projeto de filial nova e planta das filiais).

Não muda nenhum cálculo: a otimização, a capacidade e o simulador usam só as medidas do cadastro. A aparência é um
conjunto de peças simples (caixas e cilindros) encaixado nessas medidas.

Ordem de escolha (`look_of`):
  1. código colado (modelo "custom"): JSON gerado por uma IA (Claude, Gemini, ChatGPT…) a partir de uma foto, com o
     prompt de `prompt()`; o app só lê a descrição das peças, nunca executa código;
  2. modelo da biblioteca escolhido no cadastro (palete, caixa plástica, porta-palete, câmara fria…), gerado com as
     medidas reais do item (o palete tem 14 cm de base de verdade, a longarina 10 cm…);
  3. "automático" (padrão): o modelo da biblioteca sugerido pelo tipo e pelo nome; sem sugestão,
  4. o desenho simples de sempre (caixa colorida; estrutura com montantes e bandejas).

Formato das peças (o mesmo do código colado):
    {"parts": [{"shape": "box" | "cylinder", "axis": "x" | "y" | "z" (cilindro),
                "x", "y", "z": canto da peça (esquerda, baixo, frente), "w", "h", "d": tamanho — em fração (0–1) da
                largura (x, da esquerda para a direita, vista de frente), da altura (y) e da profundidade (z, da frente
                para o fundo) do item,
                "color": "#rrggbb", "opacity": 0.1–1, "tint": true (pintada com a cor da vista: tipo, ABC…),
                "each_level": true (estruturas: repetida em cada nível; y a partir da base do nível),
                "repeat": {"count": n, "dx", "dy", "dz"} (n cópias, deslocadas em fração a cada uma)}]}
Normalizado para o 3D: {"parts": [{"s": "b"|"c", "a": eixo, x, y, z, w, h, d, "c", "o", "t", "l"}], "tint": bool}.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from services.i18n import N_, _

MAX_PARTS = 60  # peças depois das repetições (o 3D desenha cada peça de todos os itens de uma vez: instancing)
CUSTOM = "custom"
EXAMPLE_DIMS = {"bin": (40.0, 30.0, 60.0), "struct": (100.0, 200.0, 60.0)}  # prévia sem medidas (largura, altura, prof.)
SIMPLE = "simple"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# --------------------------------------------------------------------------- #
# Validação e normalização
# --------------------------------------------------------------------------- #
def _strip_fences(text: str) -> str:
    """Aceita o JSON como a IA costuma devolver: dentro de ```json … ``` ou com texto em volta."""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if t.startswith("[") and t.rstrip().endswith("]"):
        return t  # só a lista de peças
    ib, ic = t.find("["), t.find("{")
    if not t.startswith("{") and ib >= 0 and (ic < 0 or ib < ic) and t.rfind("]") > ib:
        return t[ib:t.rfind("]") + 1]  # lista de peças com texto em volta
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    return t


_COLORS = {"red": "#d62728", "vermelho": "#d62728", "blue": "#2f6db5", "azul": "#2f6db5", "green": "#2e9b4f",
           "verde": "#2e9b4f", "yellow": "#f2c230", "amarelo": "#f2c230", "orange": "#e8772e", "laranja": "#e8772e",
           "brown": "#8b5a2b", "marrom": "#8b5a2b", "wood": "#c8955a", "madeira": "#c8955a", "gray": "#8a96a0",
           "grey": "#8a96a0", "cinza": "#8a96a0", "silver": "#c0c7cc", "prata": "#c0c7cc", "white": "#f2f4f5",
           "branco": "#f2f4f5", "black": "#2b2f33", "preto": "#2b2f33", "beige": "#e8dcc0", "bege": "#e8dcc0",
           "purple": "#7b3fb5", "roxo": "#7b3fb5", "pink": "#e377c2", "rosa": "#e377c2", "cyan": "#5bc0de",
           "transparent": "#dfe6ea", "glass": "#cfe8f3", "vidro": "#cfe8f3"}
_SHAPES = {"box": "box", "cube": "box", "cuboid": "box", "rect": "box", "rectangle": "box", "plane": "box",
           "panel": "box", "slab": "box", "plate": "box", "caixa": "box", "cubo": "box", "placa": "box",
           "cylinder": "cylinder", "cyl": "cylinder", "tube": "cylinder", "pipe": "cylinder", "rod": "cylinder",
           "drum": "cylinder", "cilindro": "cylinder", "tubo": "cylinder", "sphere": "cylinder", "cone": "cylinder"}


def _uncomment(t: str) -> str:
    """Tira comentários // e /* */ (fora das aspas) e vírgulas sobrando antes de } ou ]: erros comuns das IAs."""
    out, i, n, quote = [], 0, len(t), False
    while i < n:
        c = t[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(t[i + 1])
                i += 1
            elif c == '"':
                quote = False
        elif c == '"':
            quote = True
            out.append(c)
        elif t.startswith("//", i):
            while i < n and t[i] != "\n":
                i += 1
            continue
        elif t.startswith("/*", i):
            j = t.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    t = "".join(out)
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return re.sub(r"\b(True|False|None)\b", lambda m: {"True": "true", "False": "false", "None": "null"}[m.group(1)], t)


def _find_parts(obj):
    """A lista de peças onde a IA a pôs: "parts", a raiz, outra chave com uma lista de objetos ou um nível abaixo."""
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("parts"), list):
        return obj["parts"]
    for v in obj.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    for v in obj.values():
        found = _find_parts(v) if isinstance(v, dict) else None
        if found:
            return found
    return None


def _f(v, default: float) -> float:
    if v is None or v == "":
        return default
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return float("nan")


_POS_KEYS = ("position", "pos", "origin", "corner", "offset", "location", "min")
_SIZE_KEYS = ("size", "dimensions", "dims", "scale", "extent", "extents")
_AXES = {"x": ("x", "width", "w", "largura"), "y": ("y", "height", "h", "altura"), "z": ("z", "depth", "d", "length",
                                                                                          "l", "profundidade")}


def _vec(v, names: dict) -> Optional[list]:
    """[a, b, c] ou {x/width…, y/height…, z/depth…} -> [a, b, c] (None se não der)."""
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return list(v[:3])
    if isinstance(v, dict):
        out = []
        for ax in ("x", "y", "z"):
            k = next((n for n in names[ax] if n in v), None)
            out.append(v[k] if k is not None else None)
        return out if any(x is not None for x in out) else None
    return None


def _unpack(q: dict) -> None:
    """Formatos alternativos de posição e tamanho que as IAs usam -> x, y, z, w, h, d (no lugar)."""
    for k in _POS_KEYS:
        v = _vec(q.get(k), _AXES)
        if v is not None:
            for key, val in zip(("x", "y", "z"), v):
                if val is not None and key not in q:
                    q[key] = val
            q.pop(k, None)
            break
    if "center" in q or "centre" in q:
        v = _vec(q.pop("center", None) or q.pop("centre", None), _AXES)
        if v is not None:
            q.update({key: val for key, val in zip(("x", "y", "z"), v) if val is not None})
            q["_center"] = True
    for k in _SIZE_KEYS:
        v = _vec(q.get(k), _AXES)
        if v is not None:
            for key, val in zip(("w", "h", "d"), v):
                if val is not None and key not in q:
                    q[key] = val
            q.pop(k, None)
            break
    for key, names in (("w", ("width", "largura")), ("h", ("height", "altura")),
                       ("d", ("depth", "length", "profundidade", "comprimento"))):
        for n in names:
            if key not in q and n in q:
                q[key] = q.pop(n)
    if "radius" in q and q.get("shape") == "cylinder":  # cilindro com raio: diâmetro nos dois eixos de fora
        r = 2 * _f(q.pop("radius"), 0)
        ax = str(q.get("axis") or "y").lower()
        for key in {"x": ("h", "d"), "y": ("w", "d"), "z": ("w", "h")}.get(ax, ("w", "d")):
            q.setdefault(key, r)


def _repair(parts: list, dims: Optional[tuple], structure: bool) -> tuple[list, list[str], str]:
    """Conserta o que a IA costuma errar, sem mudar a intenção: sinônimos de forma, cor por nome, números em texto,
    medidas em cm (com as medidas do item), valores um pouco fora do intervalo, posição e tamanho em outro formato
    (position/size, center, width/height/depth, radius) e origem no meio do item. Devolve (peças, avisos, erro)."""
    notes: list[str] = []
    out, named, fixed = [], 0, 0
    raise_missing: list[int] = []
    for p in parts:
        if not isinstance(p, dict):
            out.append(p)
            continue
        q = dict(p)
        shape = str(q.get("shape") or q.get("type") or "box").strip().lower()
        q["shape"] = _SHAPES.get(shape, "box")
        q.pop("type", None)
        col = str(q.get("color") or q.get("colour") or "").strip().lower()
        q.pop("colour", None)
        if re.fullmatch(r"#[0-9a-f]{3}", col):
            col = "#" + "".join(ch * 2 for ch in col[1:])
        elif re.fullmatch(r"[0-9a-f]{6}", col):
            col = "#" + col
        if col and not _HEX.match(col):
            named += 1
            col = _COLORS.get(col.split()[0], "#8a96a0")
        elif not col:
            col = "#8a96a0"
        q["color"] = col
        _unpack(q)
        if not any(k in q for k in ("w", "h", "d")):
            raise_missing.append(len(out) + 1)
        big = any(_f(q.get(k), 0) > 1.5 for k in ("w", "h", "d"))  # tamanho que só faz sentido em cm
        for k, dflt in (("x", 0), ("y", 0), ("z", 0), ("w", 1), ("h", 1), ("d", 1)):
            q[k] = _f(q.get(k), dflt)
        if q.pop("_center", False):  # posição dada pelo centro: vira o canto
            q["x"], q["y"], q["z"] = q["x"] - q["w"] / 2, q["y"] - q["h"] / 2, q["z"] - q["d"] / 2
        q["_cm"] = big
        for k, o in (("w", "x"), ("h", "y"), ("d", "z")):  # tamanho negativo: a peça vai para o outro lado
            if q[k] < 0:
                q[o], q[k] = q[o] + q[k], -q[k]
        if "opacity" in q:
            q["opacity"] = min(max(_f(q["opacity"], 1), 0.05), 1)
        out.append(q)
    if raise_missing:  # sem tamanho a peça viraria o item inteiro (e toda prévia ficaria igual): recusa
        return out, [], _("peça {i}: sem tamanho (w, h, d)", i=raise_missing[0])
    good = [q for q in out if isinstance(q, dict)]
    in_cm = any(q.pop("_cm", False) for q in good)
    if in_cm and dims and all(float(v or 0) > 0 for v in dims):  # medidas em cm, não em fração
        W, H, D = (float(v) for v in dims)
        for q in good:
            q["x"], q["w"], q["y"], q["h"], q["z"], q["d"] = q["x"] / W, q["w"] / W, q["y"] / H, q["h"] / H, q["z"] / D, q["d"] / D
            rep = q.get("repeat")
            if isinstance(rep, dict):
                for k, m in (("dx", W), ("dy", H), ("dz", D)):
                    if k in rep:
                        rep[k] = _f(rep[k], 0) / m
        notes.append(_("as medidas vieram em cm e foram convertidas em fração"))
    if good and not in_cm:  # origem no meio do item (x e z de -0,5 a 0,5): leva para o canto
        for k, sz in (("x", "w"), ("z", "d")):
            lo, hi = min(q[k] for q in good), max(q[k] + q[sz] for q in good)
            if lo < -0.1 and hi <= 0.6 and lo >= -0.6:
                for q in good:
                    q[k] += 0.5
                if _("a origem estava no meio do item e foi levada para o canto") not in notes:
                    notes.append(_("a origem estava no meio do item e foi levada para o canto"))
    for q in good:
        lvl = bool(q.get("each_level")) and structure
        lo_y = -0.3 if lvl else -0.05
        for k, lo, hi in (("x", -0.05, 1.05), ("z", -0.05, 1.05), ("y", lo_y, 1.05)):
            if q[k] == q[k] and (q[k] < lo or q[k] > hi) and lo - 0.25 <= q[k] <= hi + 0.25:
                q[k], fixed = min(max(q[k], lo), hi), fixed + 1
        for k in ("w", "h", "d"):
            if q[k] == q[k] and q[k] < 0.0005:
                q[k], fixed = 0.0005, fixed + 1
            elif q[k] == q[k] and 1.1 < q[k] <= 1.35:
                q[k], fixed = 1.1, fixed + 1
    if named:
        notes.append(_("{n} cor(es) por nome ou fora do formato #rrggbb trocada(s) pela cor mais próxima", n=named))
    if fixed:
        notes.append(_("{n} valor(es) um pouco fora do intervalo ajustado(s)", n=fixed))
    return out, notes, ""


def parse(text: str, dims: Optional[tuple] = None, structure: bool = True) -> tuple[Optional[dict], str, list]:
    """Código colado -> (descrição consertada, "", avisos) ou (None, motivo, []). `dims` = (largura, altura,
    profundidade) do item em cm: com elas, medidas em cm viram frações."""
    t = _strip_fences(text)
    if not t:
        return None, _("Cole o código gerado pela IA."), []
    try:
        obj = json.loads(t)
    except ValueError:
        try:
            obj = json.loads(_uncomment(t))
        except ValueError as exc:
            return None, _("O código não é um JSON válido ({exc}). Copie a resposta inteira da IA.", exc=str(exc)[:80]), []
    parts = _find_parts(obj)
    if not parts:
        return None, _("O código precisa ter a lista \"parts\" com pelo menos uma peça."), []
    parts, notes, missing = _repair(parts, dims, structure)
    if missing:
        return None, _("Código inválido: {exc}.", exc=missing) + " " + _(
            "Cada peça precisa do tamanho (w, h, d) e do canto (x, y, z), em fração das medidas do item."), []
    obj = {"parts": parts}
    _look, err = normalize(obj, structure=structure)
    if err and notes and any("cm" in n for n in notes):
        err += " " + _("(As medidas vieram em cm e passam do tamanho do item: confira as medidas do cadastro ou peça à "
                       "IA as frações de 0 a 1.)")
    return (obj, "", notes) if not err else (None, err, [])


def _num(v, lo: float, hi: float, name: str, i: int, clamp: bool = False) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(_("peça {i}: {name} inválido", i=i, name=name)) from None
    if f != f:  # texto que não é número
        raise ValueError(_("peça {i}: {name} inválido", i=i, name=name))
    if clamp:
        return min(max(f, lo), hi)
    if f != f or f < lo - 1e-9 or f > hi + 1e-9:
        raise ValueError(_("peça {i}: {name} fora do intervalo ({lo} a {hi})", i=i, name=name, lo=f"{lo:g}", hi=f"{hi:g}"))
    return f


def normalize(obj, structure: bool = False, clamp: bool = False) -> tuple[Optional[dict], str]:
    """Descrição (formato longo) -> peças normalizadas para o 3D, ou (None, motivo). `clamp`: valores fora do
    intervalo são recortados (modelos da biblioteca em itens muito pequenos) em vez de recusados."""
    if not isinstance(obj, dict) or not isinstance(obj.get("parts"), list) or not obj["parts"]:
        return None, _("O código precisa ter a lista \"parts\" com pelo menos uma peça.")
    out = []
    try:
        for i, p in enumerate(obj["parts"], 1):
            if not isinstance(p, dict):
                raise ValueError(_("peça {i}: formato inválido", i=i))
            shape = str(p.get("shape") or "box").lower()
            if shape not in ("box", "cylinder", "cyl"):
                raise ValueError(_("peça {i}: shape deve ser \"box\" ou \"cylinder\"", i=i))
            lvl = bool(p.get("each_level")) and structure
            ylo = -0.3 if lvl else -0.05
            part = {"s": "b" if shape == "box" else "c",
                    "x": _num(p.get("x", 0), -0.05, 1.05, "x", i, clamp), "y": _num(p.get("y", 0), ylo, 1.05, "y", i, clamp),
                    "z": _num(p.get("z", 0), -0.05, 1.05, "z", i, clamp),
                    "w": _num(p.get("w", 1), 0.0005, 1.1, "w", i, clamp), "h": _num(p.get("h", 1), 0.0005, 1.1, "h", i, clamp),
                    "d": _num(p.get("d", 1), 0.0005, 1.1, "d", i, clamp)}
            if part["s"] == "c":
                ax = str(p.get("axis") or "y").lower()
                if ax not in ("x", "y", "z"):
                    raise ValueError(_("peça {i}: axis deve ser x, y ou z", i=i))
                part["a"] = ax
            color = str(p.get("color") or "#8a96a0").strip()
            if not _HEX.match(color):
                raise ValueError(_("peça {i}: color deve ser hexadecimal (#rrggbb)", i=i))
            part["c"] = color.lower()
            op = _num(p.get("opacity", 1), 0.05, 1, "opacity", i)
            if op < 0.999:
                part["o"] = round(op, 2)
            if p.get("tint"):
                part["t"] = 1
            if lvl:
                part["l"] = 1
            pc = str(p.get("piece") or "").strip().upper()[:30]
            if pc:
                part["pc"] = pc  # peça da estrutura (código da composição; "@FRAME"/"@LEVEL" nos modelos da biblioteca)
            rep = p.get("repeat")
            n, dx, dy, dz = 1, 0.0, 0.0, 0.0
            if isinstance(rep, dict):
                n = int(_num(rep.get("count", 1), 1, MAX_PARTS, "repeat.count", i))
                dx, dy, dz = (_num(rep.get(k, 0), -1, 1, f"repeat.{k}", i) for k in ("dx", "dy", "dz"))
            for k in range(n):
                q = {**part, "x": part["x"] + k * dx, "y": part["y"] + k * dy, "z": part["z"] + k * dz}
                out.append({key: (round(v, 4) if isinstance(v, float) else v) for key, v in q.items()})
                if len(out) > MAX_PARTS:
                    raise ValueError(_("mais de {n} peças (contando as repetições): simplifique o desenho", n=MAX_PARTS))
    except ValueError as exc:
        return None, _("Código inválido: {exc}.", exc=str(exc))
    return {"parts": out, "tint": any(p.get("t") for p in out)}, ""


# --------------------------------------------------------------------------- #
# Biblioteca (medidas reais em cm -> frações)
# --------------------------------------------------------------------------- #
class _Kit:
    """Monta peças em cm (x da esquerda, y do piso ou da base do nível, z da frente) e converte em frações."""

    def __init__(self, w: float, h: float, d: float):
        self.W, self.H, self.D = max(w, 1.0), max(h, 1.0), max(d, 1.0)
        self.parts: list[dict] = []

    def box(self, x, y, z, w, h, d, color, cyl: str = "", **kw):
        p = {"shape": "cylinder" if cyl else "box", "x": x / self.W, "y": y / self.H, "z": z / self.D,
             "w": max(w, 0.05) / self.W, "h": max(h, 0.05) / self.H, "d": max(d, 0.05) / self.D, "color": color}
        if cyl:
            p["axis"] = cyl
        if "rep" in kw:
            n, dx, dy, dz = kw.pop("rep")
            p["repeat"] = {"count": n, "dx": dx / self.W, "dy": dy / self.H, "dz": dz / self.D}
        if not kw.pop("detail", False):  # armação ou o que se repete por nível: leva a cor da peça da composição
            p["piece"] = "@level" if kw.get("each_level") else "@frame"
        p.update(kw)
        self.parts.append(p)
        return self

    def done(self) -> dict:
        return {"parts": self.parts}


WOOD, WOOD2, STEEL, DARK = "#c8955a", "#a8743f", "#8f9aa3", "#2b2f33"


def _pallet(w, h, d):
    k = _Kit(w, h, d)
    base = min(14.0, h * 0.35)
    top, bot = 2.2 * base / 14, 2.0 * base / 14
    n = 7
    bd = d / (n * 1.6)
    k.box(0, base - top, 0, w, top, bd, WOOD, rep=(n, 0, 0, (d - bd) / (n - 1)))  # tábuas de cima
    blk = min(10.0, w / 8, d / 8)
    for x in (0, (w - blk) / 2, w - blk):  # 3 linhas de 3 blocos e as tábuas de baixo
        k.box(x, bot, 0, blk, base - top - bot, blk, WOOD2, rep=(3, 0, 0, (d - blk) / 2))
        k.box(x, 0, 0, blk, bot, d, WOOD)
    if h > base + 2:
        k.box(w * 0.015, base, d * 0.015, w * 0.97, h - base, d * 0.97, "#9fb3c0", tint=True, opacity=0.95)  # carga
    return k.done()


def _tote(w, h, d):
    k = _Kit(w, h, d)
    t = max(0.6, min(1.5, w * 0.04, d * 0.04))
    k.box(0, 0, 0, w, t, d, "#4e79a7", tint=True)
    k.box(0, 0, 0, w, h, t, "#4e79a7", tint=True).box(0, 0, d - t, w, h, t, "#4e79a7", tint=True)
    k.box(0, 0, 0, t, h, d, "#4e79a7", tint=True).box(w - t, 0, 0, t, h, d, "#4e79a7", tint=True)
    hw, hh = min(w * 0.35, 12), min(h * 0.12, 3)
    k.box((w - hw) / 2, h * 0.8, -0.2, hw, hh, 0.3, DARK).box((w - hw) / 2, h * 0.8, d - 0.1, hw, hh, 0.3, DARK)
    return k.done()


def _shelf_bin(w, h, d):
    k = _Kit(w, h, d)
    t = max(0.5, min(1.2, w * 0.04))
    k.box(0, 0, 0, w, t, d, "#4e79a7", tint=True)
    k.box(0, 0, d - t, w, h, t, "#4e79a7", tint=True)
    k.box(0, 0, 0, t, h, d, "#4e79a7", tint=True).box(w - t, 0, 0, t, h, d, "#4e79a7", tint=True)
    k.box(0, 0, 0, w, h * 0.45, t, "#4e79a7", tint=True)  # frente baixa (abertura para pegar)
    k.box(w * 0.3, h * 0.12, -0.3, w * 0.4, h * 0.18, 0.3, "#ffffff")  # porta-etiqueta
    return k.done()


def _carton(w, h, d):
    k = _Kit(w, h, d)
    k.box(0, 0, 0, w, h, d, "#c69c6d")
    k.box(w * 0.4, h - 0.2, 0, w * 0.2, 0.4, d, "#d9c6a5")  # fita
    k.box(w * 0.2, h * 0.35, -0.3, w * 0.6, h * 0.3, 0.3, "#9fb3c0", tint=True)  # etiqueta
    return k.done()


def _cage(w, h, d):
    k = _Kit(w, h, d)
    gb = min(12.0, h * 0.15)
    p = max(1.5, min(3.0, w * 0.03))
    k.box(0, 0, 0, w, gb, d, "#6b7680")
    for x, z in ((0, 0), (w - p, 0), (0, d - p), (w - p, d - p)):
        k.box(x, gb, z, p, h - gb, p, STEEL)
    k.box(0, h - p, 0, w, p, p, STEEL).box(0, h - p, d - p, w, p, p, STEEL)
    k.box(0, h - p, 0, p, p, d, STEEL).box(w - p, h - p, 0, p, p, d, STEEL)
    n = 6
    wire = 0.5
    k.box(w / (n + 1), gb, 0, wire, h - gb, wire, STEEL, rep=(n, w / (n + 1), 0, 0))
    k.box(w / (n + 1), gb, d - wire, wire, h - gb, wire, STEEL, rep=(n, w / (n + 1), 0, 0))
    k.box(0, gb, d / (n + 1), wire, h - gb, wire, STEEL, rep=(n, 0, 0, d / (n + 1)))
    k.box(w - wire, gb, d / (n + 1), wire, h - gb, wire, STEEL, rep=(n, 0, 0, d / (n + 1)))
    k.box(w * 0.08, gb, d * 0.08, w * 0.84, (h - gb) * 0.8, d * 0.84, "#9fb3c0", tint=True, opacity=0.9)
    return k.done()


def _drum(w, h, d):
    k = _Kit(w, h, d)
    k.box(0, 0, 0, w, h, d, "#2f6db5", cyl="y", tint=True)
    for y in (h / 3, 2 * h / 3):
        k.box(-0.3, y, -0.3, w + 0.6, 1.2, d + 0.6, DARK, cyl="y", opacity=0.7)
    k.box(w * 0.05, h - 0.4, d * 0.05, w * 0.9, 0.6, d * 0.9, "#b8c0c6", cyl="y")
    return k.done()


def _big_bag(w, h, d):
    k = _Kit(w, h, d)
    lh = min(15.0, h * 0.15)
    k.box(w * 0.02, 0, d * 0.02, w * 0.96, h - lh, d * 0.96, "#e8e2c8", tint=True)
    lw = min(6.0, w * 0.08)
    for x, z in ((w * 0.05, d * 0.05), (w * 0.95 - lw, d * 0.05), (w * 0.05, d * 0.95 - lw), (w * 0.95 - lw, d * 0.95 - lw)):
        k.box(x, h - lh, z, lw, lh, lw, "#d8cfa8")
    return k.done()


def _drawers(w, h, d):
    k = _Kit(w, h, d)
    t = max(0.8, min(2.0, w * 0.03))
    k.box(0, 0, 0, w, h, d, "#8f9aa3")
    n = max(2, min(8, round(h / 15)))
    fh = (h - t) / n
    k.box(t, t / 2, -0.4, w - 2 * t, fh - t, 0.5, "#4e79a7", tint=True, rep=(n, 0, fh, 0))
    k.box(w * 0.4, t / 2 + fh * 0.55, -0.9, w * 0.2, min(2.0, fh * 0.12), 0.6, DARK, rep=(n, 0, fh, 0))
    return k.done()


# estruturas: peças da armação (do piso) + peças de cada nível ("each_level": y a partir da base do nível)
BLUE, ORANGE = "#2f5fa7", "#e8772e"


def _rack(w, h, d):
    k = _Kit(w, h, d)
    p = min(9.0, w * 0.06, d * 0.2)
    for x in (0, w - p):  # dois quadros (esquerda e direita)
        k.box(x, 0, 0, p, h, p, BLUE).box(x, 0, d - p, p, h, p, BLUE)
        n = max(2, int(h // 80))
        k.box(x + p * 0.3, h / (n + 1), p, p * 0.4, 3, d - 2 * p, BLUE, rep=(n, 0, h / (n + 1), 0))  # travessas
    bh = 10.0
    k.box(0, -bh, 0, w, bh, 5, ORANGE, each_level=True).box(0, -bh, d - 5, w, bh, 5, ORANGE, each_level=True)
    return k.done()


def _shelving(w, h, d):
    k = _Kit(w, h, d)
    p = 4.0
    for x, z in ((0, 0), (w - p, 0), (0, d - p), (w - p, d - p)):
        k.box(x, 0, z, p, h, p, STEEL)
    k.box(0, -2.5, 0, w, 2.5, d, "#c4ccd2", each_level=True)
    k.box(0, h - 2.5, 0, w, 2.5, d, "#c4ccd2")  # tampo
    k.box(0, h * 0.25, d - 1, w, 1.5, 1, STEEL, rep=(3, 0, h * 0.25, 0))  # travessas no fundo
    return k.done()


def _cantilever(w, h, d):
    k = _Kit(w, h, d)
    c = min(15.0, w * 0.12)
    for x in (0, w - c):
        k.box(x, 0, d - c, c, h, c, "#3a6ea5")  # coluna no fundo
        k.box(x, 0, 0, c, 8, d, "#3a6ea5")  # pé
        k.box(x + c * 0.2, -6, 0, c * 0.6, 6, d - c, "#3a6ea5", each_level=True)  # braço
    return k.done()


def _flow(w, h, d):
    k = _Kit(w, h, d)
    p = 5.0
    for x, z in ((0, 0), (w - p, 0), (0, d - p), (w - p, d - p)):
        k.box(x, 0, z, p, h, p, STEEL)
    n = max(4, min(12, int(d // 12)))
    k.box(0, -3, 0, 2, 3, d, STEEL, each_level=True).box(w - 2, -3, 0, 2, 3, d, STEEL, each_level=True)
    k.box(2, -4, 2, w - 4, 4, 4, "#b8c0c6", cyl="x", each_level=True, rep=(n, 0, 0, (d - 6) / (n - 1)))  # roletes
    return k.done()


def _cold_room(w, h, d):
    k = _Kit(w, h, d)
    t = min(8.0, w * 0.06)
    panel = "#eef2f4"
    k.box(0, 0, 0, t, h, d, panel, opacity=0.35).box(w - t, 0, 0, t, h, d, panel, opacity=0.35)
    k.box(0, 0, d - t, w, h, t, panel, opacity=0.35)
    k.box(0, h - t, 0, w, t, d, panel, opacity=0.3)
    k.box(t, 0, 0, w * 0.45 - t, h - t, 2, "#dfe6ea", opacity=0.55, detail=True)  # porta
    k.box(w * 0.45 - 3, h * 0.45, -0.5, 2, 12, 1, DARK, detail=True)  # puxador
    strips = 8
    sw = (w * 0.55 - t) / strips
    k.box(w * 0.45, 0, 0, sw * 0.9, h - t, 0.5, "#a8d8f0", opacity=0.35, rep=(strips, sw, 0, 0), detail=True)  # cortina de tiras
    k.box(w * 0.2, h - t - 25, d - 32, w * 0.6, 25, 30, "#9aa5ad", detail=True)  # evaporador
    k.box(0, -2.5, 0, w, 2.5, d, "#b8c0c6", each_level=True)  # prateleiras
    return k.done()


LIBRARY_BINS: dict[str, tuple[str, Callable]] = {
    "pallet": (N_("Palete com carga (PBR/europeu)"), _pallet),
    "tote": (N_("Caixa plástica (monobloco)"), _tote),
    "shelf_bin": (N_("Bin de prateleira (frente aberta)"), _shelf_bin),
    "carton": (N_("Caixa de papelão"), _carton),
    "cage": (N_("Gaiola aramada"), _cage),
    "drum": (N_("Tambor / bombona"), _drum),
    "big_bag": (N_("Big bag"), _big_bag),
    "drawers": (N_("Gaveteiro"), _drawers),
}
LIBRARY_STRUCTS: dict[str, tuple[str, Callable]] = {
    "rack": (N_("Porta-palete (longarinas)"), _rack),
    "shelving": (N_("Estante de aço (prateleiras)"), _shelving),
    "cantilever": (N_("Cantilever (braços)"), _cantilever),
    "flow": (N_("Flow rack (roletes)"), _flow),
    "cold_room": (N_("Câmara fria"), _cold_room),
}


def library(kind: str) -> dict[str, tuple[str, Callable]]:
    return LIBRARY_STRUCTS if kind == "struct" else LIBRARY_BINS


def model_choices(kind: str) -> list[tuple[str, str]]:
    """Opções do campo Modelo 3D do cadastro."""
    return ([("", _("Automático (pelo tipo e pelo nome)")), (SIMPLE, _("Desenho simples"))]
            + [(k, _(lbl)) for k, (lbl, _f) in library(kind).items()] + [(CUSTOM, _("Código colado (gerado por IA)"))])


_BIN_WORDS = (("pallet", r"palete|pallet|pbr|chep"), ("cage", r"gaiola|cage|aramad"), ("drum", r"tambor|bombona|drum|barril"),
              ("big_bag", r"big ?bag|bag"), ("carton", r"papel[aã]o|carton|cardboard|caixa de papel"),
              ("drawers", r"gaveteiro|drawer"), ("shelf_bin", r"bin|gaveta|bandeja|frente aberta"),
              ("tote", r"caixa|pl[aá]stic|monobloco|tote|organizador|crate|box"))
_STRUCT_WORDS = (("cold_room", r"c[aâ]mara|fri[ao]|refriger|congel|cold"), ("flow", r"flow|din[aâ]mic|rolete"),
                 ("cantilever", r"cantilever|bra[cç]o"), ("rack", r"porta.?palete|pallet ?rack|seletiv|drive"),
                 ("shelving", r"estante|prateleira|shelv"))


def suggest(kind: str, *texts: str) -> Optional[str]:
    """Modelo da biblioteca pelo tipo e pelo nome (None = desenho simples). "Palete no chão" fica com o desenho
    próprio do 3D (paletes no piso, sem armação)."""
    from services.site_planner import is_floor

    words = _STRUCT_WORDS if kind == "struct" else _BIN_WORDS
    if kind == "struct" and is_floor(texts[0] if texts else ""):
        return None
    order = (list(texts[1:]) + list(texts[:1])) if kind == "struct" else list(texts)  # estrutura: (tipo, nome…)
    for t in order:  # o nome manda (ex.: "Câmara fria" do tipo Estante); depois o tipo
        for key, rx in words:
            if re.search(rx, (t or "").casefold()):
                return key
    return None


def look_of(kind: str, model: str, appearance, dims: tuple, *texts: str) -> Optional[dict]:
    """Peças normalizadas do item (None = desenho simples). dims = (largura, altura, profundidade) em cm."""
    w, h, d = (float(v or 0) for v in dims)
    if model == SIMPLE:
        return None
    if w <= 0 or h <= 0 or d <= 0:  # medidas ainda não preenchidas: as de exemplo (o código colado é em proporção)
        w, h, d = EXAMPLE_DIMS[kind if kind == "struct" else "bin"]
    if model == CUSTOM:
        look, _err = normalize(appearance, structure=kind == "struct") if appearance else (None, "")
        return look
    key = model if model in library(kind) else suggest(kind, *texts)
    if key is None:
        return None
    look, _err = normalize(library(kind)[key][1](w, h, d), structure=kind == "struct", clamp=True)
    if look is not None:
        look["model"] = key
    return look


def piece_colors(appearance) -> dict[str, str]:
    """Cor de cada peça da estrutura no código da IA: {código da peça: cor da 1ª parte dela}."""
    out: dict[str, str] = {}
    for p in (appearance or {}).get("parts") or []:
        if isinstance(p, dict):
            pc = str(p.get("piece") or "").strip().upper()
            col = str(p.get("color") or "").strip().lower()
            if pc and not pc.startswith("@") and _HEX.match(col):
                out.setdefault(pc, col)
    return out


def apply_piece_colors(look: Optional[dict], comp: list) -> Optional[dict]:
    """Cores das peças (composição do tipo: [(código, "module"|"level", cor ou None)]) nas partes do desenho: a parte
    marcada com o código da peça leva a cor dela; nos modelos da biblioteca, a armação leva a cor da 1ª peça por
    módulo com cor e o que se repete por nível, a da 1ª peça por nível. Estrutura: cores reais, sem tingir pela vista."""
    if look is None:
        return None
    by_code = {str(c).upper(): col for c, _per, col in comp if col}
    frame = next((col for _c, per, col in comp if col and per in ("side", "module")), None)
    level = next((col for _c, per, col in comp if col and per == "level"), None)
    parts = []
    for p in look["parts"]:
        q = {k: v for k, v in p.items() if k != "t"}
        pc = p.get("pc")
        col = by_code.get(pc) if pc else None
        if col is None and pc == "@FRAME":
            col = frame
        elif col is None and pc == "@LEVEL":
            col = level
        if col:
            q["c"] = col
        parts.append(q)
    return {**look, "parts": parts, "tint": False, "solid": True}


def kind_comp_colors(kind) -> list:
    return [(p.piece.code, p.per, p.color) for p in (kind.pieces if kind is not None else []) if p.piece is not None]


def bin_look(bt) -> Optional[dict]:
    return look_of("bin", bt.look_model or "", bt.appearance, (bt.width, bt.height, bt.length), bt.name, bt.code)


def struct_look(st, kind=None) -> Optional[dict]:
    """Aparência da estrutura: a do TIPO de estrutura (modelo da biblioteca ou código), nas medidas da estrutura."""
    src = kind if kind is not None else st
    look = look_of("struct", src.look_model or "", src.appearance, (st.width, st.height, st.depth),
                   kind.name if kind is not None else st.kind, st.name)
    return apply_piece_colors(look, kind_comp_colors(kind))


def enrich(db, data: dict) -> dict:
    """Payload do 3D: a aparência de cada estrutura e tipo de bin (pelo id do cadastro)."""
    from models import BinType, StructureType

    for s in data.get("structs") or []:
        st = db.get(StructureType, s.get("id")) if s.get("id") else None
        if st is not None and not s.get("floor"):
            from services.doors import door_of
            from services.structure_kinds import kind_of

            k = kind_of(db, st)
            s["look"] = struct_look(st, k)
            s["door"] = door_of(k)  # porta: arco no piso e as folhas, que abrem quando o separador para ali
        else:
            s["look"] = None
    for t in data.get("types") or []:
        bt = db.get(BinType, t.get("id")) if t.get("id") else None
        t["look"] = bin_look(bt) if bt is not None else None
    return data


def form_values(form, kind: str, errors, dims: Optional[tuple] = None) -> dict:
    """Campos Modelo 3D e Código do formulário do cadastro -> (look_model, appearance)."""
    model = str(form.get("look_model") or "").strip()
    code = str(form.get("look_code") or "").strip()
    valid = {k for k, _l in model_choices(kind)}
    if model not in valid:
        model = ""
    if code and model == "":
        model = CUSTOM  # colou o código e deixou no automático: vale o código
    appearance = None
    if model == CUSTOM:
        appearance, err, _notes = parse(code, dims, structure=kind == "struct")
        if err:
            errors.add("look_code", err)
    return {"look_model": model, "appearance": appearance}


def code_text(appearance) -> str:
    return json.dumps(appearance, ensure_ascii=False, indent=1) if appearance else ""


def prompt(kind: str) -> str:
    """Prompt para colar no chat de IA junto com a foto. {w}, {h}, {d} e {levels} são trocados no navegador pelas
    medidas do formulário."""
    base = _(
        "Descreva o objeto da foto anexada como um modelo 3D simples para um software de armazém. Responda SOMENTE com "
        "um JSON, sem texto antes ou depois, neste formato:\n"
        "{\"parts\": [{\"shape\": \"box\", \"x\": 0, \"y\": 0, \"z\": 0, \"w\": 1, \"h\": 0.1, \"d\": 1, \"color\": \"#8b5a2b\"}]}\n\n"
        "Regras:\n"
        "- O objeto ocupa uma caixa de {w} cm de largura (x, da esquerda para a direita, visto de frente), {h} cm de "
        "altura (y, de baixo para cima) e {d} cm de profundidade (z, da frente para o fundo).\n"
        "- x, y, z = canto da peça mais à esquerda, mais baixo e mais à frente; w, h, d = tamanho da peça. Tudo em "
        "fração (0 a 1) dessas medidas.\n"
        "- shape: \"box\" (caixa) ou \"cylinder\" (cilindro, com \"axis\": \"x\", \"y\" ou \"z\" = direção do eixo; "
        "padrão \"y\").\n"
        "- color: a cor real da peça, em hexadecimal. \"opacity\" (0.1 a 1) para peças transparentes ou translúcidas.\n"
        "- \"repeat\": {\"count\": n, \"dx\": 0.1, \"dy\": 0, \"dz\": 0} repete a peça n vezes, deslocando a cada cópia "
        "(use para ripas, grades, barras, gavetas).\n")
    if kind != "struct":
        base += _("- \"tint\": true nas peças do corpo principal ou da carga: o software pinta essas peças com a cor da "
                  "informação exibida (tipo, curva ABC, ocupação). Marque pelo menos uma.\n")
    if kind == "struct":
        base += _("- Cada parte do desenho pertence a uma peça da estrutura: informe \"piece\" com o código dela, entre "
                  "estas: {pieces}. Partes da mesma peça têm a mesma cor: use a cor real dessa peça na foto. Não use "
                  "\"tint\".\n")
        base += _(
            "- É uma estrutura de armazenagem com {levels} níveis. \"each_level\": true repete a peça em cada nível "
            "(longarinas, prateleiras, braços); nesse caso y é medido a partir da base do nível (ex.: longarina logo "
            "abaixo da base: \"y\": -0.04). Montantes, colunas e paredes não usam each_level. Não desenhe a carga: o "
            "software desenha as bins.\n")
    base += _("- Em cada peça, use exatamente as chaves x, y, z, w, h e d (não use position, size, center nem "
              "coordenadas a partir do centro).\n")
    base += _("- Use no máximo 50 peças, contando as repetições. Reproduza a forma e as cores reais; ignore etiquetas, "
              "textos e logotipos.")
    return base
