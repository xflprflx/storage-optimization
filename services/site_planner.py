"""
Projeto de filial nova (greenfield): dimensiona o armazém do zero.

Entradas (sem banco de dados — ver `site_service` para a carga):
  - Imóvel: comprimento × largura × pé-direito útil, largura dos corredores, afastamento das paredes, faixas
    reservadas (docas/expedição) e corredores transversais;
  - Estruturas de armazenagem candidatas (módulos): frente, profundidade, altura, nº de níveis, vão útil, carga por
    nível e custo. Cada uma tem um modo: "auto" (o sistema escolhe a quantidade, opcionalmente entre um mínimo e um
    máximo), "fixed" (quantidade exata informada) ou "off";
  - Tipos de bin permitidos e a demanda (SKUs com dimensões e quantidade a guardar).

Etapas:
  1. Planta: monta as fileiras (estrutura simples encostada na parede + duplas costas com costas, separadas por
     corredores) na orientação que dá mais metros de fileira; corredores transversais dividem cada fileira em trechos;
  2. Bins por SKU: escolhe o tipo e a quantidade de bins de cada SKU (mesma regra da otimização por endereço),
     só entre os tipos que cabem em pelo menos uma estrutura ativa;
  3. Quantidade de estruturas (OR-Tools CP-SAT): quantos módulos de cada estrutura em cada trecho de fileira e
     quantos níveis de cada estrutura ficam com cada tipo de bin (um tipo por nível). Objetivo lexicográfico:
     1º guardar toda a demanda (falta penalizada), 2º o critério escolhido (menos módulos, menor custo ou menos
     metros de fileira), 3º desempate;
  4. Endereçamento: posiciona os módulos na planta, distribui os tipos pelos níveis (bins maiores embaixo) e os
     SKUs pelas posições, gerando endereços Corredor-Estante-Nível-Posição.
"""
from __future__ import annotations

import math
import string
import time
from dataclasses import dataclass, field
from typing import Optional

from services.bin_optimizer import BinSpec, Params, ProductUnit, _sku_options, _units, capacity

EPS = 1e-9
OPTIONS_COST = 5  # opções de bins por SKU que o solver compara no objetivo de custo total

SITE_CRITERIA = {  # critérios do projeto (ordem de prioridade definida pelo usuário)
    "modules": "Menos módulos de estrutura",
    "cost": "Menor custo total (estruturas + bins)",
    "area": "Menos metros de fileira (mais área livre)",
    "structures_cost": "Menor custo de estruturas",
    "bins_cost": "Menor custo de bins",
    "bins": "Menos bins",
    "levels": "Menos níveis ocupados (mais níveis livres)",
}
SITE_OBJECTIVES = SITE_CRITERIA  # o 1º critério é o objetivo
DEFAULT_TIEBREAK = {"modules": ["area"], "cost": ["modules"], "area": ["modules"], "structures_cost": ["modules"],
                    "bins_cost": ["modules"], "bins": ["modules"], "levels": ["modules"]}
BIN_CRITERIA = {"cost", "bins_cost", "bins"}  # critérios que dependem das bins escolhidas para cada SKU
CRIT_UNITS = {"cost": ("R$", 100), "structures_cost": ("R$", 100), "bins_cost": ("R$", 100), "area": ("m", 1000),
              "modules": ("módulos", 1), "bins": ("bins", 1), "levels": ("níveis", 1)}


def site_priority(objective: str, custom: Optional[list] = None) -> list[str]:
    """Ordem completa: objetivo, depois os desempates escolhidos (ou o padrão do objetivo) e, por último, os níveis
    ocupados (se ainda não estiverem na lista)."""
    first = objective if objective in SITE_CRITERIA else "modules"
    rest = [c for c in (custom or []) if c in SITE_CRITERIA and c != first]
    order = [first] + list(dict.fromkeys(rest or DEFAULT_TIEBREAK[first]))
    return order + ([] if "levels" in order else ["levels"])
STRUCT_MODES = {"auto": "Otimizar quantidade", "fixed": "Quantidade fixa", "off": "Não usar"}
ORIENTATIONS = {"auto": "Automática (a que comporta mais)", "length": "Fileiras ao longo do comprimento",
                "width": "Fileiras ao longo da largura"}
DEMAND_STATUS = {"ok": "Alocado", "sem_dados": "Faltam dimensões", "nao_cabe": "Não cabe em nenhuma bin/estrutura",
                 "falta": "Faltou espaço", "sem_qtd": "Sem quantidade"}


# --------------------------------------------------------------------------- #
# Entradas
# --------------------------------------------------------------------------- #
@dataclass
class Building:
    length_m: float
    width_m: float
    clear_height_m: float
    aisle_m: float = 3.0
    wall_gap_m: float = 0.3
    front_reserved_m: float = 0.0  # docas, recebimento e expedição numa cabeceira
    back_reserved_m: float = 0.0
    cross_aisles: int = 0
    cross_aisle_m: float = 2.0
    orientation: str = "auto"

    @property
    def area_m2(self) -> float:
        return self.length_m * self.width_m


@dataclass
class StructureSpec:
    id: int
    code: str
    name: str
    width: float  # frente (cm)
    depth: float  # profundidade (cm)
    height: float  # altura total (cm)
    levels: int
    level_h: float  # vão útil (cm)
    max_load: Optional[float] = None  # kg por nível
    cost: Optional[float] = None  # R$ por módulo
    mode: str = "auto"
    qty: int = 0  # modo "fixed"
    min_qty: int = 0  # modo "auto"
    max_qty: Optional[int] = None  # modo "auto" (None = sem limite além da planta)


@dataclass
class SiteParams:
    objective: str = "modules"
    bin_objective: str = "min_volume"
    fill_limit: float = 0.85
    rotation: str = "upright"
    single_type: bool = True
    enforce_weight: bool = True
    growth: float = 0.0
    level_fill: float = 1.0  # aproveitamento máximo da frente de cada nível
    top_gap_cm: float = 5.0  # folga entre o topo da bin e o nível de cima (pegar a bin)
    side_gap_cm: float = 0.0  # folga entre bins lado a lado
    allowed_types: Optional[set] = None
    time_limit: float = 15.0
    priority: Optional[list] = None  # desempates (2º, 3º, 4º critério); None = padrão do objetivo

    @property
    def order(self) -> list[str]:
        return site_priority(self.objective, self.priority)

    def as_params(self) -> Params:
        return Params(objective=self.bin_objective, allowed_types=self.allowed_types, fill_limit=self.fill_limit,
                      rotation=self.rotation, enforce_weight=self.enforce_weight, single_type=self.single_type,
                      mix_skus=False, growth=self.growth)


@dataclass
class Demand:
    sku: str
    name: str
    material: str
    unit: ProductUnit
    units: int = 0
    bins: Optional[dict] = None  # tipo -> qtd
    status: str = "ok"
    note: str = ""
    placed_bins: int = 0

    @property
    def status_label(self) -> str:
        return DEMAND_STATUS.get(self.status, self.status)

    @property
    def total_bins(self) -> int:
        return sum((self.bins or {}).values())


# --------------------------------------------------------------------------- #
# Planta
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    index: int
    y: float  # cm, posição na direção transversal às fileiras
    depth: float
    aisle: int  # índice do corredor que a fileira atende
    side: str  # "L" (antes do corredor) ou "R" (depois)


@dataclass
class Floor:
    along: str  # "length" | "width": direção das fileiras
    run_cm: float  # dimensão do imóvel ao longo das fileiras
    cross_cm: float
    depth: float
    rows: list[Row] = field(default_factory=list)
    aisles: list[tuple[float, float]] = field(default_factory=list)  # (y, largura)
    segments: list[tuple[float, float]] = field(default_factory=list)  # trechos (x0, x1) ao longo da fileira
    x_start: float = 0.0
    x_end: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def seg_len(self) -> float:
        return (self.segments[0][1] - self.segments[0][0]) if self.segments else 0.0

    @property
    def total_row_m(self) -> float:
        return len(self.rows) * len(self.segments) * self.seg_len / 100.0


def _letters(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


def _cross_layout(cross: float, d: float, a: float) -> Optional[list[str]]:
    """Melhor sequência de blocos na direção transversal: S (fileira simples na parede), D (dupla), A (corredor)."""
    best = None
    kmax = int(cross // (2 * d)) + 1 if d > 0 else 0
    for s_first in (1, 0):
        for s_last in (1, 0):
            for k in range(0, kmax + 1):
                blocks = ["S"] * s_first + ["D"] * k + ["S"] * s_last
                if not blocks:
                    continue
                seq: list[str] = []
                if blocks[0] == "D":
                    seq.append("A")
                for i, b in enumerate(blocks):
                    if i:
                        seq.append("A")
                    seq.append(b)
                if blocks[-1] == "D" or len(blocks) == 1:
                    seq.append("A")
                total = sum({"S": d, "D": 2 * d, "A": a}[x] for x in seq)
                if total > cross + EPS:
                    continue
                rows = s_first + s_last + 2 * k
                key = (rows, -total)
                if best is None or key > best[0]:
                    best = (key, seq)
    return best[1] if best else None


def build_floor(b: Building, depth_cm: float, along: str) -> Floor:
    run = (b.length_m if along == "length" else b.width_m) * 100.0
    cross = (b.width_m if along == "length" else b.length_m) * 100.0
    fl = Floor(along=along, run_cm=run, cross_cm=cross, depth=depth_cm)
    gap, a = b.wall_gap_m * 100.0, b.aisle_m * 100.0
    seq = _cross_layout(cross - 2 * gap, depth_cm, a) if depth_cm > 0 else None
    if not seq:
        fl.notes.append("O imóvel não comporta nenhuma fileira com corredor nessa orientação.")
        return fl
    y = gap
    aisle_idx = -1
    pending_l: list[tuple[float]] = []  # fileiras que esperam o próximo corredor
    for i, el in enumerate(seq):
        if el == "A":
            aisle_idx += 1
            fl.aisles.append((y, a))
            for (yy,) in pending_l:
                fl.rows.append(Row(len(fl.rows), yy, depth_cm, aisle_idx, "L"))
            pending_l = []
            y += a
        elif el == "S":
            prev_is_aisle = i > 0 and seq[i - 1] == "A"
            if prev_is_aisle:
                fl.rows.append(Row(len(fl.rows), y, depth_cm, aisle_idx, "R"))
            else:
                pending_l.append((y,))
            y += depth_cm
        else:  # D: frente para o corredor anterior, fundo para o próximo
            fl.rows.append(Row(len(fl.rows), y, depth_cm, aisle_idx, "R"))
            pending_l.append((y + depth_cm,))
            y += 2 * depth_cm
    fl.rows.sort(key=lambda r: r.y)
    for i, r in enumerate(fl.rows):
        r.index = i
    x0 = gap + b.front_reserved_m * 100.0
    x1 = run - gap - b.back_reserved_m * 100.0
    n = max(0, int(b.cross_aisles)) + 1
    ca = b.cross_aisle_m * 100.0
    seg = (x1 - x0 - (n - 1) * ca) / n
    fl.x_start, fl.x_end = x0, x1
    if seg <= 0:
        fl.notes.append("Faixas reservadas e corredores transversais ocupam todo o comprimento das fileiras.")
        fl.rows = []
        return fl
    fl.segments = [(x0 + i * (seg + ca), x0 + i * (seg + ca) + seg) for i in range(n)]
    return fl


def choose_floor(b: Building, structs: list[StructureSpec]) -> Floor:
    depth = max((s.depth for s in structs), default=0.0)
    options = ["length", "width"] if b.orientation == "auto" else [b.orientation]
    floors = [build_floor(b, depth, o) for o in options]
    narrow = min((s.width for s in structs), default=1.0) or 1.0
    best = max(floors, key=lambda f: (len(f.rows) * len(f.segments) * math.floor(f.seg_len / narrow + EPS), f.along == "length"))
    if len({s.depth for s in structs}) > 1:
        best.notes.append(f"Estruturas com profundidades diferentes: as fileiras foram dimensionadas pela maior "
                          f"({depth:g} cm).")
    return best


# --------------------------------------------------------------------------- #
# Capacidade de um nível
# --------------------------------------------------------------------------- #
def level_capacity(t: BinSpec, s: StructureSpec, prm: SiteParams, parent: Optional[tuple] = None) -> int:
    """Bins do tipo `t` que cabem em UM nível da estrutura `s` (frente × profundidade). 0 = não cabe.
    Compartimentos (parent = (bin pai, nº por pai)) contam pela bin pai."""
    if parent:
        pspec, per = parent
        return level_capacity(pspec, s, prm) * per
    if not t.has_dims:
        return 0
    if t.h + prm.top_gap_cm > s.level_h + EPS or t.l > s.depth + EPS or t.w > s.width + EPS:
        return 0
    usable = s.width * prm.level_fill
    per_row = int(math.floor((usable + prm.side_gap_cm) / (t.w + prm.side_gap_cm) + EPS))
    deep = int(math.floor(s.depth / t.l + EPS))
    return max(0, per_row * deep)


# --------------------------------------------------------------------------- #
# Saída
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    address: str
    aisle: str
    bay: str
    level: str
    position: str
    struct_code: str
    type_id: int
    bins: int
    sku: str
    name: str
    units: int
    weight: float = 0.0


@dataclass
class LevelPlaced:
    n: int
    type_id: Optional[int] = None
    cap: int = 0
    used: int = 0
    load: float = 0.0
    positions: list[Position] = field(default_factory=list)


@dataclass
class ModulePlaced:
    struct: StructureSpec
    row: Row
    x0: float
    aisle: str = ""
    bay: str = ""
    levels: list[LevelPlaced] = field(default_factory=list)


@dataclass
class StructLine:
    struct: StructureSpec
    qty: int = 0
    levels_by_type: dict = field(default_factory=dict)  # tipo -> nº de níveis
    floor_max: int = 0  # quantos módulos dessa estrutura caberiam sozinhos na planta

    @property
    def cost(self) -> Optional[float]:
        return self.struct.cost * self.qty if self.struct.cost is not None else None

    @property
    def front_m(self) -> float:
        return self.struct.width * self.qty / 100.0

    @property
    def footprint_m2(self) -> float:
        return self.struct.width * self.struct.depth * self.qty / 10_000.0

    @property
    def levels(self) -> int:
        return self.struct.levels * self.qty


@dataclass
class TypeLine:
    spec: BinSpec
    needed: int = 0
    capacity: int = 0
    placed: int = 0
    parent_code: str = ""
    parents: int = 0  # bins inteiras (pai) a comprar quando o tipo é compartimento
    parent_cost: Optional[float] = None

    @property
    def unit_cost(self) -> Optional[float]:
        return self.spec.cost

    @property
    def cost(self) -> Optional[float]:
        """Compra das bins necessárias (compartimento: as bins pai inteiras)."""
        if self.parent_code:
            return self.parents * self.parent_cost if self.parent_cost is not None else None
        return self.needed * self.spec.cost if self.spec.cost is not None else None

    @property
    def shortage(self) -> int:
        return max(0, self.needed - self.placed)

    @property
    def fill(self) -> Optional[float]:
        return self.placed / self.capacity * 100 if self.capacity else None


@dataclass
class SitePlan:
    building: Building
    floor: Floor
    structs: list[StructLine] = field(default_factory=list)
    types: list[TypeLine] = field(default_factory=list)
    demands: list[Demand] = field(default_factory=list)
    modules: list[ModulePlaced] = field(default_factory=list)
    solver: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def positions(self) -> list[Position]:
        return [p for m in self.modules for lv in m.levels for p in lv.positions]

    @property
    def total_modules(self) -> int:
        return sum(s.qty for s in self.structs)

    @property
    def structures_cost(self) -> Optional[float]:
        costs = [s.cost for s in self.structs if s.qty]
        return sum(c for c in costs if c is not None) if costs and all(c is not None for c in costs) else None

    @property
    def bins_cost(self) -> Optional[float]:
        costs = [t.cost for t in self.types if t.needed]
        return sum(c for c in costs if c is not None) if costs and all(c is not None for c in costs) else None

    @property
    def total_cost(self) -> Optional[float]:
        """Estruturas + bins (só quando todos os itens usados têm custo informado)."""
        parts = [c for c in (self.structures_cost, self.bins_cost)]
        if self.total_modules and parts[0] is None or self.bins_needed and parts[1] is None:
            return None
        return sum(c or 0 for c in parts)

    @property
    def footprint_m2(self) -> float:
        return sum(s.footprint_m2 for s in self.structs)

    @property
    def front_m(self) -> float:
        return sum(s.front_m for s in self.structs)

    @property
    def bins_needed(self) -> int:
        return sum(t.needed for t in self.types)

    @property
    def bins_placed(self) -> int:
        return sum(t.placed for t in self.types)

    @property
    def bins_capacity(self) -> int:
        return sum(t.capacity for t in self.types)

    @property
    def levels_total(self) -> int:
        return sum(s.levels for s in self.structs)

    @property
    def levels_used(self) -> int:
        return sum(1 for m in self.modules for lv in m.levels if lv.used)

    @property
    def levels_assigned(self) -> int:
        return sum(1 for m in self.modules for lv in m.levels if lv.type_id is not None)

    @property
    def fill(self) -> Optional[float]:
        return self.bins_placed / self.bins_capacity * 100 if self.bins_capacity else None

    @property
    def row_use(self) -> Optional[float]:
        tot = self.floor.total_row_m
        return self.front_m / tot * 100 if tot else None

    @property
    def shortage(self) -> int:
        return sum(t.shortage for t in self.types)

    @property
    def by_status(self) -> dict:
        out: dict[str, int] = {}
        for d in self.demands:
            out[d.status] = out.get(d.status, 0) + 1
        return out


# --------------------------------------------------------------------------- #
# Planejamento
# --------------------------------------------------------------------------- #
def plan_site(b: Building, structs: list[StructureSpec], types: dict[int, BinSpec], demands: list[Demand],
              prm: SiteParams, parents: Optional[dict] = None) -> SitePlan:
    parents = parents or {}
    active = [s for s in structs if s.mode != "off"]
    plan = SitePlan(building=b, floor=Floor("length", 0, 0, 0))
    too_tall = [s for s in active if s.height > b.clear_height_m * 100.0 + EPS]
    for s in too_tall:
        plan.notes.append(f"Estrutura {s.code} ({s.height:g} cm) é mais alta que o pé-direito útil "
                          f"({b.clear_height_m * 100:g} cm): descartada.")
    active = [s for s in active if s not in too_tall]
    if not active:
        plan.errors.append("Nenhuma estrutura ativa que caiba no imóvel. Cadastre estruturas e marque ao menos uma "
                           "como \"Otimizar quantidade\" ou \"Quantidade fixa\".")
        plan.demands = demands
        return plan
    floor = choose_floor(b, active)
    plan.floor = floor
    plan.notes.extend(floor.notes)

    # ---- capacidade por (tipo, estrutura) ----
    allowed = [t for t in types.values() if prm.allowed_types is None or t.id in prm.allowed_types]
    caps = {(t.id, s.id): level_capacity(t, s, prm, parents.get(t.id)) for t in allowed for s in active}
    fitting = {t.id for t in allowed if any(caps[(t.id, s.id)] > 0 for s in active)}
    if not fitting:
        plan.errors.append("Nenhum tipo de bin permitido cabe nos níveis das estruturas ativas (confira altura/vão, "
                           "profundidade e frente).")

    # ---- bins por SKU ----
    bp = prm.as_params()
    choices: list[tuple[Demand, list[dict]]] = []
    for d in demands:
        d.units = _units(d.unit.qty, prm.growth)
        if d.units <= 0:
            d.status = "sem_qtd"
            continue
        if not d.unit.has_dims:
            d.status = "sem_dados"
            d.note = "SKU sem altura, largura ou comprimento."
            continue
        sku_caps = {t: capacity(d.unit, types[t], bp) for t in fitting}
        sku_caps = {t: c for t, c in sku_caps.items() if c > 0}
        if not sku_caps:
            d.status = "nao_cabe"
            d.note = "Nenhum tipo de bin permitido (que caiba nas estruturas) comporta uma unidade."
            continue
        opts = _sku_options(d.units, sku_caps, types, bp)
        d.bins = dict(opts[0]) if opts else None
        if not d.bins:
            d.status = "nao_cabe"
        elif BIN_CRITERIA & set(prm.order) and len(opts) > 1:
            # critério que depende das bins (custo, nº de bins): o solver escolhe entre as melhores opções de bins do
            # SKU (inclui a mais barata), pesando-as contra as estruturas que elas ocupam
            choices.append((d, [dict(o) for o in opts[:OPTIONS_COST]]))
    fixed_need: dict[int, int] = {}
    chosen = {id(d) for d, _ in choices}
    for d in demands:
        if id(d) in chosen:
            continue
        for t, n in (d.bins or {}).items():
            fixed_need[t] = fixed_need.get(t, 0) + n

    # ---- quantidade de estruturas (e, no custo total, as bins de cada SKU) ----
    sol = _solve(floor, active, fixed_need, caps, prm, types, choices)
    for (d, opts), k in zip(choices, sol.get("picks") or []):
        d.bins = opts[k]
    need: dict[int, int] = {}
    for d in demands:
        for t, n in (d.bins or {}).items():
            need[t] = need.get(t, 0) + n
    plan.solver = sol["info"]
    if sol.get("error"):
        plan.errors.append(sol["error"])
    qty = sol.get("qty", {})
    z = sol.get("z", {})
    seg_mods = sol.get("seg_mods", [])

    # ---- linhas de resumo ----
    narrow = {s.id: math.floor(floor.seg_len / s.width + EPS) * len(floor.rows) * len(floor.segments)
              if s.width > 0 else 0 for s in active}
    for s in structs:
        line = StructLine(struct=s, qty=qty.get(s.id, 0), floor_max=narrow.get(s.id, 0))
        line.levels_by_type = {t: n for (t, sid), n in z.items() if sid == s.id and n > 0}
        plan.structs.append(line)

    # ---- posicionamento e endereçamento ----
    _place(plan, floor, active, seg_mods, z, caps, types, demands, prm)

    by_t = {t: TypeLine(spec=types[t]) for t in sorted(set(need) | {t for (t, _), n in z.items() if n > 0},
                                                         key=lambda k: types[k].code)}
    for t, line in by_t.items():
        line.needed = need.get(t, 0)
        line.capacity = sum(lv.cap for m in plan.modules for lv in m.levels if lv.type_id == t)
        line.placed = sum(lv.used for m in plan.modules for lv in m.levels if lv.type_id == t)
        if t in parents:
            pspec, per = parents[t]
            line.parent_code = pspec.code
            line.parents = -(-line.needed // per)
            line.parent_cost = pspec.cost
    plan.types = list(by_t.values())
    plan.demands = demands
    if plan.shortage:
        plan.notes.append(f"{plan.shortage} bin(s) ficaram sem lugar: aumente as estruturas (ou libere o modo "
                          "\"Otimizar quantidade\"), o imóvel ou reduza a demanda.")
    return plan


def _solve(floor: Floor, active: list[StructureSpec], need: dict[int, int], caps: dict, prm: SiteParams,
           types: Optional[dict] = None, choices: Optional[list] = None) -> dict:
    """Modelo CP-SAT: módulos por trecho de fileira e níveis por tipo de bin. `need` = bins já decididas por tipo;
    `choices` = SKUs com várias opções de bins (objetivo de custo total): o modelo escolhe uma opção por SKU."""
    types, choices = types or {}, choices or []
    info = {"status": "", "time": 0.0, "stages": []}
    segs = [(r.index, k) for r in floor.rows for k in range(len(floor.segments))]
    seg_len = int(math.floor(floor.seg_len * 10 + EPS))  # mm
    if not segs or seg_len <= 0:
        fixed = [s for s in active if s.mode == "fixed" and s.qty > 0]
        return {"info": {**info, "status": "inviável"},
                "error": "A planta não tem fileiras disponíveis." + (" As estruturas com quantidade fixa não cabem."
                                                                    if fixed else "")}
    try:
        from ortools.sat.python import cp_model
    except Exception:  # pragma: no cover
        return {"info": {**info, "status": "indisponível"},
                "error": "OR-Tools não está instalado (pip install ortools)."}

    t0 = time.monotonic()
    m = cp_model.CpModel()
    w = {s.id: max(1, int(round(s.width * 10))) for s in active}
    per_seg = {s.id: seg_len // w[s.id] for s in active}
    n = {(s.id, g): m.NewIntVar(0, per_seg[s.id], f"n_{s.id}_{g}") for s in active for g in range(len(segs))}
    x = {}
    for s in active:
        floor_cap = per_seg[s.id] * len(segs)
        hi = floor_cap if s.mode != "auto" or s.max_qty is None else min(floor_cap, s.max_qty)
        x[s.id] = m.NewIntVar(0, max(0, hi), f"x_{s.id}")
        m.Add(x[s.id] == sum(n[(s.id, g)] for g in range(len(segs))))
        if s.mode == "fixed":
            if s.qty > floor_cap:
                return {"info": {**info, "status": "inviável"},
                        "error": f"Quantidade fixa de {s.code} ({s.qty}) não cabe na planta (máximo {floor_cap})."}
            m.Add(x[s.id] == s.qty)
        elif s.min_qty:
            if s.min_qty > hi:
                return {"info": {**info, "status": "inviável"},
                        "error": f"Mínimo de {s.code} ({s.min_qty}) maior que o que cabe ({hi})."}
            m.Add(x[s.id] >= s.min_qty)
    used = []
    for g in range(len(segs)):
        u = m.NewIntVar(0, seg_len, f"u_{g}")
        m.Add(u == sum(w[s.id] * n[(s.id, g)] for s in active))
        used.append(u)
    for g in range(len(segs) - 1):  # simetria: os primeiros trechos enchem primeiro
        m.Add(used[g] >= used[g + 1])
    # opções de bins por SKU (custo total): y[i][k] = 1 se o SKU i usa a opção k
    y = []
    for i, (_d, opts) in enumerate(choices):
        vs = [m.NewBoolVar(f"y_{i}_{k}") for k in range(len(opts))]
        m.AddExactlyOne(vs)
        y.append(vs)
    all_t = set(need) | {t for _d, opts in choices for o in opts for t in o}
    need_ub = {t: need.get(t, 0) + sum(max(o.get(t, 0) for o in opts) for _d, opts in choices) for t in all_t}
    need_ex = {t: need.get(t, 0) + sum(o.get(t, 0) * v for (_d, opts), vs in zip(choices, y)
                                         for o, v in zip(opts, vs) if o.get(t)) for t in all_t}
    z = {}
    for s in active:
        for t in all_t:
            if caps.get((t, s.id), 0) > 0:
                z[(t, s.id)] = m.NewIntVar(0, s.levels * per_seg[s.id] * len(segs), f"z_{t}_{s.id}")
        m.Add(sum(v for (t, sid), v in z.items() if sid == s.id) <= s.levels * x[s.id])
    short = {}
    for t in all_t:
        short[t] = m.NewIntVar(0, need_ub[t], f"short_{t}")
        m.Add(sum(caps[(t, sid)] * v for (tt, sid), v in z.items() if tt == t) + short[t] >= need_ex[t])

    cost_c = {s.id: int(round((s.cost or 0) * 100)) for s in active}
    total_mods = sum(x.values())
    front = sum(w[s.id] * x[s.id] for s in active)
    bin_c = {t: int(round(((types[t].cost if t in types else 0) or 0) * 100)) for t in all_t}
    st_cost = sum(cost_c[s.id] * x[s.id] for s in active)
    bins_cost = sum(bin_c[t] * need_ex[t] for t in all_t)
    levels_used = sum(z.values()) if z else 0
    exprs = {"modules": total_mods, "area": front, "cost": st_cost + bins_cost, "structures_cost": st_cost,
             "bins_cost": bins_cost, "bins": sum(need_ex[t] for t in all_t), "levels": levels_used}
    stages = [("short", sum(short.values()) if short else 0)] + [(c, exprs[c]) for c in prm.order]

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 8
    deadline = t0 + prm.time_limit
    all_optimal, ok = True, False
    sol: dict = {"qty": {}, "z": {}, "seg_mods": [], "picks": []}
    for i, (label, expr) in enumerate(stages):
        if isinstance(expr, int):  # critério que não depende de nenhuma decisão (ex.: bins já definidas)
            unit, div = CRIT_UNITS.get(label, ("bins", 1))
            info["stages"].append({"criterion": label, "label": SITE_CRITERIA.get(label, "Falta de bins (guardar toda a demanda)"),
                                   "value": expr, "display": expr / div, "unit": unit, "optimal": True, "fixed": True})
            continue
        left = deadline - time.monotonic()
        if left <= 0.05:
            break
        m.Minimize(expr)
        solver.parameters.max_time_in_seconds = max(0.1, left / (len(stages) - i) if i < len(stages) - 1 else left)
        st = solver.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if not ok:
                return {"info": {**info, "status": "inviável", "time": time.monotonic() - t0},
                        "error": "Não foi possível montar um plano com essas restrições (confira as quantidades "
                                 "fixas/mínimas e o tamanho do imóvel)."}
            break
        ok = True
        val = int(round(solver.ObjectiveValue()))
        all_optimal &= st == cp_model.OPTIMAL
        unit, div = CRIT_UNITS.get(label, ("bins", 1))
        info["stages"].append({"criterion": label, "label": SITE_CRITERIA.get(label, "Falta de bins (guardar toda a demanda)"),
                               "value": val, "display": val / div, "unit": unit, "optimal": st == cp_model.OPTIMAL})
        m.Add(expr <= val)
        # guarda a solução desta etapa (a próxima pode estourar o tempo)
        sol = {"qty": {s.id: solver.Value(x[s.id]) for s in active},
               "z": {k: solver.Value(v) for k, v in z.items()},
               "seg_mods": [{s.id: solver.Value(n[(s.id, g)]) for s in active} for g in range(len(segs))],
               "picks": [next(k for k, v in enumerate(vs) if solver.Value(v)) for vs in y]}
        m.ClearHints()
        for k, v in n.items():
            m.AddHint(v, sol["seg_mods"][k[1]][k[0]])
        for k, v in z.items():
            m.AddHint(v, sol["z"][k])
        for vs, k in zip(y, sol["picks"]):
            for j, v in enumerate(vs):
                m.AddHint(v, 1 if j == k else 0)
    info["status"] = "ótimo comprovado" if all_optimal else "melhor solução no tempo limite"
    info["time"] = time.monotonic() - t0
    sol["seg_mods"] = list(zip(segs, sol["seg_mods"])) if sol["seg_mods"] else []
    return {"info": info, **sol}


def _place(plan: SitePlan, floor: Floor, active: list[StructureSpec], seg_mods: list, z: dict, caps: dict,
           types: dict[int, BinSpec], demands: list[Demand], prm: SiteParams) -> None:
    from services.addressing import build_address

    by_id = {s.id: s for s in active}
    # módulos na planta: em cada trecho, as estruturas em ordem de código
    for (row_i, seg_k), mods in seg_mods:
        row = floor.rows[row_i]
        x = floor.segments[seg_k][0]
        for sid in sorted(mods, key=lambda i: by_id[i].code):
            for _ in range(mods[sid]):
                s = by_id[sid]
                plan.modules.append(ModulePlaced(struct=s, row=row, x0=x,
                                                 levels=[LevelPlaced(n=i + 1) for i in range(s.levels)]))
                x += s.width
    # endereço: corredor (letra) e estante (ímpar de um lado, par do outro, na ordem ao longo do corredor)
    aisles_used = sorted({m.row.aisle for m in plan.modules})
    aisle_code = {a: _letters(i) for i, a in enumerate(aisles_used)}
    for a in aisles_used:
        for side, start in (("L", 1), ("R", 2)):
            mods = sorted([m for m in plan.modules if m.row.aisle == a and m.row.side == side], key=lambda m: m.x0)
            for i, m in enumerate(mods):
                m.aisle, m.bay = aisle_code[a], f"{start + 2 * i:02d}"
    plan.modules.sort(key=lambda m: (m.aisle, int(m.bay or 0)))

    # tipos nos níveis: por estrutura, os níveis mais baixos recebem as bins mais altas/pesadas
    for s in active:
        alloc = sorted([(t, k) for (t, sid), k in z.items() if sid == s.id and k > 0],
                       key=lambda tk: (-(types[tk[0]].h or 0), -(types[tk[0]].volume_cm3 or 0), types[tk[0]].code))
        slots = sorted([lv for m in plan.modules if m.struct.id == s.id for lv in m.levels], key=lambda lv: lv.n)
        # ordem: nível 1 de todos os módulos, depois nível 2... (mantém o mesmo tipo contínuo ao longo do corredor)
        it = iter(slots)
        for t, k in alloc:
            for _ in range(k):
                lv = next(it, None)
                if lv is None:
                    break
                lv.type_id, lv.cap = t, caps[(t, s.id)]
    # SKUs nas posições
    owner = {id(lv): m for m in plan.modules for lv in m.levels}
    for t in sorted({t for d in demands for t in (d.bins or {})}, key=lambda k: types[k].code):
        slots = sorted([lv for m in plan.modules for lv in m.levels if lv.type_id == t],
                       key=lambda lv: (lv.n, owner[id(lv)].aisle, int(owner[id(lv)].bay or 0)))
        order = sorted([d for d in demands if (d.bins or {}).get(t)], key=lambda d: (d.material, d.sku))
        si = 0
        for d in order:
            k = d.bins[t]
            total_bins = d.total_bins or 1
            units_left = round(d.units * k / total_bins)
            unit_w = d.unit.weight or 0.0
            per_bin_units = units_left / k if k else 0
            while k > 0 and si < len(slots):
                lv = slots[si]
                free = lv.cap - lv.used
                s = owner[id(lv)].struct
                if prm.enforce_weight and s.max_load and unit_w:
                    w_bin = per_bin_units * unit_w
                    by_w = int((s.max_load - lv.load) // w_bin) if w_bin > 0 else free
                    free = min(free, max(0, by_w))
                if free <= 0:
                    si += 1
                    continue
                take = min(free, k)
                units = round(per_bin_units * take) if k > take else units_left
                mod = owner[id(lv)]
                pos = f"{len(lv.positions) + 1:02d}"
                lvl = str(lv.n)
                lv.positions.append(Position(build_address(mod.aisle, mod.bay, lvl, pos), mod.aisle, mod.bay, lvl, pos,
                                             s.code, t, take, d.sku, d.name, units, units * unit_w))
                lv.used += take
                lv.load += units * unit_w
                d.placed_bins += take
                k -= take
                units_left -= units
        for d in order:
            if d.placed_bins < d.total_bins and d.status == "ok":
                d.status = "falta"
                d.note = f"{d.total_bins - d.placed_bins} de {d.total_bins} bin(s) sem lugar."
    for d in demands:
        if d.status == "ok" and d.total_bins and len({p.address for p in plan.positions if p.sku == d.sku}) > 1:
            d.note = "Dividido em mais de uma posição."


# --------------------------------------------------------------------------- #
# Planta em SVG (vista de cima)
# --------------------------------------------------------------------------- #
PALETTE = ["#2f7ed8", "#e8743b", "#19a979", "#ed4a7b", "#945ecf", "#13a4b4", "#c3a32c", "#6f7c87"]


def floor_svg(plan: SitePlan, width_px: int = 1000) -> str:
    fl, b = plan.floor, plan.building
    if not fl.run_cm or not fl.cross_cm:
        return ""
    sc = width_px / fl.run_cm
    W, H = fl.run_cm * sc, fl.cross_cm * sc
    color = {s.struct.id: PALETTE[i % len(PALETTE)] for i, s in enumerate(plan.structs)}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 -2 {W + 4:.1f} {H + 22:.1f}" class="w-100" '
           f'style="max-height:70vh" font-family="sans-serif">',
           f'<rect x="0" y="0" width="{W:.1f}" height="{H:.1f}" fill="#fbfcfd" stroke="#1f2d33" stroke-width="2"/>']
    gap = b.wall_gap_m * 100
    if b.front_reserved_m > 0:
        out.append(f'<rect x="{gap * sc:.1f}" y="{gap * sc:.1f}" width="{b.front_reserved_m * 100 * sc:.1f}" '
                   f'height="{(fl.cross_cm - 2 * gap) * sc:.1f}" fill="#fff3cd" stroke="#e0c068"/>')
        out.append(f'<text x="{(gap + b.front_reserved_m * 50) * sc:.1f}" y="{H / 2:.1f}" font-size="11" '
                   f'text-anchor="middle" fill="#8a6d1c" transform="rotate(-90 {(gap + b.front_reserved_m * 50) * sc:.1f} '
                   f'{H / 2:.1f})">Docas / expedição</text>')
    if b.back_reserved_m > 0:
        x = fl.run_cm - gap - b.back_reserved_m * 100
        out.append(f'<rect x="{x * sc:.1f}" y="{gap * sc:.1f}" width="{b.back_reserved_m * 100 * sc:.1f}" '
                   f'height="{(fl.cross_cm - 2 * gap) * sc:.1f}" fill="#fff3cd" stroke="#e0c068"/>')
    for i, (y, a) in enumerate(fl.aisles):
        out.append(f'<rect x="{fl.x_start * sc:.1f}" y="{y * sc:.1f}" width="{(fl.x_end - fl.x_start) * sc:.1f}" '
                   f'height="{a * sc:.1f}" fill="#eef2f4"/>')
    aisle_label = {}
    for m in plan.modules:
        aisle_label.setdefault(m.row.aisle, m.aisle)
    for i, (y, a) in enumerate(fl.aisles):
        if i in aisle_label:
            out.append(f'<text x="{(fl.x_start * sc) + 4:.1f}" y="{(y + a / 2) * sc + 4:.1f}" font-size="11" '
                       f'fill="#5b6770" font-weight="bold">Corredor {aisle_label[i]}</text>')
    for r in fl.rows:  # contorno das fileiras disponíveis
        for x0, x1 in fl.segments:
            out.append(f'<rect x="{x0 * sc:.1f}" y="{r.y * sc:.1f}" width="{(x1 - x0) * sc:.1f}" '
                       f'height="{r.depth * sc:.1f}" fill="none" stroke="#c9d1d6" stroke-dasharray="3 2"/>')
    for m in plan.modules:
        used = sum(lv.used for lv in m.levels)
        cap = sum(lv.cap for lv in m.levels) or 1
        op = 0.35 + 0.65 * min(1.0, used / cap)
        title = f"{m.aisle}-{m.bay} · {m.struct.code} · {used}/{cap} bins"
        out.append(f'<rect x="{m.x0 * sc:.1f}" y="{m.row.y * sc:.1f}" width="{m.struct.width * sc:.1f}" '
                   f'height="{m.row.depth * sc:.1f}" fill="{color[m.struct.id]}" fill-opacity="{op:.2f}" '
                   f'stroke="#fff" stroke-width="0.6"><title>{title}</title></rect>')
    out.append(f'<text x="0" y="{H + 15:.1f}" font-size="11" fill="#5b6770">{b.length_m:g} m × {b.width_m:g} m · '
               f'fileiras ao longo do {"comprimento" if fl.along == "length" else "largura"} · '
               f'{len(fl.rows)} fileira(s), {len(fl.aisles)} corredor(es)</text>')
    out.append("</svg>")
    return "".join(out)


def struct_colors(plan: SitePlan) -> dict:
    return {s.struct.id: PALETTE[i % len(PALETTE)] for i, s in enumerate(plan.structs)}
