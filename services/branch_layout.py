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
from services.site_planner import is_floor

PALLET_H = 14.0  # altura do palete de madeira (cm): no "Palete no chão" as bins ficam em cima dele

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
    base: Optional[float] = None  # altura do 1º nível nesta estante (None = a da estrutura)


def scope_bays(db: Session, branch_id: int, warehouse_id: Optional[int]) -> dict[str, list[Bay]]:
    """Estantes (corredor -> [Bay]) do depósito, com as medidas da estrutura cadastrada quando houver."""
    from services.app_settings import canon

    # códigos equivalentes ("07 = 7" nas Configurações) são a mesma estante; o rótulo é o 1º código visto
    disp_a: dict[str, str] = {}
    pairs: dict[tuple, str] = {}
    for a, b in db.execute(scope_filter(select(StorageLocation.aisle, StorageLocation.bay).distinct(), branch_id, warehouse_id)):
        A, B = (a or "").upper(), (b or "").upper()
        disp_a.setdefault(canon(A), A)
        pairs.setdefault((canon(A), canon(B)), B)
    st = {}
    bases: dict[tuple, float] = {}
    stmt = select(BayStructure, StructureType).join(StructureType, StructureType.id == BayStructure.structure_type_id) \
        .where(BayStructure.branch_id == branch_id)
    stmt = stmt.where(BayStructure.warehouse_id.is_(None) if warehouse_id is None else BayStructure.warehouse_id == warehouse_id)
    for bs, t in db.execute(stmt):
        st[(canon(bs.aisle), canon(bs.bay))] = t
        if bs.first_level_cm is not None:
            bases[(canon(bs.aisle), canon(bs.bay))] = float(bs.first_level_cm)
    out: dict[str, list[Bay]] = {}
    for (ca, cb), b in pairs.items():
        t = st.get((ca, cb))
        a = disp_a[ca]
        out.setdefault(a, []).append(Bay(a, b, float(t.width) if t else 0.0, float(t.depth) if t else None,
                                         float(t.height) if t else None, t.levels if t else None, t,
                                         bases.get((ca, cb))))
    for a in out:
        out[a].sort(key=lambda x: natural_key(x.bay))
    return dict(sorted(out.items(), key=lambda kv: natural_key(kv[0])))


def bays_json(bays: dict[str, list[Bay]]) -> dict:
    from services.site_planner import is_floor

    return {a: [{"bay": b.bay, "w": b.w or None, "d": b.d, "p": parity(b.bay),
                 **({"f": 1} if b.struct is not None and is_floor(b.struct.kind) else {})} for b in bs]
            for a, bs in bays.items()}


def row_bays(row: dict, bays: dict[str, list[Bay]]) -> list[Bay]:
    from services.app_settings import canon

    side = row.get("side") or "all"
    ra = (row.get("aisle") or "").upper()
    have = bays.get(ra)
    if have is None:  # "07 = 7": o corredor da fileira pode estar escrito do outro jeito
        have = next((v for k, v in bays.items() if canon(k) == canon(ra)), [])
    if row.get("bays"):  # lista fixa (na ordem dada)
        by = {canon(b.bay): b for b in have}
        out = [by[canon(c)] for c in row["bays"] if canon(c) in by]
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
                     **({"both": True} if r.get("both") else {}),
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
    back_nodes: dict[tuple, Node] = field(default_factory=dict)  # paradas na face de trás (pega pelos dois lados)
    back_addrs: Optional[set] = None  # endereços com as bins no fundo (atendidos pela face de trás)
    warn: list[str] = field(default_factory=list)
    net: object = None  # rede de caminhada (services/walk_net.WalkNet)

    @property
    def ref(self) -> dict:
        return self.data["ref"]

    def ref_node(self) -> Node:
        return Node(key="REF", gx=self.ref["x"], gy=self.ref["y"], net=self.net)

    def node(self, aisle: str, bay: str, address: Optional[str] = None) -> Optional[Node]:
        """Parada da estante; com `address` e pega pelos dois lados, a da face de trás quando as bins do endereço
        ficam no fundo (ver mark_back)."""
        from services.app_settings import canon

        if not hasattr(self, "_cidx"):
            self._cidx = {(canon(a), canon(b)): n for (a, b), n in self.nodes.items()}
            self._bidx = {(canon(a), canon(b)): n for (a, b), n in self.back_nodes.items()}
        k = (canon(aisle or ""), canon(bay or ""))
        if address and self.back_addrs and address in self.back_addrs and k in self._bidx:
            return self._bidx[k]
        return self._cidx.get(k)


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
            # profundidade da estrutura cadastrada (alinhada à face do corredor); sem estrutura, a da fileira
            bd = min(b.d, r["depth"]) if b.d else r["depth"]
            off = (r["depth"] - bd) if r["face"] == "+" else 0.0
            if r["orient"] == "h":
                x, y, ww, dd = r["x"] + cur, r["y"] + off, w, bd
                along = x + w / 2
                gx, gy = along, (r["y"] + r["depth"] if r["face"] == "+" else r["y"])
            else:
                x, y, ww, dd = r["x"] + off, r["y"] + cur, bd, w
                along = y + w / 2
                gx, gy = (r["x"] + r["depth"] if r["face"] == "+" else r["x"]), along
            key = (b.aisle, b.bay)
            if key in g.placed:
                g.dup.append(f"{b.aisle}-{b.bay}")
            else:
                g.placed[key] = PlacedBay(b, ri, x, y, ww, dd, along, gx, gy)
            cur += w
        length = cur or r["bay_w"]
        rw, rd = (length, r["depth"]) if r["orient"] == "h" else (r["depth"], length)
        g.row_rects.append((r["x"], r["y"], rw, rd, ri))
        info.append([ri, r, *_edge(r, length), "front"])
    # pega pelos dois lados: a face de trás também atende um corredor — só se não houver vizinho encostado atrás
    for ri, r in enumerate(rows):
        if not r.get("both"):
            continue
        back = {**r, "face": "-" if r["face"] == "+" else "+", "pair": 0}
        length = info[ri][4] - info[ri][3]
        e, a0, a1 = _edge(back, length)
        blocked = False
        for x, y, w, h, rj in g.row_rects + [(o["x"], o["y"], o["w"], o["d"], -1) for o in data.get("obstacles") or []]:
            if rj == ri:
                continue
            if r["orient"] == "h":
                ov, near_, far_ = min(a1, x + w) - max(a0, x), y, y + h
            else:
                ov, near_, far_ = min(a1, y + h) - max(a0, y), x, x + w
            gap = (near_ - e) if back["face"] == "+" else (e - far_)
            if ov > 0 and -1.0 <= gap < MIN_PASSAGE:
                blocked = True
                break
        if blocked:
            g.warn.append(f"{r['aisle']} {SIDES.get(r['side'], '')}: pega pelos dois lados ignorada — há fileira ou obstáculo "
                          f"encostado atrás (menos de {MIN_PASSAGE / 100:g} m)")
            continue
        info.append([ri, back, e, a0, a1, "back"])
    # corredores: faces que se olham ficam no mesmo; as demais têm o seu (cada face de fileira é uma entrada)
    ent = [(k, r, e, a0, a1) for k, (_ri, r, e, a0, a1, _side) in enumerate(info)]
    parent = list(range(len(info)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pair_c: dict[int, list] = {}
    for ki, kj, gap, c, manual in _pairs(rows, ent):
        if info[ki][0] == info[kj][0]:
            continue  # as duas faces da mesma fileira
        g.pairs.append((info[ki][0], info[kj][0], gap, manual))
        pair_c.setdefault(ki, []).append((c, gap))
        pair_c.setdefault(kj, []).append((c, gap))
        ra, rb = find(ki), find(kj)
        if ra != rb:
            parent[rb] = ra
    groups: dict[int, list] = {}
    for k, (_ri, _r, _e, a0, a1, _side) in enumerate(info):
        groups.setdefault(find(k), []).append((k, a0, a1))
    ent_corr: dict[tuple, Corridor] = {}  # (fileira, "front"|"back") -> corredor
    for idx, members in enumerate(groups.values()):
        ivs = sorted((a0, a1) for _k, a0, a1 in members)
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
        pcs = [pc for k, *_ in members for pc in pair_c.get(k, [])]
        if pcs:
            c = sum(v for v, _g in pcs) / len(pcs)
            wdt = min(gp for _v, gp in pcs)
        else:  # face sozinha: o corredor fica a LONE_AISLE dela
            k0 = members[0][0]
            e, rf = info[k0][2], info[k0][1]
            c = e + (LONE_AISLE if rf["face"] == "+" else -LONE_AISLE)
            wdt = 2 * LONE_AISLE
        mrows = [info[k][0] for k, *_ in members]
        flow = next((rows[ri].get("flow") for ri in mrows if rows[ri].get("flow") in ("+", "-")), "")
        cor = Corridor(idx, rows[mrows[0]]["orient"], c, wdt, tuple(sorted(ends)), lo, hi,
                       {"+": 1, "-": -1}.get(flow, 0), mrows, [tuple(v) for v in covered])
        g.corridors.append(cor)
        for k, *_ in members:
            ent_corr[(info[k][0], info[k][5])] = cor
    if with_net:
        g.net = _net(g)

    def tg_of(cor):
        if cor.orient == "h":
            return lambda along, cross, w, d: (along, cross, w, d)
        return lambda along, cross, w, d: (cross, along, w, d)

    for key, p in g.placed.items():
        cor = ent_corr[(p.row, "front")]
        g.nodes[key] = Node(key=f"{key[0]}-{key[1]}", gx=p.gx, gy=p.gy, zone=cor.index, aisle=0, along=p.along,
                            cross=cor.center, ends=cor.ends, to_global=tg_of(cor), corridor=cor.index, net=g.net)
        bc = ent_corr.get((p.row, "back"))
        if bc is not None:  # pega pelos dois lados: parada também na face de trás (para as bins do fundo)
            r = rows[p.row]
            if r["orient"] == "h":
                bx, by = p.along, (r["y"] if r["face"] == "+" else r["y"] + r["depth"])
            else:
                bx, by = (r["x"] if r["face"] == "+" else r["x"] + r["depth"]), p.along
            g.back_nodes[key] = Node(key=f"{key[0]}-{key[1]} (fundo)", gx=bx, gy=by, zone=bc.index, aisle=0, along=p.along,
                                     cross=bc.center, ends=bc.ends, to_global=tg_of(bc), corridor=bc.index, net=g.net)
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
    out.append(f'<defs><pattern id="nkPalletB" width="{fs * 0.6:.0f}" height="{fs * 0.6:.0f}" patternUnits="userSpaceOnUse">'
               f'<rect width="{fs * 0.6:.0f}" height="{fs * 0.35:.0f}" fill="#b8864b" fill-opacity=".6"/></pattern></defs>')
    for p in g.placed.values():
        fl = p.bay.struct is not None and is_floor(p.bay.struct.kind)  # palete no chão: tábuas do palete
        out.append(f'<rect x="{p.x:.0f}" y="{p.y:.0f}" width="{p.w:.0f}" height="{p.d:.0f}" '
                   f'fill="{"url(#nkPalletB)" if fl else "#cfd8df"}" stroke="#7b8a95" '
                   f'stroke-width="{fs / 12:.1f}"><title>{_esc(p.bay.aisle)}-{_esc(p.bay.bay)}'
                   f'{" · palete no chão" if fl else ""}</title></rect>')
    rows = d["rows"]
    blocks = [rr[:4] for rr in g.row_rects] + [(o["x"], o["y"], o["w"], o["d"]) for o in d["obstacles"]]
    for x, y, w, h, ri in g.row_rects:
        r = rows[ri]
        out.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" fill="none" stroke="#24548c" stroke-width="{fs / 8:.1f}"/>')
        side = {"odd": " ímp.", "even": " pares", "all": ""}[r["side"]]
        others = [b_ for k, b_ in enumerate(blocks) if k != ri]
        tx, ty, anchor = label_spot((x, y, w, h), r["orient"], r["aisle"] + side, fs, others, L, W)
        out.append(f'<text x="{tx:.0f}" y="{ty:.0f}" font-size="{fs:.0f}" fill="#24548c" text-anchor="{anchor}" '
                   f'font-weight="600" stroke="#fbfcfd" stroke-width="{fs / 5:.1f}" paint-order="stroke">'
                   f'{_esc(r["aisle"])}{side}</text>')
    ref = d["ref"]
    out.append(f'<circle cx="{ref["x"]:.0f}" cy="{ref["y"]:.0f}" r="{fs * 0.6:.0f}" fill="#d62728"><title>Referência: '
               f'{_esc(ref["label"])}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def label_spot(rect: tuple, orient: str, text: str, fs: float, others: list, L: float, W: float) -> tuple:
    """Onde escrever o rótulo da fileira para ele ficar visível: tenta à esquerda e à direita (fileira horizontal)
    ou acima e abaixo (vertical), depois os outros lados, e fica com o 1º lugar que não cai sobre outra fileira ou
    obstáculo e fica dentro do imóvel (ou, se nenhum servir, o que menos sobrepõe). (x, y da linha de base, âncora)."""
    x, y, w, h = rect
    tw, th = fs * 0.62 * max(1, len(text)), fs
    g = fs * 0.3
    cands = [(x - g - tw, y + h / 2 - th / 2, "end"), (x + w + g, y + h / 2 - th / 2, "start"),
             (x + w / 2 - tw / 2, y - g - th, "middle"), (x + w / 2 - tw / 2, y + h + g, "middle")]
    if orient == "v":
        cands = [cands[2], cands[3], cands[0], cands[1]]
    best = None
    for bx, by, anchor in cands:
        over = sum(max(0.0, min(bx + tw, ox + ow) - max(bx, ox)) * max(0.0, min(by + th, oy + oh) - max(by, oy))
                   for ox, oy, ow, oh in others)
        out = 0.0 if (bx >= 0 and by >= 0 and bx + tw <= L and by + th <= W) else tw * th * 0.5
        score = over + out
        if best is None or score < best[0] - 1e-6:
            best = (score, bx, by, anchor)
        if score <= 0:
            break
    _s, bx, by, anchor = best
    ax = bx + tw if anchor == "end" else bx if anchor == "start" else bx + tw / 2
    return ax, by + th * 0.85, anchor


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


def level_id(code) -> object:
    """Identidade de um nível na estante: o número ("01" = "1") ou, se não for só número (ex.: "01D1", nível
    dedicado criado acima do 01), o código equivalente."""
    from services.app_settings import canon

    c = str(code or "").strip()
    return int(c) if c.isdigit() else canon(c)


def shelf_info(db: Session, branch_id: int, warehouse_id: Optional[int]) -> dict:
    """Níveis cadastrados das estantes (Capacidade dos níveis): por estante (códigos equivalentes), nível -> vão (cm
    ou None); e por id do nível -> código (para as bins enviadas a um nível extra)."""
    from models import ShelfLevel
    from services.app_settings import canon

    stl = select(ShelfLevel).where(ShelfLevel.branch_id == branch_id)
    stl = stl.where(ShelfLevel.warehouse_id.is_(None) if warehouse_id is None else ShelfLevel.warehouse_id == warehouse_id)
    levels: dict[tuple, dict] = {}
    by_id: dict[int, str] = {}
    for sl in db.scalars(stl):
        levels.setdefault((canon(sl.aisle or ""), canon(sl.bay or "")), {})[sl.level] = \
            float(sl.height_cm) if sl.height_cm else None
        by_id[sl.id] = sl.level
    return {"levels": levels, "by_id": by_id}


def level_stack(st, codes: list, shelf: dict, base: Optional[float] = None) -> dict:
    """Níveis de uma estante, de baixo para cima: os da estrutura (1..N), os dos endereços e os cadastrados
    (inclusive reservados e dedicados), sem repetir os equivalentes. O 1º nível começa na altura `base` (a da
    estante ou, sem ela, a da estrutura; 0 = no piso). Cada nível com a base (z) e a altura: o vão cadastrado no nível
    ou, sem ele, o vão da estrutura para aquele nível (vão por nível) ou a altura acima do 1º nível ÷ níveis (50 cm
    sem estrutura); a sobra da altura da estrutura (altura − 1º nível − soma dos vãos) vira a espessura das
    longarinas. Zona de ouro da pega: o meio do nível dentro da faixa da estrutura.
    {"levels": [{"n", "code", "z", "h", "gold"}], "index": {level_id: n}, "h": altura total}."""
    ids: dict = {}
    for c in [str(i) for i in range(1, (st.levels if st is not None else 0) + 1)] + [c for c in codes if c] + list(shelf):
        ids.setdefault(level_id(c), c)
    if not ids:
        ids[1] = "1"
    def phys(kv):  # ordem física (sempre numérica, qualquer que seja a ordenação de endereços configurada)
        k, c = kv
        if isinstance(k, int):
            return (0, float(k), "")
        m = re.fullmatch(r"(\d+)D(\d+)", str(c).upper())
        if m:  # nível dedicado "01D1": logo acima do 01
            return (0, int(m.group(1)) + int(m.group(2)) / 1000.0, "")
        return (1, 0.0, str(c))

    order = sorted(ids.items(), key=phys)
    vao = {level_id(c): v for c, v in shelf.items() if v}
    n = len(order)
    if base is None:
        base = float(st.first_level_cm or 0) if st is not None else 0.0
    if st is not None:
        H = float(st.height)
        pitch = max(1.0, H - base) / max(1, st.levels)
        if st.level_heights:  # vão por nível da estrutura (de baixo para cima) para os níveis sem vão cadastrado
            sv = st.vaos()
            for i, (k, _c) in enumerate(order):
                if not vao.get(k) and i < len(sv):
                    vao[k] = sv[i]
    else:
        pitch = DEFAULT_PITCH
        H = pitch * n
    beam = 0.0
    if st is not None and all(vao.get(k) for k, _c in order):
        beam = max(0.0, (H - base - sum(vao[k] for k, _c in order)) / n)
    gold = [float(st.golden_from), float(st.golden_to)] if st is not None and st.golden_from is not None \
        and st.golden_to is not None else None
    out, index, acc = [], {}, base
    for i, (k, c) in enumerate(order, start=1):
        h = vao.get(k) or pitch
        mid = acc + h / 2
        out.append({"n": i, "code": c, "z": acc, "h": h, "gold": bool(gold) and gold[0] <= mid <= gold[1]})
        index[k] = i
        acc += h + (beam if vao.get(k) else 0.0)
    return {"levels": out, "index": index, "h": max(H, acc), "has_gold": bool(gold)}


def layout_3d(db: Session, g: Geometry, branch_id: int, warehouse_id: Optional[int], abc: Optional[dict] = None) -> dict:
    """Arranjo 3D do depósito desenhado, no mesmo formato de services/site_3d.plan_3d (visualizador view3d.html):
    cada estante da planta com os níveis (da estrutura cadastrada ou dos endereços) e as bins cadastradas em cada
    endereço, lado a lado ao longo da frente, na ordem das posições."""
    from models import BinType, LocationBin, Product, StockItem
    from services.layout_service import unit_volume_l

    d = g.data
    abc = abc or {}
    L = StorageLocation
    locs = list(db.execute(scope_filter(select(L.id, L.aisle, L.bay, L.level, L.position, L.compartment, L.address,
                                               L.is_generic), branch_id, warehouse_id)))
    ids = [r[0] for r in locs]
    bins_of: dict[int, list] = {}
    sku_of: dict[int, tuple] = {}
    vol_of: dict[int, float] = {}
    names: dict[str, str] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for lid, tid, qty, sl_id in db.execute(select(LocationBin.location_id, LocationBin.bin_type_id, LocationBin.quantity,
                                                      LocationBin.shelf_level_id)
                                               .where(LocationBin.location_id.in_(chunk)).order_by(LocationBin.id)):
            bins_of.setdefault(lid, []).append((tid, int(qty or 1), sl_id))
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
    used_types = sorted({tid for bs in bins_of.values() for tid, _q, _s in bs if tid in types}, key=lambda t: types[t].code)
    tidx = {t: i for i, t in enumerate(used_types)}
    # zona de ouro da caminhada: as 20% estantes mais perto da referência
    ref = g.ref_node()
    walks = sorted(dist(ref, n) for n in g.nodes.values())
    wlim = walks[max(0, int(math.ceil(len(walks) * 0.2)) - 1)] if walks else 0.0

    structs: dict[int, dict] = {}
    out_mods, out_bins, mod_idx = [], [], {}
    fills_by_type: dict[int, list] = {}
    from services.app_settings import canon

    by_bay: dict[tuple, list] = {}
    for r in locs:
        by_bay.setdefault((canon(r.aisle or ""), canon(r.bay or "")), []).append(r)
    sh = shelf_info(db, branch_id, warehouse_id)
    back: set = set()
    for key, p in sorted(g.placed.items(), key=lambda kv: (natural_key(kv[0][0]), natural_key(kv[0][1]))):
        b = p.bay
        ckey = (canon(key[0]), canon(key[1]))
        rows_here = by_bay.get(ckey, [])
        st = b.struct
        if st is not None:
            sid = st.id
            if sid not in structs:
                structs[sid] = {"id": sid, "code": st.code, "name": st.name, "qty": 0, "kind": st.kind or "",
                                "floor": is_floor(st.kind), "color": STRUCT_PALETTE[len(structs) % len(STRUCT_PALETTE)],
                                "golden": [float(st.golden_from), float(st.golden_to)]
                                if st.golden_from is not None and st.golden_to is not None else None, "golden_levels": []}
        else:
            sid = 0
            if 0 not in structs:
                structs[0] = {"id": 0, "code": "Sem estrutura", "name": "Estante sem estrutura cadastrada", "qty": 0,
                              "color": "#8a96a0", "golden": None, "golden_levels": []}
        structs[sid]["qty"] += 1
        lv = level_stack(st, [r.level for r in rows_here], sh["levels"].get(ckey, {}), b.base)
        if structs[sid]["golden"]:
            structs[sid]["golden_levels"] = sorted({*structs[sid]["golden_levels"],
                                                    *[x["n"] for x in lv["levels"] if x["gold"]]})
        mi = len(out_mods)
        mod_idx[key] = mi
        node = g.nodes.get(key)
        wgold = node is not None and dist(ref, node) <= wlim + 1e-6
        row = d["rows"][p.row]
        front_x = row["orient"] == "h"
        out_mods.append({"x": round(p.x, 1), "y": round(p.y, 1), "w": round(p.w, 1), "d": round(p.d, 1),
                         "h": round(lv["h"], 1), "s": sid, "code": f"{b.aisle}-{b.bay}", "fx": front_x, "cap": False,
                         "levels": [{"n": x["n"], "z": round(x["z"], 1)} for x in lv["levels"]]})
        # bins de cada nível: as do próprio nível do endereço, as enviadas a um nível extra (ou excedente) e, por
        # último, as de endereços genéricos da estante (sem nível), que preenchem a estante de baixo para cima
        per_level: dict[int, list] = {x["n"]: [] for x in lv["levels"]}
        loose = []
        for r in sorted(rows_here, key=lambda r: (natural_key(r.level or ""), natural_key(r.position or ""),
                                                   natural_key(r.compartment or ""))):
            for tid, q, sl_id in bins_of.get(r.id, []):
                tgt = sh["by_id"].get(sl_id) if sl_id else None
                # endereço genérico: não usa o próprio nível (sem nível escolhido, preenche a estante)
                n = lv["index"].get(level_id(tgt)) if tgt is not None else (lv["index"].get(level_id(r.level))
                                                                             if r.level and not r.is_generic else None)
                (per_level[n] if n in per_level else loose).append((r, tid, q))
        front = p.w if front_x else p.d
        depth = p.d if front_x else p.w
        state = {x["n"]: [0.0, 0.0] for x in lv["levels"]}  # (ao longo da frente, profundidade) ocupados
        floor_kind = st is not None and is_floor(st.kind)  # palete no chão: bins em cima do palete
        both = bool(row.get("both")) and key in g.back_nodes  # pega pelos dois lados: as bins do fundo saem pelo outro corredor
        seen_addr: set = set()
        lv_of = {x["n"]: x for x in lv["levels"]}

        def put(n, r, tid, q, spill):
            t = types.get(tid)
            if t is None or t.width is None or t.height is None or t.length is None:
                return
            bw, bh, bd = float(t.width), float(t.height), float(t.length)
            if t.is_fraction and t.parent is not None and t.parent.width is not None:
                bw = float(t.parent.width) / max(1, t.grid[0])
            bl_ = bins_of.get(r.id, [])
            cap = sum((float(types[tt].volume or 0) * qq) for tt, qq, _s in bl_ if tt in types)
            fill = round(vol_of[r.id] / cap * 100, 1) if r.id in vol_of and cap else None
            sku = sku_of.get(r.id, ("", ""))[0]
            for _ in range(q):
                cur, across = state[n]
                if cur + bw > front + 1e-6 and cur > 0:
                    cur, across = 0.0, across + bd
                if across + bd > depth + 1e-6:
                    nxt = [m for m in sorted(state) if m > n]
                    if spill and nxt:  # endereço genérico: passa para o nível de cima
                        state[n] = [cur, across]
                        n = nxt[0]
                        cur, across = state[n]
                    else:
                        across = 0.0  # não cabe: sobrepõe (desenho aproximado)
                x_ = lv_of[n]
                if both and r.address not in seen_addr and across > 1e-6:
                    back.add(r.address)  # a 1ª bin do endereço ficou no fundo: atendido pela face de trás
                seen_addr.add(r.address)
                acr = (depth - across - bd) if row["face"] == "+" else across  # frente da bin no lado do corredor
                if front_x:
                    bx, by, bwx, bdy = p.x + cur, p.y + acr, bw, bd
                else:
                    bx, by, bwx, bdy = p.x + acr, p.y + cur, bd, bw
                out_bins.append([round(bx, 1), round(by, 1), round(x_["z"] + (PALLET_H if floor_kind else 2.0), 1),
                                 round(bwx, 1), round(bdy, 1),
                                 round(min(bh, x_["h"] - 4), 1), tidx[tid], fill, mi, r.address, sku, x_["gold"],
                                 abc.get(sku, ""), wgold])
                fills_by_type.setdefault(tid, []).append(fill)
                state[n] = [cur + bw, across]

        for n, items in per_level.items():
            for r, tid, q in items:
                put(n, r, tid, q, False)
        first = min(state) if state else None
        for r, tid, q in loose:
            if first is not None:
                put(first, r, tid, q, True)
    g.back_addrs = back
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
    blocks = [rr[:4] for rr in g.row_rects] + [(o["x"], o["y"], o["w"], o["d"]) for o in d["obstacles"]]
    for x, y, w, h_, ri in g.row_rects:
        r = d["rows"][ri]
        lab = r["aisle"] + {"odd": " ímp.", "even": " pares", "all": ""}[r["side"]]
        # o rótulo vai para um lado livre da fileira (não fica escondido atrás de outra fileira)
        tx, ty, anchor = label_spot((x, y, w, h_), r["orient"], lab, 60.0, [b_ for k, b_ in enumerate(blocks) if k != ri],
                                    d["L"], d["W"])
        cx = tx - 0.31 * 60 * len(lab) if anchor == "end" else tx + 0.31 * 60 * len(lab) if anchor == "start" else tx
        aisles.append({"label": lab, "x": cx, "y": ty - 25})
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


def address_golden(db: Session, branch_id: int, warehouse_id: Optional[int], loc_ids: Optional[list] = None) -> dict:
    """Zona de ouro da PEGA de cada endereço pelo nível onde as bins dele estão de fato (próprio nível, nível extra,
    excedente ou, no endereço genérico da estante, os níveis de baixo para cima): True/False pela maioria das bins;
    None quando a estrutura da estante não tem zona de ouro (ou o endereço não tem estante)."""
    from models import LocationBin
    from services.app_settings import canon

    L = StorageLocation
    stmt = scope_filter(select(L.id, L.aisle, L.bay, L.level, L.is_generic), branch_id, warehouse_id).where(L.bay != "")
    rows = list(db.execute(stmt))
    if loc_ids is not None:
        want = set(loc_ids)
        rows_q = [r for r in rows if r[0] in want]
    else:
        rows_q = rows
    bays = scope_bays(db, branch_id, warehouse_id)
    st_of = {(canon(b.aisle), canon(b.bay)): b.struct for bs in bays.values() for b in bs}
    base_of = {(canon(b.aisle), canon(b.bay)): b.base for bs in bays.values() for b in bs}
    sh = shelf_info(db, branch_id, warehouse_id)
    codes: dict[tuple, list] = {}
    for _i, a, bay, lv, _g in rows:
        codes.setdefault((canon(a), canon(bay)), []).append(lv)
    stacks: dict[tuple, dict] = {}
    bins: dict[int, list] = {}
    ids = [r[0] for r in rows_q]
    for i in range(0, len(ids), 900):
        for lid, q, sl_id, tid in db.execute(select(LocationBin.location_id, LocationBin.quantity, LocationBin.shelf_level_id,
                                                    LocationBin.bin_type_id)
                                             .where(LocationBin.location_id.in_(ids[i:i + 900]))):
            bins.setdefault(lid, []).append((int(q or 1), sl_id, tid))
    from models import BinType

    tdim = {t.id: t for t in db.scalars(select(BinType))}
    out = {}
    for lid, a, bay, lv, generic in rows_q:
        k = (canon(a), canon(bay))
        st = st_of.get(k)
        if st is None or st.golden_from is None or st.golden_to is None:
            out[lid] = None
            continue
        if k not in stacks:
            stacks[k] = level_stack(st, codes.get(k, []), sh["levels"].get(k, {}), base_of.get(k))
        stk = stacks[k]
        gold_of = {x["n"]: x["gold"] for x in stk["levels"]}
        yes = no = 0
        free = {x["n"]: 1.0 for x in stk["levels"]}  # fração livre de cada nível (endereço genérico da estante)
        for q, sl_id, tid in bins.get(lid, []) or [(1, None, None)]:
            code = sh["by_id"].get(sl_id) if sl_id else (None if generic else lv)
            if code:
                n = stk["index"].get(level_id(code))
                if n is not None:
                    yes, no = (yes + q, no) if gold_of.get(n) else (yes, no + q)
                continue
            # genérico (sem nível): as bins preenchem a estante de baixo para cima, como no 3D
            t = tdim.get(tid)
            per = 1
            if t is not None and t.width and t.length:
                per = max(1, int(float(st.width) // float(t.width)) * max(1, int(float(st.depth) // float(t.length))))
            left = q
            for n in sorted(free):
                if left <= 0:
                    break
                take = min(left, int(round(free[n] * per)))
                if take <= 0:
                    continue
                free[n] -= take / per
                left -= take
                yes, no = (yes + take, no) if gold_of.get(n) else (yes, no + take)
            if left > 0:
                no += left
        out[lid] = None if yes + no == 0 else yes >= no
    return out


def mark_back(db: Session, g: Geometry, branch_id: int, warehouse_id: Optional[int]) -> None:
    """Pega pelos dois lados: descobre quais endereços têm as bins no fundo da estante (pela mesma arrumação do 3D),
    para a parada ser na face de trás."""
    if g.back_nodes and g.back_addrs is None:
        layout_3d(db, g, branch_id, warehouse_id)
