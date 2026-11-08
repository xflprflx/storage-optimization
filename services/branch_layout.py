"""
Planta desenhada de um depósito existente (filial + armazém).

O cadastro só tem endereços (corredor, estante, nível, posição…); a planta diz ONDE cada estante fica. Ela é feita de
FILEIRAS: um retângulo com as estantes de um corredor/rua lado a lado, de um lado dos números (ímpares, pares ou
todos). Cada fileira guarda:
    aisle   corredor/rua dos endereços (ex.: "A")
    side    "odd" (ímpares), "even" (pares) ou "all" (todas as estantes do corredor)
    x, y    canto da fileira na planta (cm; X ao longo do comprimento, Y ao longo da largura, a partir de cima)
    orient  "h" (estantes lado a lado ao longo de X) ou "v" (ao longo de Y)
    rev     a numeração começa na outra ponta
    face    lado do corredor de acesso: "+" (Y/X maior) ou "-" (Y/X menor)
    depth   profundidade da fileira (cm); bay_w = frente de cada estante sem estrutura cadastrada (cm)
    id      identificador estável da fileira (para o vizinho de frente)
    pair    vizinho de frente: 0 = automático (a fileira mais perto que olha para esta), -1 = nenhum (fileira sozinha)
            ou o id da fileira escolhida
    flow    sentido do corredor: "" = dois sentidos (contra fluxo permitido), "+" = só no sentido crescente de X/Y,
            "-" = só no decrescente (mão única)
    bays    opcional: lista fixa das estantes da fileira, na ordem (plantas geradas do projeto de filial nova)
A frente de cada estante vem da estrutura cadastrada (Estruturas nas estantes) ou de bay_w.

Corredores de caminhada: duas fileiras que se olham (mesma orientação, faces opostas, até MAX_AISLE de vão; as mais
perto primeiro, sem uma fileira "atravessar" outra) formam um corredor com o eixo no meio; com o vizinho escolhido à
mão, vale a escolha (qualquer vão). Uma fileira sozinha tem o eixo a LONE_AISLE da face. As pontas do corredor (e as
passagens entre fileiras) são por onde se entra e sai. A distância é o menor caminho pela rede de caminhada
(services/walk_net), que contorna as fileiras e os obstáculos e respeita a mão única dos corredores.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BayStructure, BranchLayout, StorageLocation, StructureType
from services.addressing import scope_filter
from services.layout_service import natural_key
from services.route_sim import Node, dist

MAX_AISLE = 600.0  # vão máximo entre duas fileiras que se olham (cm)
LONE_AISLE = 100.0  # eixo do corredor de uma fileira sozinha, a partir da face (cm)
MIN_PASSAGE = 80.0  # vão mínimo entre fileiras para servir de passagem (cm)
END_MARGIN = 60.0  # a saída do corredor fica um pouco além da ponta das fileiras (no corredor transversal)
DEFAULT_BAY_W = 100.0
DEFAULT_DEPTH = 60.0
DEFAULT_PITCH = 50.0  # altura de cada nível quando a estante não tem estrutura (cm)
SIDES = {"odd": "ímpares", "even": "pares", "all": "todas"}


def _num(v, default: float, lo: float = 0.0, hi: float = 1e7) -> float:
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, x)) if math.isfinite(x) else default


def parity(bay: str) -> Optional[int]:
    d = re.sub(r"\D", "", bay or "")
    return int(d) % 2 if d else None


def get_layout(db: Session, branch_id: int, warehouse_id: Optional[int]) -> Optional[BranchLayout]:
    stmt = select(BranchLayout).where(BranchLayout.branch_id == branch_id)
    stmt = stmt.where(BranchLayout.warehouse_id.is_(None) if warehouse_id is None else BranchLayout.warehouse_id == warehouse_id)
    return db.scalar(stmt)


@dataclass
class Bay:
    aisle: str
    bay: str
    w: float
    d: Optional[float]
    h: Optional[float]
    levels: Optional[int]
    struct: Optional[StructureType] = None


def scope_bays(db: Session, branch_id: int, warehouse_id: Optional[int]) -> dict[str, list[Bay]]:
    """Estantes (corredor -> [Bay]) do depósito, com as medidas da estrutura cadastrada quando houver."""
    pairs = set()
    for a, b in db.execute(scope_filter(select(StorageLocation.aisle, StorageLocation.bay).distinct(), branch_id, warehouse_id)):
        pairs.add(((a or "").upper(), (b or "").upper()))
    st = {}
    stmt = select(BayStructure, StructureType).join(StructureType, StructureType.id == BayStructure.structure_type_id) \
        .where(BayStructure.branch_id == branch_id)
    stmt = stmt.where(BayStructure.warehouse_id.is_(None) if warehouse_id is None else BayStructure.warehouse_id == warehouse_id)
    for bs, t in db.execute(stmt):
        st[(bs.aisle.upper(), bs.bay.upper())] = t
    out: dict[str, list[Bay]] = {}
    for a, b in pairs:
        t = st.get((a, b))
        out.setdefault(a, []).append(Bay(a, b, float(t.width) if t else 0.0, float(t.depth) if t else None,
                                         float(t.height) if t else None, t.levels if t else None, t))
    for a in out:
        out[a].sort(key=lambda x: natural_key(x.bay))
    return dict(sorted(out.items(), key=lambda kv: natural_key(kv[0])))


def bays_json(bays: dict[str, list[Bay]]) -> dict:
    return {a: [{"bay": b.bay, "w": b.w or None, "d": b.d, "p": parity(b.bay)} for b in bs] for a, bs in bays.items()}


def row_bays(row: dict, bays: dict[str, list[Bay]]) -> list[Bay]:
    side = row.get("side") or "all"
    have = bays.get((row.get("aisle") or "").upper(), [])
    if row.get("bays"):  # lista fixa (na ordem dada)
        by = {b.bay: b for b in have}
        out = [by[c] for c in row["bays"] if c in by]
    else:
        out = [b for b in have if side == "all" or parity(b.bay) == (1 if side == "odd" else 0)]
    return out[::-1] if row.get("rev") else out


def parse_widths(text: str) -> dict[str, float]:
    """"A=3,5; C=2.8" -> {"A": 350.0, "C": 280.0} (metros -> cm): largura própria de alguns corredores."""
    out = {}
    for part in re.split(r"[;\n]+", text or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            w = _num(v, 0.0, 0, 50)
            if k.strip() and w > 0:
                out[k.strip().upper()] = round(w * 100, 1)
    return out


def auto_layout(bays: dict[str, list[Bay]], mode: str = "aisle", aisle_w: float = 300.0, front: float = 400.0,
                widths: Optional[dict] = None) -> dict:
    """Planta inicial: fileiras horizontais empilhadas, na ordem dos corredores, com a frente (expedição) à esquerda.
    mode "aisle": uma letra por corredor — ímpares de um lado e pares do outro, frente a frente;
    mode "row": uma letra por rua — as ruas em pares (A|B, C|D…) frente a frente.
    widths: largura própria (cm) de alguns corredores, pela letra (no modo "row", a de qualquer uma das duas ruas)."""
    aisle_w = _num(aisle_w, 300.0, 80, 2000)
    widths = {str(k).upper(): _num(v, aisle_w, 80, 5000) for k, v in (widths or {}).items()}
    front = _num(front, 400.0, 0, 10000)
    rows, y = [], 150.0

    def depth_of(bs):
        return max([b.d for b in bs if b.d] or [DEFAULT_DEPTH])

    def length_of(bs):
        return sum(b.w or DEFAULT_BAY_W for b in bs)

    pairs = []
    names = list(bays)
    if mode == "row":
        for i in range(0, len(names), 2):
            a = names[i]
            b = names[i + 1] if i + 1 < len(names) else None
            pairs.append(((a, "all"), (b, "all") if b else None))
    else:
        for a in names:
            odd = [x for x in bays[a] if parity(x.bay) == 1]
            even = [x for x in bays[a] if parity(x.bay) == 0]
            if odd and even:
                pairs.append(((a, "odd"), (a, "even")))
            else:
                pairs.append(((a, "all"), None))
    for top, bottom in pairs:
        r1 = {"aisle": top[0], "side": top[1]}
        d1 = depth_of(row_bays(r1, bays))
        rows.append({**r1, "x": front, "y": y, "orient": "h", "rev": False, "face": "+", "depth": d1, "bay_w": DEFAULT_BAY_W})
        y += d1 + (widths.get(top[0]) or (widths.get(bottom[0]) if bottom else None) or aisle_w)
        if bottom:
            r2 = {"aisle": bottom[0], "side": bottom[1]}
            d2 = depth_of(row_bays(r2, bays))
            rows.append({**r2, "x": front, "y": y, "orient": "h", "rev": False, "face": "-", "depth": d2,
                         "bay_w": DEFAULT_BAY_W})
            y += d2
        y += 60.0  # costas com costas / folga até a próxima fileira
    for i, r in enumerate(rows):
        r.update(id=i + 1, pair=0, flow="")
    length = max([front + length_of(row_bays(r, bays)) for r in rows] + [front + 500.0]) + 300.0
    width = y + 150.0
    return {"L": round(length), "W": round(width), "rows": rows, "obstacles": [],
            "ref": {"x": 100.0, "y": round(width / 2), "label": "expedição"}}


def clean(data: dict) -> dict:
    """Valida o JSON vindo do editor (números, limites e campos conhecidos)."""
    L = _num(data.get("L"), 3000, 100, 100000)
    W = _num(data.get("W"), 2000, 100, 100000)
    rows = []
    ids: set[int] = set()
    raw = [r for r in (data.get("rows") or []) if isinstance(r, dict) and str(r.get("aisle") or "").strip()]
    for r in raw:  # id estável (o vizinho de frente aponta para ele); repetido ou ausente ganha um novo
        try:
            rid = int(r.get("id"))
        except (TypeError, ValueError):
            rid = 0
        r["_id"] = rid if rid > 0 and rid not in ids else 0
        ids.add(r["_id"])
    nxt = max(ids | {0}) + 1
    for r in raw:
        if not r["_id"]:
            r["_id"], nxt = nxt, nxt + 1
    valid = {r["_id"] for r in raw}
    for r in raw:
        try:
            pair = int(r.get("pair") or 0)
        except (TypeError, ValueError):
            pair = 0
        pair = pair if pair == -1 or (pair in valid and pair != r["_id"]) else 0
        bl = [str(b).strip().upper()[:20] for b in (r.get("bays") or []) if str(b).strip()][:2000] \
            if isinstance(r.get("bays"), list) else []
        rows.append({"id": r["_id"], "pair": pair, "flow": r.get("flow") if r.get("flow") in ("+", "-") else "",
                     **({"bays": bl} if bl else {}),
                     "aisle": str(r["aisle"]).strip().upper()[:20],
                     "side": r.get("side") if r.get("side") in SIDES else "all",
                     "x": round(_num(r.get("x"), 0, -L, 2 * L), 1), "y": round(_num(r.get("y"), 0, -W, 2 * W), 1),
                     "orient": "v" if r.get("orient") == "v" else "h", "rev": bool(r.get("rev")),
                     "face": "-" if r.get("face") == "-" else "+",
                     "depth": round(_num(r.get("depth"), DEFAULT_DEPTH, 10, 2000), 1),
                     "bay_w": round(_num(r.get("bay_w"), DEFAULT_BAY_W, 10, 2000), 1)})
    obstacles = []
    for o in data.get("obstacles") or []:
        if isinstance(o, dict):
            obstacles.append({"x": round(_num(o.get("x"), 0, 0, L), 1), "y": round(_num(o.get("y"), 0, 0, W), 1),
                              "w": round(_num(o.get("w"), 100, 1, L), 1), "d": round(_num(o.get("d"), 100, 1, W), 1),
                              "name": str(o.get("name") or "Obstáculo")[:60]})
    ref = data.get("ref") or {}
    return {"L": L, "W": W, "rows": rows, "obstacles": obstacles,
            "ref": {"x": round(_num(ref.get("x"), 100, 0, L), 1), "y": round(_num(ref.get("y"), W / 2, 0, W), 1),
                    "label": str(ref.get("label") or "expedição")[:60]}}


@dataclass
class PlacedBay:
    bay: Bay
    row: int
    x: float
    y: float
    w: float
    d: float
    along: float  # centro ao longo do corredor
    gx: float  # ponto de parada (na face, no centro da estante)
    gy: float


@dataclass
class Corridor:
    index: int
    orient: str
    center: float
    width: float
    ends: tuple
    lo: float
    hi: float
    flow: int = 0  # 0 = dois sentidos; +1 / -1 = mão única (sentido crescente / decrescente de X ou Y)
    rows: list = field(default_factory=list)  # índices das fileiras do corredor
    covered: list = field(default_factory=list)  # trechos (ao longo) com fileiras


@dataclass
class Geometry:
    data: dict
    bays: dict[str, list[Bay]]
    placed: dict[tuple, PlacedBay] = field(default_factory=dict)  # (corredor, estante) -> posição
    corridors: list[Corridor] = field(default_factory=list)
    nodes: dict[tuple, Node] = field(default_factory=dict)
    row_rects: list[tuple] = field(default_factory=list)  # (x, y, w, d, row)
    unplaced: list[str] = field(default_factory=list)  # "A (ímpares)…" sem fileira
    dup: list[str] = field(default_factory=list)  # estantes em mais de uma fileira
    pairs: list[tuple] = field(default_factory=list)  # (fileira, fileira, vão em cm, manual?) frente a frente
    net: object = None  # rede de caminhada (services/walk_net.WalkNet)

    @property
    def ref(self) -> dict:
        return self.data["ref"]

    def ref_node(self) -> Node:
        return Node(key="REF", gx=self.ref["x"], gy=self.ref["y"], net=self.net)

    def node(self, aisle: str, bay: str) -> Optional[Node]:
        return self.nodes.get(((aisle or "").upper(), (bay or "").upper()))


def _edge(r: dict, length: float) -> tuple[float, float, float]:
    """(coordenada da face, início e fim ao longo) da fileira."""
    if r["orient"] == "h":
        e = r["y"] + r["depth"] if r["face"] == "+" else r["y"]
        return e, r["x"], r["x"] + length
    e = r["x"] + r["depth"] if r["face"] == "+" else r["x"]
    return e, r["y"], r["y"] + length


def _pairs(rows: list, info: list) -> list[tuple]:
    """Fileiras frente a frente: (i, j, vão, centro do corredor, manual?). Primeiro as escolhidas à mão; depois as mais
    perto, sem que uma fileira forme corredor com outra que está atrás de uma vizinha já aceita (o trecho em comum
    não pode repetir)."""
    cands = []
    for k, (ri, r, e, a0, a1) in enumerate(info):
        for rj, s_, e2, b0, b1 in info[k + 1:]:
            if s_["orient"] != r["orient"] or s_["face"] == r["face"]:
                continue
            gap = (e2 - e) if r["face"] == "+" else (e - e2)
            o0, o1 = max(a0, b0), min(a1, b1)
            manual = r.get("pair") == s_.get("id") or s_.get("pair") == r.get("id")
            if manual:
                if gap >= 0:
                    cands.append((0, gap, ri, rj, o0, o1, (e + e2) / 2))
                continue
            if r.get("pair") or s_.get("pair") or o1 - o0 <= 0 or not 0 <= gap <= MAX_AISLE:
                continue  # quem escolheu o vizinho (ou "nenhum") não entra no automático
            cands.append((1, gap, ri, rj, o0, o1, (e + e2) / 2))
    cands.sort(key=lambda c: (c[0], c[1]))
    taken: dict[int, list] = {}
    out = []
    for auto, gap, ri, rj, o0, o1, c in cands:
        hit = lambda r: any(min(o1, q1) - max(o0, q0) > 1e-6 for q0, q1 in taken.get(r, []))  # noqa: E731
        if auto and (hit(ri) or hit(rj)):
            continue
        taken.setdefault(ri, []).append((o0, o1))
        taken.setdefault(rj, []).append((o0, o1))
        out.append((ri, rj, gap, c, not auto))
    return out


def build(data: dict, bays: dict[str, list[Bay]], with_net: bool = False) -> Geometry:
    g = Geometry(data=data, bays=bays)
    rows = data.get("rows") or []
    info = []
    for ri, r in enumerate(rows):
        bs = row_bays(r, bays)
        cur = 0.0
        for b in bs:
            w = b.w or r["bay_w"]
            if r["orient"] == "h":
                x, y, ww, dd = r["x"] + cur, r["y"], w, r["depth"]
                along = x + w / 2
                gx, gy = along, (y + dd if r["face"] == "+" else y)
            else:
                x, y, ww, dd = r["x"], r["y"] + cur, r["depth"], w
                along = y + w / 2
                gx, gy = (x + ww if r["face"] == "+" else x), along
            key = (b.aisle, b.bay)
            if key in g.placed:
                g.dup.append(f"{b.aisle}-{b.bay}")
            else:
                g.placed[key] = PlacedBay(b, ri, x, y, ww, dd, along, gx, gy)
            cur += w
        length = cur or r["bay_w"]
        rw, rd = (length, r["depth"]) if r["orient"] == "h" else (r["depth"], length)
        g.row_rects.append((r["x"], r["y"], rw, rd, ri))
        info.append((ri, r, *_edge(r, length)))
    # corredores: fileiras frente a frente ficam no mesmo; as demais têm o seu
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pair_c: dict[int, list] = {}
    for ri, rj, gap, c, manual in _pairs(rows, info):
        g.pairs.append((ri, rj, gap, manual))
        pair_c.setdefault(ri, []).append((c, gap))
        pair_c.setdefault(rj, []).append((c, gap))
        ra, rb = find(ri), find(rj)
        if ra != rb:
            parent[rb] = ra
    groups: dict[int, list] = {}
    for ri, r, e, a0, a1 in info:
        groups.setdefault(find(ri), []).append((ri, a0, a1))
    row_corr: dict[int, Corridor] = {}
    for k, members in enumerate(groups.values()):
        ivs = sorted((a0, a1) for _ri, a0, a1 in members)
        lo, hi = ivs[0][0], max(a1 for _a0, a1 in ivs)
        ends, reach = {lo - END_MARGIN, hi + END_MARGIN}, ivs[0][1]
        covered = [[ivs[0][0], ivs[0][1]]]
        for a0, a1 in ivs[1:]:
            if a0 - reach >= MIN_PASSAGE:
                ends.add((a0 + reach) / 2)  # passagem entre fileiras
            if a0 > covered[-1][1]:
                covered.append([a0, a1])
            else:
                covered[-1][1] = max(covered[-1][1], a1)
            reach = max(reach, a1)
        pcs = [pc for ri, *_ in members for pc in pair_c.get(ri, [])]
        if pcs:
            c = sum(v for v, _g in pcs) / len(pcs)
            wdt = min(gp for _v, gp in pcs)
        else:  # fileira sozinha
            ri0 = members[0][0]
            e = info[ri0][2]
            c = e + (LONE_AISLE if rows[ri0]["face"] == "+" else -LONE_AISLE)
            wdt = 2 * LONE_AISLE
        flow = next((rows[ri].get("flow") for ri, *_ in members if rows[ri].get("flow") in ("+", "-")), "")
        cor = Corridor(k, rows[members[0][0]]["orient"], c, wdt, tuple(sorted(ends)), lo, hi,
                       {"+": 1, "-": -1}.get(flow, 0), [ri for ri, *_ in members], [tuple(v) for v in covered])
        g.corridors.append(cor)
        for ri, *_ in members:
            row_corr[ri] = cor
    if with_net:
        g.net = _net(g)
    for key, p in g.placed.items():
        cor = row_corr[p.row]
        if cor.orient == "h":
            tg = (lambda along, cross, w, d: (along, cross, w, d))
        else:
            tg = (lambda along, cross, w, d: (cross, along, w, d))
        g.nodes[key] = Node(key=f"{key[0]}-{key[1]}", gx=p.gx, gy=p.gy, zone=cor.index, aisle=0, along=p.along,
                            cross=cor.center, ends=cor.ends, to_global=tg, corridor=cor.index, net=g.net)
    # estantes sem fileira
    for a, bs in bays.items():
        miss = [b for b in bs if (a, b.bay) not in g.placed]
        if miss:
            odd = [b for b in miss if parity(b.bay) == 1]
            even = [b for b in miss if parity(b.bay) == 0]
            other = len(miss) - len(odd) - len(even)
            parts = [f"{len(odd)} ímpares" if odd else "", f"{len(even)} pares" if even else "",
                     f"{other} sem número" if other else ""]
            g.unplaced.append(f"{a} ({', '.join(p for p in parts if p)})")
    return g


_NET_CACHE: dict[str, object] = {}


def _net(g: Geometry):
    """Rede de caminhada da planta (guardada pela geometria: a mesma planta não é recalculada a cada tela)."""
    import json

    from services.walk_net import CorridorSpec, WalkNet

    d = g.data
    blocks = [tuple(round(v, 1) for v in r[:4]) for r in g.row_rects] + \
        [(o["x"], o["y"], o["w"], o["d"]) for o in d["obstacles"]]
    cors = [(c.index, c.orient, round(c.center, 1), round(c.width, 1), [round(e, 1) for e in c.ends], c.flow,
             [[round(a, 1), round(b, 1)] for a, b in c.covered]) for c in g.corridors]
    key = json.dumps([blocks, cors, d["ref"]["x"], d["ref"]["y"], d["L"], d["W"]])
    hit = _NET_CACHE.get(key)
    if hit is not None:
        return hit
    specs = {}
    for c in g.corridors:
        to_xy = (lambda e, cc=c.center: (e, cc)) if c.orient == "h" else (lambda e, cc=c.center: (cc, e))
        specs[c.index] = CorridorSpec(ends=c.ends, to_xy=to_xy, flow=c.flow, covered=c.covered,
                                      half_width=c.width / 2, orient=c.orient, center=c.center)
    net = WalkNet(blocks, specs, (d["ref"]["x"], d["ref"]["y"]), (d["L"], d["W"]))
    if len(_NET_CACHE) >= 8:
        _NET_CACHE.pop(next(iter(_NET_CACHE)))
    _NET_CACHE[key] = net
    return net


def geometry(db: Session, branch_id: int, warehouse_id: Optional[int]) -> Optional[Geometry]:
    lay = get_layout(db, branch_id, warehouse_id)
    if lay is None or not (lay.data or {}).get("rows"):
        return None
    return build(clean(lay.data), scope_bays(db, branch_id, warehouse_id), with_net=True)


def walk_m(g: Geometry, node: Node) -> float:
    return dist(g.ref_node(), node) / 100.0


def svg(g: Geometry, width_px: int = 1000) -> str:
    """Planta (vista de cima) em cm no viewBox: fileiras com as estantes, corredores, obstáculos e a referência.
    Quem desenha por cima (rotas) usa as mesmas coordenadas em cm."""
    d = g.data
    L, W = d["L"], d["W"]
    fs = max(L, W) / 70
    out = [f'<svg class="nk-floor" viewBox="0 0 {L:.0f} {W:.0f}" width="100%" style="max-width:{width_px}px;background:#fbfcfd" '
           f'xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">',
           f'<rect x="0" y="0" width="{L:.0f}" height="{W:.0f}" fill="none" stroke="#1f2d33" stroke-width="{fs / 6:.1f}"/>']
    for c in g.corridors:
        if c.orient == "h":
            x, y, w, h = c.lo, c.center - c.width / 2, c.hi - c.lo, c.width
        else:
            x, y, w, h = c.center - c.width / 2, c.lo, c.width, c.hi - c.lo
        out.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" fill="#e3e9ee"/>')
        out += flow_arrows(c, fs)
    for o in d["obstacles"]:
        out.append(f'<rect x="{o["x"]:.0f}" y="{o["y"]:.0f}" width="{o["w"]:.0f}" height="{o["d"]:.0f}" fill="#2b2f33" opacity=".85">'
                   f'<title>{_esc(o["name"])}</title></rect>')
    for p in g.placed.values():
        out.append(f'<rect x="{p.x:.0f}" y="{p.y:.0f}" width="{p.w:.0f}" height="{p.d:.0f}" fill="#cfd8df" stroke="#7b8a95" '
                   f'stroke-width="{fs / 12:.1f}"><title>{_esc(p.bay.aisle)}-{_esc(p.bay.bay)}</title></rect>')
    rows = d["rows"]
    for x, y, w, h, ri in g.row_rects:
        r = rows[ri]
        out.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" fill="none" stroke="#24548c" stroke-width="{fs / 8:.1f}"/>')
        tx, ty = (x - fs * 0.3, y + h / 2 + fs * 0.35) if r["orient"] == "h" else (x + w / 2, y - fs * 0.3)
        anchor = "end" if r["orient"] == "h" else "middle"
        side = {"odd": " ímp.", "even": " pares", "all": ""}[r["side"]]
        out.append(f'<text x="{tx:.0f}" y="{ty:.0f}" font-size="{fs:.0f}" fill="#24548c" text-anchor="{anchor}" '
                   f'font-weight="600">{_esc(r["aisle"])}{side}</text>')
    ref = d["ref"]
    out.append(f'<circle cx="{ref["x"]:.0f}" cy="{ref["y"]:.0f}" r="{fs * 0.6:.0f}" fill="#d62728"><title>Referência: '
               f'{_esc(ref["label"])}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def flow_arrows(c: Corridor, fs: float) -> list[str]:
    """Setas do sentido de um corredor de mão única (SVG, cm)."""
    if not c.flow:
        return []
    n = max(1, int((c.hi - c.lo) // max(300.0, fs * 8)))
    out, a = [], fs * 0.5
    for k in range(n):
        t = c.lo + (c.hi - c.lo) * (k + 0.5) / n
        tip, back = t + c.flow * a, t - c.flow * a
        if c.orient == "h":
            pts = [(tip, c.center), (back, c.center - a * 0.8), (back, c.center + a * 0.8)]
        else:
            pts = [(c.center, tip), (c.center - a * 0.8, back), (c.center + a * 0.8, back)]
        out.append('<polygon points="' + " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)
                   + '" fill="#2f6db5" opacity=".75"><title>Mão única</title></polygon>')
    return out


def _esc(t) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


TYPE_PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#edc948", "#76b7b2", "#ff9da7", "#9c755f", "#e15759",
                "#bab0ac", "#86bcb6", "#d37295"]
STRUCT_PALETTE = ["#24548c", "#2a9d8f", "#8e5ea2", "#c0392b", "#d68910", "#5d6d7e"]


def _lvl(v: str) -> int:
    d = re.sub(r"\D", "", v or "")
    return max(1, int(d)) if d else 1


def layout_3d(db: Session, g: Geometry, branch_id: int, warehouse_id: Optional[int], abc: Optional[dict] = None) -> dict:
    """Arranjo 3D do depósito desenhado, no mesmo formato de services/site_3d.plan_3d (visualizador view3d.html):
    cada estante da planta com os níveis (da estrutura cadastrada ou dos endereços) e as bins cadastradas em cada
    endereço, lado a lado ao longo da frente, na ordem das posições."""
    from models import BinType, LocationBin, Product, StockItem
    from services.layout_service import unit_volume_l

    d = g.data
    abc = abc or {}
    L = StorageLocation
    locs = list(db.execute(scope_filter(select(L.id, L.aisle, L.bay, L.level, L.position, L.compartment, L.address),
                                        branch_id, warehouse_id)))
    ids = [r[0] for r in locs]
    bins_of: dict[int, list] = {}
    sku_of: dict[int, tuple] = {}
    vol_of: dict[int, float] = {}
    names: dict[str, str] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for lid, tid, qty in db.execute(select(LocationBin.location_id, LocationBin.bin_type_id, LocationBin.quantity)
                                        .where(LocationBin.location_id.in_(chunk)).order_by(LocationBin.id)):
            bins_of.setdefault(lid, []).append((tid, int(qty or 1)))
        for lid, sku, name, q, h, w, ln in db.execute(
                select(StockItem.location_id, Product.sku, Product.name, StockItem.quantity, Product.height, Product.width,
                       Product.length).join(Product, Product.id == StockItem.product_id)
                .where(StockItem.location_id.in_(chunk), StockItem.quantity >= 0).order_by(Product.sku)):
            sku_of.setdefault(lid, (sku, name))
            names[sku] = name
            uv = unit_volume_l(h, w, ln)
            if uv is not None:
                vol_of[lid] = vol_of.get(lid, 0.0) + uv * float(q)
    types = {t.id: t for t in db.scalars(select(BinType))}
    used_types = sorted({tid for bs in bins_of.values() for tid, _q in bs if tid in types}, key=lambda t: types[t].code)
    tidx = {t: i for i, t in enumerate(used_types)}
    # zona de ouro da caminhada: as 20% estantes mais perto da referência
    ref = g.ref_node()
    walks = sorted(dist(ref, n) for n in g.nodes.values())
    wlim = walks[max(0, int(math.ceil(len(walks) * 0.2)) - 1)] if walks else 0.0

    structs: dict[int, dict] = {}
    out_mods, out_bins, mod_idx = [], [], {}
    fills_by_type: dict[int, list] = {}
    by_bay: dict[tuple, list] = {}
    for r in locs:
        by_bay.setdefault(((r.aisle or "").upper(), (r.bay or "").upper()), []).append(r)
    for key, p in sorted(g.placed.items(), key=lambda kv: (natural_key(kv[0][0]), natural_key(kv[0][1]))):
        b = p.bay
        rows_here = by_bay.get(key, [])
        nmax = max([_lvl(r.level) for r in rows_here] + [b.levels or 1])
        st = b.struct
        if st is not None:
            sid = st.id
            if sid not in structs:
                structs[sid] = {"id": sid, "code": st.code, "name": st.name, "qty": 0,
                                "color": STRUCT_PALETTE[len(structs) % len(STRUCT_PALETTE)],
                                "golden": [float(st.golden_from), float(st.golden_to)]
                                if st.golden_from is not None and st.golden_to is not None else None, "golden_levels": []}
            h = float(st.height)
            pitch = h / max(1, st.levels)
        else:
            sid = 0
            if 0 not in structs:
                structs[0] = {"id": 0, "code": "Sem estrutura", "name": "Estante sem estrutura cadastrada", "qty": 0,
                              "color": "#8a96a0", "golden": None, "golden_levels": []}
            pitch = DEFAULT_PITCH
            h = pitch * nmax
        structs[sid]["qty"] += 1
        gold = structs[sid]["golden"]

        def is_gold(n):
            if not gold:
                return False
            mid = (n - 1) * pitch + pitch / 2
            return gold[0] <= mid <= gold[1]

        if gold:
            structs[sid]["golden_levels"] = sorted({*structs[sid]["golden_levels"], *[n for n in range(1, nmax + 1) if is_gold(n)]})
        mi = len(out_mods)
        mod_idx[key] = mi
        node = g.nodes.get(key)
        wgold = node is not None and dist(ref, node) <= wlim + 1e-6
        row = d["rows"][p.row]
        front_x = row["orient"] == "h"
        out_mods.append({"x": round(p.x, 1), "y": round(p.y, 1), "w": round(p.w, 1), "d": round(p.d, 1), "h": round(max(h, pitch * nmax), 1),
                         "s": sid, "code": f"{b.aisle}-{b.bay}", "fx": front_x, "cap": False,
                         "levels": [{"n": n, "z": round((n - 1) * pitch, 1)} for n in range(1, nmax + 1)]})
        # bins por nível, na ordem das posições
        for n in range(1, nmax + 1):
            here = sorted((r for r in rows_here if _lvl(r.level) == n), key=lambda r: (natural_key(r.position or ""),
                                                                                       natural_key(r.compartment or "")))
            cur, across = 0.0, 0.0
            front = p.w if front_x else p.d
            depth = p.d if front_x else p.w
            for r in here:
                bl = bins_of.get(r.id, [])
                cap = sum((float(types[t].volume or 0) * q) for t, q in bl if t in types)
                fill = round(vol_of[r.id] / cap * 100, 1) if r.id in vol_of and cap else None
                sku = sku_of.get(r.id, ("", ""))[0]
                for tid, q in bl:
                    t = types.get(tid)
                    if t is None or t.width is None or t.height is None or t.length is None:
                        continue
                    bw, bh, bd = float(t.width), float(t.height), float(t.length)
                    if t.is_fraction and t.parent is not None and t.parent.width is not None:
                        bw = float(t.parent.width) / max(1, t.grid[0])
                    for _ in range(q):
                        if cur + bw > front + 1e-6 and cur > 0:
                            cur, across = 0.0, across + bd
                        if across + bd > depth + 1e-6:
                            across = 0.0  # não cabe: sobrepõe (desenho aproximado)
                        # frente da bin no lado do corredor
                        acr = (depth - across - bd) if row["face"] == "+" else across
                        if front_x:
                            bx, by, bwx, bdy = p.x + cur, p.y + acr, bw, bd
                        else:
                            bx, by, bwx, bdy = p.x + acr, p.y + cur, bd, bw
                        out_bins.append([round(bx, 1), round(by, 1), round((n - 1) * pitch + 2.0, 1), round(bwx, 1),
                                         round(bdy, 1), round(min(bh, pitch - 4), 1), tidx[tid], fill, mi, r.address, sku,
                                         is_gold(n), abc.get(sku, ""), wgold])
                        fills_by_type.setdefault(tid, []).append(fill)
                        cur += bw
    out_types = []
    for t in used_types:
        fs = [f for f in fills_by_type.get(t, []) if f is not None]
        out_types.append({"id": t, "code": types[t].code, "color": TYPE_PALETTE[tidx[t] % len(TYPE_PALETTE)],
                          "placed": len(fills_by_type.get(t, [])), "fill": sum(fs) / len(fs) if fs else None})
    allf = [b[7] for b in out_bins if b[7] is not None]
    areas = []
    for c in g.corridors:
        if c.orient == "h":
            areas.append({"kind": "aisle", "x": c.lo, "y": c.center - c.width / 2, "w": c.hi - c.lo, "d": c.width})
        else:
            areas.append({"kind": "aisle", "x": c.center - c.width / 2, "y": c.lo, "w": c.width, "d": c.hi - c.lo})
    aisles = []
    for x, y, w, h_, ri in g.row_rects:
        r = d["rows"][ri]
        lab = r["aisle"] + {"odd": " ímp.", "even": " pares", "all": ""}[r["side"]]
        aisles.append({"label": lab, "x": x - 60 if r["orient"] == "h" else x + w / 2,
                       "y": y + h_ / 2 if r["orient"] == "h" else y - 60})
    hmax = max([m["h"] for m in out_mods] + [300.0])
    return {"building": {"L": d["L"], "W": d["W"], "H": hmax + 60, "name": ""}, "areas": areas,
            "obstacles": [{**o, "h": min(300.0, hmax)} for o in d["obstacles"]], "sectors": [], "zones": [],
            "structs": list(structs.values()), "types": out_types, "modules": out_mods, "bins": out_bins,
            "aisles": aisles, "skus": names, "ref": d["ref"],
            "kpis": {"modules": len(out_mods), "bins": len(out_bins), "bin_fill": sum(allf) / len(allf) if allf else None,
                     "level_fill": None}}


def from_plan(plan) -> dict:
    """Planta da filial a partir do plano do projeto de filial nova (para o cadastro gerado pelo projeto): cada trecho
    contínuo de fileira vira uma fileira com a lista fixa das estantes, na ordem; as cabeceiras (giradas 90°) viram
    fileiras próprias; os obstáculos do imóvel entram como obstáculos (setores são áreas de trabalho, por onde se anda);
    a referência é a do plano."""
    from services.site_planner import ref_point

    fl = plan.floor
    rows = []

    def add(x, y, w, d, orient, face, depth, bay_w, aisle, bays):
        rows.append({"aisle": aisle, "side": "all", "bays": bays, "x": round(x, 1), "y": round(y, 1), "orient": orient,
                     "rev": False, "face": face, "depth": round(depth, 1), "bay_w": round(bay_w, 1), "pair": 0, "flow": ""})

    runs: dict[tuple, list] = {}
    caps: dict[tuple, list] = {}
    for m in plan.modules:
        if m.zone is None or not m.aisle:
            continue
        if m.rotated:
            caps.setdefault((m.zone.index, m.segment, round(m.x0, 1), m.aisle), []).append(m)
        else:
            runs.setdefault((m.zone.index, m.row.index, m.segment, m.aisle), []).append(m)
    for ms in runs.values():
        ms.sort(key=lambda m: m.x0)
        chunk = [ms[0]]
        for m in ms[1:] + [None]:
            if m is not None and abs(m.x0 - (chunk[-1].x0 + chunk[-1].struct.width)) < 1.0:
                chunk.append(m)
                continue
            zn, r = chunk[0].zone, chunk[0].row
            x0, x1 = chunk[0].x0, chunk[-1].x0 + chunk[-1].struct.width
            X, Y, _w, _d = zn.to_global(x0, r.y, x1 - x0, r.depth)
            add(X, Y, _w, _d, "v" if zn.rot else "h", "+" if r.side == "L" else "-", r.depth,
                chunk[0].struct.width, chunk[0].aisle, [c.bay for c in chunk])
            chunk = [m] if m is not None else []
    for ms in caps.values():
        ms.sort(key=lambda m: m.y0 or 0.0)
        chunk = [ms[0]]
        for m in ms[1:] + [None]:  # cabeceiras encostadas umas nas outras = uma fileira
            if m is not None and abs((m.y0 or 0.0) - ((chunk[-1].y0 or 0.0) + chunk[-1].struct.width)) < 1.0:
                chunk.append(m)
                continue
            zn, m0 = chunk[0].zone, chunk[0]
            seg = zn.segments[m0.segment] if m0.segment < len(zn.segments) else (zn.x_start, zn.x_end)
            width = sum(c.struct.width for c in chunk)
            X, Y, _w, _d = zn.to_global(m0.x0, m0.y0 or 0.0, m0.struct.depth, width)
            add(X, Y, _w, _d, "h" if zn.rot else "v", "-" if m0.x0 <= seg[0] + 1.0 else "+", m0.struct.depth,
                m0.struct.width, m0.aisle, [c.bay for c in chunk])
            chunk = [m] if m is not None else []
    rows.sort(key=lambda r: (natural_key(r["aisle"]), natural_key(r["bays"][0])))
    for i, r in enumerate(rows):
        r["id"] = i + 1
    obstacles = [{"x": round(o.x_m * 100, 1), "y": round(o.y_m * 100, 1), "w": round(o.length_m * 100, 1),
                  "d": round(o.width_m * 100, 1), "name": (o.name or "Obstáculo")[:60]}
                 for o in (getattr(fl, "obstacles", None) or []) if getattr(o, "kind", "obstacle") == "obstacle"]
    ref = (plan.slotting or {}).get("ref") or ref_point(plan, {})
    return {"L": round(fl.length_cm, 1), "W": round(fl.width_cm, 1), "rows": rows, "obstacles": obstacles,
            "ref": {"x": round(ref["x"], 1), "y": round(ref["y"], 1), "label": str(ref.get("label") or "expedição")[:60]}}
