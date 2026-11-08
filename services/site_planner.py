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
    end_caps: bool = True  # módulos de cabeceira (girados 90°) nas pontas das fileiras, aproveitando a sobra
    cap_align: str = "row"  # cabeceira "row" = encostada na fileira | "edge" = alinhada à borda do trecho
    address_mode: str = "aisle"  # letra por "aisle" (corredor: os dois lados com a mesma letra) | "row" (por rua/fileira)
    serpentine: bool = False  # por rua: a rua do outro lado do corredor é numerada ao contrário (começa onde a outra termina)
    zones: list = field(default_factory=list)  # [ZoneSpec]; vazio = zona única
    avenue_m: float = 2.0  # largura das avenidas entre zonas
    obstacles: list = field(default_factory=list)  # [Obstacle] áreas bloqueadas
    sectors: list = field(default_factory=list)  # [SectorSpec] recebimento, expedição…: posição escolhida pelo sistema

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
    golden_from: Optional[float] = None  # zona de ouro: faixa de altura (cm do piso) mais ergonômica para pegar
    golden_to: Optional[float] = None

    @property
    def pitch(self) -> float:
        """Distância entre as bases de dois níveis (cm): a altura total dividida pelos níveis."""
        return self.height / max(1, self.levels)

    def is_golden(self, n: int) -> bool:
        """O nível `n` (1 = de baixo) está na zona de ouro: a altura de pega (meio do vão) cai na faixa."""
        if self.golden_from is None or self.golden_to is None:
            return False
        mid = (n - 1) * self.pitch + self.level_h / 2
        return self.golden_from - EPS <= mid <= self.golden_to + EPS

    @property
    def golden_levels(self) -> list[int]:
        return [n for n in range(1, self.levels + 1) if self.is_golden(n)]


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
    time_limit: Optional[float] = 15.0  # None = sem limite (cada critério vai até provar o ótimo)
    priority: Optional[list] = None  # desempates (2º, 3º, 4º critério); None = padrão do objetivo
    # posição dos SKUs (slotting): {"on": bool, "ref": "auto"|"docks"|"sector:<nome>"|"xy", "ref_xy": (x_m, y_m),
    # "golden_m": metros de caminhada que a zona de ouro vale, "affinity": {(sku_a, sku_b): força 0–1},
    # "source": rótulo da popularidade, "metric": rótulo da medida}; None = sem slotting
    slotting: Optional[dict] = None

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
    cap: Optional[int] = None  # limite de unidades definido pelo usuário (sugestão de redução da demanda)
    bin_fill: Optional[float] = None  # ocupação das bins do SKU (%, volume), depois de alocado
    pop: Optional[float] = None  # popularidade (linhas de pedido ou unidades de saída no período)
    abc: str = ""  # curva ABC pela popularidade (A = até 80% das saídas, B = até 95%, C = o resto)

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
    y: float  # cm, posição na direção transversal às fileiras (coordenada local da zona)
    depth: float
    aisle: int  # índice do corredor que a fileira atende (na zona)
    side: str  # "L" (antes do corredor) ou "R" (depois)
    block: int = 0  # bloco de fileiras: fileira simples na parede ou as duas de uma fileira dupla (costas com costas)
    zone: int = 0


@dataclass
class Block:
    """Faixa de fileiras contíguas (simples = 1 fileira; dupla = 2 costas com costas) — onde cabem as cabeceiras."""
    id: int
    y: float
    depth: float  # profundidade total da faixa (cm)
    rows: list = field(default_factory=list)  # índices das fileiras


@dataclass
class Lane:
    """Trecho disponível de uma fileira (entre pontas, corredores transversais e obstáculos)."""
    row: int
    seg: int  # trecho original da zona (entre corredores transversais)
    x0: float
    x1: float
    start: Optional[str] = None  # área livre na ponta inicial (None = sem acesso: parede/obstáculo)
    end: Optional[str] = None
    full_start: bool = True  # a ponta é a ponta original do trecho (não foi cortada por obstáculo)
    full_end: bool = True

    @property
    def length(self) -> float:
        return self.x1 - self.x0


@dataclass
class Obstacle:
    """Área bloqueada (escritório, sala, pilar, rampa…): retângulo na planta, em metros a partir do canto
    (X ao longo do comprimento, Y ao longo da largura)."""
    name: str
    x_m: float
    y_m: float
    length_m: float  # extensão em X
    width_m: float  # extensão em Y
    kind: str = "obstacle"  # "obstacle" | "sector" (setor posicionado: ocupa o piso como um obstáculo)
    note: str = ""


SECTOR_PLACES = {"auto": "Automático (onde perde menos espaço de armazenagem)",
                 "front": "Junto às docas (frente)", "back": "No fundo", "side": "Nas laterais (paredes de cima/baixo)",
                 "fixed": "Posição fixa (X/Y)"}


@dataclass
class SectorSpec:
    """Setor operacional (recebimento, expedição, conferência, embalagem…): o usuário informa só o espaço
    necessário; o sistema escolhe o lugar (faixa junto a uma parede ou canto, em proporções diferentes) que deixa o
    máximo de posições de estrutura na planta. Com `place = "fixed"`, X/Y/comprimento/largura definem o retângulo."""
    name: str
    area_m2: float
    min_side_m: float = 2.0  # menor lado aceitável do retângulo
    place: str = "auto"
    x_m: Optional[float] = None
    y_m: Optional[float] = None
    length_m: Optional[float] = None
    width_m: Optional[float] = None


@dataclass
class Floor:
    """Uma zona da planta, em coordenadas locais: x ao longo das fileiras, y transversal. `to_global` leva para a
    planta (X ao longo do comprimento do imóvel, Y ao longo da largura)."""
    along: str  # "length" | "width": direção das fileiras no imóvel
    run_cm: float  # dimensão da zona ao longo das fileiras
    cross_cm: float
    depth: float
    rows: list[Row] = field(default_factory=list)
    aisles: list[tuple[float, float]] = field(default_factory=list)  # (y, largura); largura 0 = área livre da borda
    segments: list[tuple[float, float]] = field(default_factory=list)  # trechos (x0, x1) ao longo da fileira
    blocks: list[Block] = field(default_factory=list)
    # pontas de cada trecho: [(nome da área livre no início ou None, idem no fim)] — None = sem acesso (parede)
    seg_ends: list[tuple] = field(default_factory=list)
    end_caps: bool = True
    x_start: float = 0.0
    x_end: float = 0.0
    notes: list[str] = field(default_factory=list)
    index: int = 0
    name: str = ""
    ox: float = 0.0  # origem na planta (cm)
    oy: float = 0.0
    rot: bool = False  # True: x local corre ao longo da largura do imóvel (Y)
    aisle_cm: float = 0.0
    struct_ids: Optional[set] = None  # estruturas permitidas na zona (None = todas)
    edge_names: dict = field(default_factory=dict)  # corredor de borda (largura 0) -> área livre que ele usa
    cross_lo: float = 0.0
    cross_hi: float = 0.0
    areas: list = field(default_factory=list)  # [(tipo, x, y, w, d, rótulo)] locais: faixas reservadas
    lanes: list[Lane] = field(default_factory=list)  # trechos disponíveis de cada fileira

    @property
    def seg_len(self) -> float:
        return (self.segments[0][1] - self.segments[0][0]) if self.segments else 0.0

    @property
    def total_row_m(self) -> float:
        return sum(ln.length for ln in self.lanes) / 100.0

    def to_local(self, X: float, Y: float, W: float, D: float) -> tuple[float, float, float, float]:
        """Inverso de `to_global` (retângulo da planta -> coordenadas da zona)."""
        if self.rot:
            return Y - self.oy, X - self.ox, D, W
        return X - self.ox, Y - self.oy, W, D

    def allows(self, sid: int) -> bool:
        return self.struct_ids is None or sid in self.struct_ids

    def to_global(self, x: float, y: float, w: float, d: float) -> tuple[float, float, float, float]:
        if self.rot:
            return self.ox + y, self.oy + x, d, w
        return self.ox + x, self.oy + y, w, d


@dataclass
class Layout:
    """Planta do imóvel: uma ou mais zonas (faixas ao longo do comprimento, cada uma com a sua orientação),
    separadas por avenidas, mais as faixas reservadas."""
    length_cm: float
    width_cm: float
    zones: list[Floor] = field(default_factory=list)
    areas: list = field(default_factory=list)  # [(tipo, X, Y, W, D, rótulo)] globais: avenidas e faixas reservadas
    notes: list[str] = field(default_factory=list)
    obstacles: list = field(default_factory=list)  # [Obstacle] (inclui os setores posicionados, kind="sector")
    sectors: list = field(default_factory=list)  # [Obstacle] setores posicionados

    @property
    def mixed(self) -> bool:
        return len(self.zones) > 1

    @property
    def along(self) -> str:
        kinds = {z.along for z in self.zones}
        return kinds.pop() if len(kinds) == 1 else "mixed"

    @property
    def rows(self) -> list[Row]:
        return [r for z in self.zones for r in z.rows]

    @property
    def real_aisles(self) -> int:
        return sum(1 for z in self.zones for _, a in z.aisles if a > 0)

    @property
    def depth(self) -> float:
        return max((z.depth for z in self.zones), default=0.0)

    @property
    def total_row_m(self) -> float:
        return sum(z.total_row_m for z in self.zones)

    def shapes(self) -> list[tuple]:
        """Áreas para desenhar, em coordenadas da planta: (tipo, X, Y, W, D, rótulo).
        Tipos: reserved, avenue, aisle, cross, lane (contorno de fileira disponível)."""
        out = list(self.areas)
        for z in self.zones:
            for kind, x, y, w, d, label in z.areas:
                out.append((kind, *z.to_global(x, y, w, d), label))
            for y, a in z.aisles:
                if a > 0:
                    out.append(("aisle", *z.to_global(z.x_start, y, z.x_end - z.x_start, a), ""))
            for (_, x1), (x2, _) in zip(z.segments, z.segments[1:]):
                out.append(("cross", *z.to_global(x1, z.cross_lo, x2 - x1, z.cross_hi - z.cross_lo), ""))
            for ln in z.lanes:
                r = z.rows[ln.row]
                out.append(("lane", *z.to_global(ln.x0, r.y, ln.length, r.depth), ""))
        for ob in self.obstacles:  # por cima de corredores e avenidas
            out.append((getattr(ob, "kind", "obstacle"), ob.x_m * 100, ob.y_m * 100, ob.length_m * 100,
                        ob.width_m * 100, ob.name))
        return out


ZONE_ORIENTATIONS = {"along": "Fileiras ao longo do comprimento", "across": "Fileiras ao longo da largura (perpendiculares)"}
ADDRESS_MODES = {"aisle": "Uma letra por corredor (as duas fileiras de frente têm a mesma letra; estantes ímpares de um lado, "
                          "pares do outro)",
                 "row": "Uma letra por rua/fileira (fileiras de frente uma para a outra têm letras diferentes; estantes "
                        "01, 02, 03… ao longo da fileira)"}
CAP_ALIGN = {"row": "Encostada na fileira (a sobra fica do lado da área livre)",
             "edge": "Alinhada à borda do trecho (a sobra fica entre a fileira e a cabeceira)"}


@dataclass
class ZoneSpec:
    """Zona definida pelo usuário. Sem X/Y: faixa do imóvel ao longo do comprimento, empilhada a partir da frente
    (docas) e separada das outras por avenidas. Com X/Y: retângulo em qualquer lugar do imóvel (como um obstáculo),
    e o espaço entre as zonas é a circulação."""
    length_m: Optional[float] = None  # extensão em X; sem X/Y, None/0 = o comprimento que sobrar
    orientation: str = "along"  # "along" | "across"
    aisle_m: Optional[float] = None  # largura dos corredores da zona (None = a do imóvel)
    cross_aisles: int = 0
    structures: Optional[set] = None  # estruturas permitidas (None = todas)
    name: str = ""
    x_m: Optional[float] = None  # canto da zona na planta (m); None = zona empilhada
    y_m: Optional[float] = None
    width_m: Optional[float] = None  # extensão em Y (None = até a parede oposta)

    @property
    def positioned(self) -> bool:
        return self.x_m is not None and self.y_m is not None


def _letters(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


def _cross_layout(cross: float, d: float, a: float, open_start: bool = False, open_end: bool = False) -> Optional[list[str]]:
    """Melhor sequência de blocos na direção transversal: S (fileira simples), D (dupla, costas com costas),
    A (corredor) e E (área livre na borda da zona — avenida/docas — usada como corredor, sem custo de largura).
    Cada fileira precisa de um corredor (A ou E) à frente; a dupla, dos dois lados."""
    best = None
    kmax = int(cross // (2 * d)) + 1 if d > 0 else 0
    for s_first in (1, 0):
        for s_last in (1, 0):
            for k in range(0, kmax + 1):
                blocks = ["S"] * s_first + ["D"] * k + ["S"] * s_last
                if not blocks:
                    continue
                for lead in (0, 1):
                    for trail in (0, 1):
                        seq: list[str] = (["E" if open_start else "A"] if lead else [])
                        for i, bk in enumerate(blocks):
                            if i:
                                seq.append("A")
                            seq.append(bk)
                        if trail:
                            seq.append("E" if open_end else "A")
                        aisle = lambda j: 0 <= j < len(seq) and seq[j] in "AE"  # noqa: E731
                        valid = all((aisle(j - 1) or aisle(j + 1)) if el == "S" else (aisle(j - 1) and aisle(j + 1))
                                    for j, el in enumerate(seq) if el in "SD")
                        if not valid:
                            continue
                        total = sum({"S": d, "D": 2 * d, "A": a, "E": 0}[x] for x in seq)
                        if total > cross + EPS:
                            continue
                        rows = s_first + s_last + 2 * k
                        key = (rows, -total)
                        if best is None or key > best[0]:
                            best = (key, seq)
    return best[1] if best else None


def build_zone(depth_cm: float, aisle_cm: float, run_len: float, cross_len: float, *, run_pad=(0.0, 0.0),
               cross_pad=(0.0, 0.0), open_cross=(None, None), run_ends=(None, None), cross_aisles: int = 0,
               cross_aisle_cm: float = 200.0, end_caps: bool = True, along: str = "length") -> Floor:
    """Monta as fileiras de uma zona retangular (coordenadas locais). `open_cross`: área livre (nome) nas bordas
    transversais, que serve de corredor; `run_ends`: área livre nas pontas das fileiras (acesso às cabeceiras)."""
    fl = Floor(along=along, run_cm=run_len, cross_cm=cross_len, depth=depth_cm, end_caps=end_caps, aisle_cm=aisle_cm)
    a = aisle_cm
    fl.cross_lo, fl.cross_hi = cross_pad[0], cross_len - cross_pad[1]
    seq = _cross_layout(fl.cross_hi - fl.cross_lo, depth_cm, a, bool(open_cross[0]), bool(open_cross[1])) \
        if depth_cm > 0 else None
    if not seq:
        fl.notes.append("A zona não comporta nenhuma fileira com corredor.")
        return fl
    y = fl.cross_lo
    aisle_idx = -1
    pending_l: list[tuple[float]] = []  # fileiras que esperam o próximo corredor
    for i, el in enumerate(seq):
        if el in "AE":
            aisle_idx += 1
            wdt = a if el == "A" else 0.0
            fl.aisles.append((y, wdt))
            if el == "E":
                fl.edge_names[aisle_idx] = open_cross[0] if i == 0 else open_cross[1]
            for (yy,) in pending_l:
                fl.rows.append(Row(len(fl.rows), yy, depth_cm, aisle_idx, "L"))
            pending_l = []
            y += wdt
        elif el == "S":
            prev_is_aisle = i > 0 and seq[i - 1] in "AE"
            if prev_is_aisle:
                fl.rows.append(Row(len(fl.rows), y, depth_cm, aisle_idx, "R"))
            else:
                pending_l.append((y,))
            fl.blocks.append(Block(len(fl.blocks), y, depth_cm))
            y += depth_cm
        else:  # D: frente para o corredor anterior, fundo para o próximo
            fl.rows.append(Row(len(fl.rows), y, depth_cm, aisle_idx, "R"))
            pending_l.append((y + depth_cm,))
            fl.blocks.append(Block(len(fl.blocks), y, 2 * depth_cm))
            y += 2 * depth_cm
    fl.rows.sort(key=lambda r: r.y)
    for i, r in enumerate(fl.rows):
        r.index = i
        blk = next(bk for bk in fl.blocks if bk.y - EPS <= r.y < bk.y + bk.depth - EPS)
        r.block = blk.id
        blk.rows.append(i)
    x0, x1 = run_pad[0], run_len - run_pad[1]
    n = max(0, int(cross_aisles)) + 1
    ca = cross_aisle_cm
    seg = (x1 - x0 - (n - 1) * ca) / n
    fl.x_start, fl.x_end = x0, x1
    if seg <= 0:
        fl.notes.append("Faixas reservadas e corredores transversais ocupam todo o comprimento das fileiras.")
        fl.rows = []
        return fl
    fl.segments = [(x0 + i * (seg + ca), x0 + i * (seg + ca) + seg) for i in range(n)]
    fl.seg_ends = [(run_ends[0] if i == 0 else f"o corredor transversal {i}",
                    run_ends[1] if i == n - 1 else f"o corredor transversal {i + 1}") for i in range(n)]
    fl.lanes = [Lane(r.index, k, a0, a1, *fl.seg_ends[k]) for r in fl.rows for k, (a0, a1) in enumerate(fl.segments)]
    return fl


def apply_obstacles(lay: Layout, obstacles: list, clearance_cm: float = 0.0) -> None:
    """Corta os trechos de fileira que passam por obstáculos (com a folga das paredes). A ponta cortada fica sem
    acesso (não recebe cabeceira)."""
    lay.obstacles = list(obstacles)
    for ob in obstacles:
        cut = 0
        X, Y, L, W = ob.x_m * 100 - clearance_cm, ob.y_m * 100 - clearance_cm, ob.length_m * 100 + 2 * clearance_cm, \
            ob.width_m * 100 + 2 * clearance_cm
        for z in lay.zones:
            ox, oy, ow, od = z.to_local(X, Y, L, W)
            keep = []
            for ln in z.lanes:
                r = z.rows[ln.row]
                if not (r.y < oy + od - EPS and oy < r.y + r.depth - EPS and ln.x0 < ox + ow - EPS and ox < ln.x1 - EPS):
                    keep.append(ln)
                    continue
                cut += 1
                if ox - ln.x0 >= 1:  # pedaço antes do obstáculo
                    keep.append(Lane(ln.row, ln.seg, ln.x0, ox, ln.start, None, ln.full_start, False))
                if ln.x1 - (ox + ow) >= 1:  # pedaço depois
                    keep.append(Lane(ln.row, ln.seg, ox + ow, ln.x1, None, ln.end, False, ln.full_end))
            z.lanes = keep
        if cut and getattr(ob, "kind", "obstacle") == "obstacle":
            lay.notes.append(f"Obstáculo {ob.name}: {cut} trecho(s) de fileira encurtado(s) ou removido(s).")


def build_floor(b: Building, depth_cm: float, along: str) -> Floor:
    """Planta de zona única (sem zonas definidas): todas as fileiras na mesma direção."""
    run = (b.length_m if along == "length" else b.width_m) * 100.0
    cross = (b.width_m if along == "length" else b.length_m) * 100.0
    gap, a = b.wall_gap_m * 100.0, b.aisle_m * 100.0
    front, back = b.front_reserved_m * 100.0, b.back_reserved_m * 100.0
    # pontas com acesso (cabeceira de frente para uma área livre): docas/frente e fundo se a faixa tiver ao menos a
    # largura de um corredor; corredores transversais sempre
    fl = build_zone(depth_cm, a, run, cross, run_pad=(gap + front, gap + back), cross_pad=(gap, gap),
                    run_ends=("a faixa da frente (docas)" if front >= a - EPS else None,
                              "a faixa do fundo" if back >= a - EPS else None),
                    cross_aisles=b.cross_aisles, cross_aisle_cm=b.cross_aisle_m * 100.0, end_caps=b.end_caps,
                    along=along)
    if not fl.rows and not fl.notes:
        fl.notes.append("O imóvel não comporta nenhuma fileira com corredor nessa orientação.")
    if fl.notes and fl.notes[0].startswith("A zona não comporta"):
        fl.notes[0] = "O imóvel não comporta nenhuma fileira com corredor nessa orientação."
    fl.rot = along == "width"
    if front > 0:
        fl.areas.append(("reserved", gap, gap, front, cross - 2 * gap, "Docas / expedição"))
    if back > 0:
        fl.areas.append(("reserved", run - gap - back, gap, back, cross - 2 * gap, "Área reservada"))
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


def zone_rects(b: Building) -> list[tuple[float, float, float, float]]:
    """Retângulos (X, Y, extensão X, extensão Y em cm) das zonas posicionadas, limitados ao interior do imóvel
    (descontado o afastamento das paredes)."""
    L, W, gap = b.length_m * 100.0, b.width_m * 100.0, b.wall_gap_m * 100.0
    out = []
    for z in b.zones:
        x0, y0 = max(gap, (z.x_m or 0) * 100.0), max(gap, (z.y_m or 0) * 100.0)
        x1 = min(L - gap, (z.x_m or 0) * 100.0 + (z.length_m or 0) * 100.0) if z.length_m else L - gap
        y1 = min(W - gap, (z.y_m or 0) * 100.0 + z.width_m * 100.0) if z.width_m else W - gap
        out.append((x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0)))
    return out


def _overlap(a: tuple, b_: tuple) -> bool:
    return a[0] < b_[0] + b_[2] - EPS and b_[0] < a[0] + a[2] - EPS and a[1] < b_[1] + b_[3] - EPS and b_[1] < a[1] + a[3] - EPS


def _positioned_layout(b: Building, structs: list[StructureSpec], lay: Layout) -> Layout:
    """Zonas em qualquer lugar do imóvel (X/Y). Uma borda da zona é "aberta" (as fileiras podem ficar de frente para
    ela, e as pontas recebem cabeceira) quando, do lado de fora, há uma faixa livre de pelo menos um corredor dentro
    do imóvel, sem outra zona nem obstáculo."""
    L, W = b.length_m * 100.0, b.width_m * 100.0
    gap, front, back = (v * 100.0 for v in (b.wall_gap_m, b.front_reserved_m, b.back_reserved_m))
    if front > 0:
        lay.areas.append(("reserved", gap, gap, front, W - 2 * gap, "Docas / expedição"))
    if back > 0:
        lay.areas.append(("reserved", L - gap - back, gap, back, W - 2 * gap, "Área reservada"))
    rects = zone_rects(b)
    blocks = [(o.x_m * 100, o.y_m * 100, o.length_m * 100, o.width_m * 100) for o in b.obstacles]

    def side(i: int, which: str, a: float) -> Optional[str]:
        x, y, lx, wy = rects[i]
        strip = {"left": (x - a, y, a, wy), "right": (x + lx, y, a, wy),
                 "top": (x, y - a, lx, a), "bottom": (x, y + wy, lx, a)}[which]
        sx, sy, sw, sd = strip
        if sx < gap - EPS or sy < gap - EPS or sx + sw > L - gap + EPS or sy + sd > W - gap + EPS:
            return None  # parede
        if any(_overlap(strip, r) for j, r in enumerate(rects) if j != i) or any(_overlap(strip, o) for o in blocks):
            return None
        if front > 0 and sx < gap + front - EPS:
            return "a faixa da frente (docas)"
        if back > 0 and sx + sw > L - gap - back + EPS:
            return "a faixa do fundo"
        return "a circulação"

    for i, (z, (x, y, lx, wy)) in enumerate(zip(b.zones, rects)):
        allowed = [s for s in structs if not z.structures or s.id in z.structures]
        depth = max((s.depth for s in allowed), default=0.0)
        a = (z.aisle_m or b.aisle_m) * 100.0
        if z.orientation == "across":  # fileiras ao longo de Y; transversal = X
            fl = build_zone(depth, a, wy, lx, open_cross=(side(i, "left", a), side(i, "right", a)),
                            run_ends=(side(i, "top", a), side(i, "bottom", a)), cross_aisles=z.cross_aisles,
                            cross_aisle_cm=b.cross_aisle_m * 100.0, end_caps=b.end_caps, along="width")
            fl.rot = True
        else:
            fl = build_zone(depth, a, lx, wy, open_cross=(side(i, "top", a), side(i, "bottom", a)),
                            run_ends=(side(i, "left", a), side(i, "right", a)), cross_aisles=z.cross_aisles,
                            cross_aisle_cm=b.cross_aisle_m * 100.0, end_caps=b.end_caps, along="length")
        fl.ox, fl.oy, fl.index, fl.name = x, y, i, z.name or f"Zona {i + 1}"
        fl.struct_ids = {s.id for s in allowed}
        for r in fl.rows:
            r.zone = i
        if not allowed:
            fl.notes.append("nenhuma estrutura ativa permitida")
        if len({s.depth for s in allowed}) > 1:
            fl.notes.append(f"profundidades diferentes: fileiras dimensionadas pela maior ({depth:g} cm)")
        lay.notes += [f"{fl.name}: {n}" for n in fl.notes]
        lay.areas.append(("zone", x, y, lx, wy, fl.name))
        lay.zones.append(fl)
    return lay


def build_layout(b: Building, structs: list[StructureSpec]) -> Layout:
    if b.sectors:
        return _layout_with_sectors(b, structs)
    lay = _build_layout(b, structs)
    if b.obstacles:
        apply_obstacles(lay, b.obstacles, b.wall_gap_m * 100.0)
    return lay


def _slots(lay: Layout, structs: list[StructureSpec]) -> int:
    """Medida da capacidade de armazenagem da planta: módulos da estrutura mais estreita que cabem nos trechos."""
    narrow = min((s.width for s in structs), default=100.0) or 100.0
    return sum(int(math.floor(ln.length / narrow + EPS)) for z in lay.zones for ln in z.lanes)


def _sector_candidates(b: Building, sec: SectorSpec, taken: list) -> list[tuple[str, float, float, float, float]]:
    """Retângulos possíveis (lugar, X, Y, extensão X, extensão Y em cm) com a área do setor: faixas encostadas numa
    parede (largura inteira) e cantos em proporções diferentes, sem invadir docas, faixa do fundo, obstáculos e
    setores já posicionados."""
    L, W = b.length_m * 100.0, b.width_m * 100.0
    gap, front, back = (v * 100.0 for v in (b.wall_gap_m, b.front_reserved_m, b.back_reserved_m))
    x0, x1, y0, y1 = gap + front, L - gap - back, gap, W - gap
    fw, fh = x1 - x0, y1 - y0
    area, mins = sec.area_m2 * 10_000.0, (sec.min_side_m or 0) * 100.0
    up = lambda v: math.ceil(v / 10.0 - EPS) * 10.0  # noqa: E731  (múltiplos de 10 cm, sem perder área)
    out = []
    if fh > 0:  # faixas na largura inteira, na frente (junto às docas) e no fundo
        d = up(area / fh)
        out += [("front", x0, y0, d, fh), ("back", x1 - d, y0, d, fh)]
    if fw > 0:  # faixas no comprimento inteiro, nas paredes de cima e de baixo
        d = up(area / fw)
        out += [("side", x0, y0, fw, d), ("side", x0, y1 - d, fw, d)]
    for r in (1.0, 2.0, 0.5, 3.0, 1 / 3):  # cantos (comprimento/largura = r)
        w = up(math.sqrt(area * r))
        d = up(area / w)
        for place, x, y in (("front", x0, y0), ("front", x0, y1 - d), ("back", x1 - w, y0), ("back", x1 - w, y1 - d)):
            out.append((place, x, y, w, d))
    ok = []
    for c in out:
        _, x, y, w, d = c
        if w < mins - EPS or d < mins - EPS or w <= 0 or d <= 0:
            continue
        if x < x0 - EPS or y < y0 - EPS or x + w > x1 + EPS or y + d > y1 + EPS:
            continue
        if any(_overlap((x, y, w, d), t) for t in taken):
            continue
        if c not in ok:
            ok.append(c)
    return ok


def _layout_with_sectors(b: Building, structs: list[StructureSpec]) -> Layout:
    """Posiciona os setores (do maior para o menor), cada um no candidato que deixa mais posições de estrutura;
    empate: o mais perto das docas. Os setores entram na planta como áreas ocupadas (cortam as fileiras)."""
    import dataclasses

    placed: list[Obstacle] = []
    notes: list[str] = []
    taken = [(o.x_m * 100, o.y_m * 100, o.length_m * 100, o.width_m * 100) for o in b.obstacles]
    for sec in [s for s in b.sectors if s.place == "fixed"]:
        ob = Obstacle(sec.name, sec.x_m or 0.0, sec.y_m or 0.0, sec.length_m or 0.0, sec.width_m or 0.0, kind="sector",
                      note="posição fixa")
        placed.append(ob)
        taken.append((ob.x_m * 100, ob.y_m * 100, ob.length_m * 100, ob.width_m * 100))
    br = lambda v: f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")  # noqa: E731
    for sec in sorted([s for s in b.sectors if s.place != "fixed"], key=lambda s: -s.area_m2):
        cands = [c for c in _sector_candidates(b, sec, taken) if sec.place == "auto" or c[0] == sec.place]
        if not cands:
            notes.append(f"Setor {sec.name}: não coube ({br(sec.area_m2)} m², lado mínimo {br(sec.min_side_m or 0)} m"
                         f"{'' if sec.place == 'auto' else ', ' + SECTOR_PLACES[sec.place].lower()}).")
            continue
        best = None
        for c in cands:
            _, x, y, w, d = c
            ob = Obstacle(sec.name, x / 100, y / 100, w / 100, d / 100, kind="sector")
            trial = dataclasses.replace(b, sectors=[], obstacles=list(b.obstacles) + placed + [ob])
            lay = _build_layout(trial, structs)
            apply_obstacles(lay, trial.obstacles, b.wall_gap_m * 100.0)
            score = (_slots(lay, structs), -x, -abs(w - d))  # mais posições; depois mais perto das docas; mais compacto
            if best is None or score > best[0]:
                best = (score, ob)
        ob = best[1]
        ob.note = f"{br(ob.length_m)} × {br(ob.width_m)} m ({br(ob.length_m * ob.width_m)} m²)"
        placed.append(ob)
        taken.append((ob.x_m * 100, ob.y_m * 100, ob.length_m * 100, ob.width_m * 100))
    final = dataclasses.replace(b, sectors=[], obstacles=list(b.obstacles) + placed)
    lay = _build_layout(final, structs)
    apply_obstacles(lay, final.obstacles, b.wall_gap_m * 100.0)
    lay.sectors = placed
    for ob in placed:
        who = ob.name if ob.name.lower().startswith("setor") else f"Setor {ob.name}"
        size = f"{br(ob.length_m)} × {br(ob.width_m)} m"
        lay.notes.append(f"{who}: X {br(ob.x_m)} m, Y {br(ob.y_m)} m, "
                         + (f"{size} ({ob.note})." if ob.note == "posição fixa" else f"{ob.note}."))
    lay.notes += notes
    return lay


def _build_layout(b: Building, structs: list[StructureSpec]) -> Layout:
    """Planta completa: zona única (orientação automática ou escolhida) ou as zonas definidas pelo usuário,
    em sequência a partir da frente (docas), separadas por avenidas da largura informada."""
    L, W = b.length_m * 100.0, b.width_m * 100.0
    lay = Layout(L, W)
    if not b.zones:
        fl = choose_floor(b, structs)
        lay.zones = [fl]
        lay.notes = list(fl.notes)
        return lay
    if any(z.positioned for z in b.zones):
        return _positioned_layout(b, structs, lay)
    gap, front, back, av = (v * 100.0 for v in (b.wall_gap_m, b.front_reserved_m, b.back_reserved_m, b.avenue_m))
    zs = b.zones
    avail = L - 2 * gap - front - back - av * (len(zs) - 1)
    fixed = sum((z.length_m or 0) * 100.0 for z in zs)
    free = [z for z in zs if not z.length_m]
    rem = avail - fixed
    if rem < -EPS or (free and rem <= EPS):
        br = lambda v: f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")  # noqa: E731
        lay.notes.append(f"As zonas com comprimento informado ({br(fixed / 100)} m) não cabem no comprimento útil para "
                         f"zonas ({br(avail / 100)} m, já descontadas paredes, faixas reservadas e avenidas)"
                         + (" e não sobra nada para a zona sem comprimento." if free and rem >= -EPS else "."))
        return lay
    if front > 0:
        lay.areas.append(("reserved", gap, gap, front, W - 2 * gap, "Docas / expedição"))
    if back > 0:
        lay.areas.append(("reserved", L - gap - back, gap, back, W - 2 * gap, "Área reservada"))
    X = gap + front
    for i, z in enumerate(zs):
        lz = (z.length_m or 0) * 100.0 or rem / len(free)
        allowed = [s for s in structs if not z.structures or s.id in z.structures]
        depth = max((s.depth for s in allowed), default=0.0)
        a = (z.aisle_m or b.aisle_m) * 100.0
        prev = f"a avenida {i}" if i > 0 else ("a faixa da frente (docas)" if front >= a - EPS else None)
        nxt = f"a avenida {i + 1}" if i < len(zs) - 1 else ("a faixa do fundo" if back >= a - EPS else None)
        if z.orientation == "across":
            side = "a circulação junto à parede" if gap >= a - EPS else None
            fl = build_zone(depth, a, W, lz, run_pad=(gap, gap), open_cross=(prev, nxt), run_ends=(side, side),
                            cross_aisles=z.cross_aisles, cross_aisle_cm=b.cross_aisle_m * 100.0, end_caps=b.end_caps,
                            along="width")
            fl.rot = True
        else:
            fl = build_zone(depth, a, lz, W, cross_pad=(gap, gap), run_ends=(prev, nxt), cross_aisles=z.cross_aisles,
                            cross_aisle_cm=b.cross_aisle_m * 100.0, end_caps=b.end_caps, along="length")
        fl.ox, fl.index, fl.name = X, i, z.name or f"Zona {i + 1}"
        fl.struct_ids = {s.id for s in allowed}
        for r in fl.rows:
            r.zone = i
        if not allowed:
            fl.notes.append("nenhuma estrutura ativa permitida")
        if len({s.depth for s in allowed}) > 1:
            fl.notes.append(f"profundidades diferentes: fileiras dimensionadas pela maior ({depth:g} cm)")
        lay.notes += [f"{fl.name}: {n}" for n in fl.notes]
        lay.zones.append(fl)
        X += lz
        if i < len(zs) - 1:
            lay.areas.append(("avenue", X, gap, av, W - 2 * gap, f"Avenida {i + 1}"))
            X += av
    return lay


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
    fill: Optional[float] = None  # ocupação das bins (%): volume das unidades ÷ volume útil das bins
    dist_m: Optional[float] = None  # caminhada (em L, pelos corredores) do ponto de referência até a frente da estante
    golden: bool = False  # zona de ouro da PEGA: nível na faixa de altura ergonômica da estrutura
    walk_golden: bool = False  # zona de ouro da CAMINHADA: posição entre as mais perto do ponto de referência


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
    rotated: bool = False  # cabeceira: girado 90°, atravessa a faixa da fileira (simples ou dupla) na ponta
    y0: Optional[float] = None  # cabeceira: posição transversal do canto
    faces: str = ""  # cabeceira: área livre para onde a frente do módulo está voltada
    segment: int = 0
    zone: Optional["Floor"] = None

    @property
    def grect(self) -> tuple[float, float, float, float]:
        """(X, Y, largura em X, largura em Y) na planta do imóvel (cm)."""
        if self.zone is None:
            return self.gx, self.gy, self.gw, self.gd
        return self.zone.to_global(self.gx, self.gy, self.gw, self.gd)

    @property
    def gx(self) -> float:
        return self.x0

    @property
    def gy(self) -> float:
        return self.y0 if self.rotated else self.row.y

    @property
    def gw(self) -> float:  # extensão ao longo da fileira
        return self.struct.depth if self.rotated else self.struct.width

    @property
    def gd(self) -> float:  # extensão transversal
        return self.struct.width if self.rotated else self.row.depth


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
    bin_fill: Optional[float] = None  # ocupação das bins alocadas desse tipo (%, volume)

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
    floor: Layout
    structs: list[StructLine] = field(default_factory=list)
    types: list[TypeLine] = field(default_factory=list)
    demands: list[Demand] = field(default_factory=list)
    modules: list[ModulePlaced] = field(default_factory=list)
    solver: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    aisle_codes: dict = field(default_factory=dict)  # (zona, corredor) -> letra
    bin_fill: Optional[float] = None  # ocupação média das bins alocadas (%, volume das unidades ÷ volume das bins)
    slotting: dict = field(default_factory=dict)  # indicadores da posição dos SKUs (ver `slotting_kpis`)
    # entradas do cálculo, para as vistas (3D): tipos de bin, compartimentos (tipo -> (bin pai, nº por pai)) e regras
    specs: dict = field(default_factory=dict, repr=False)
    parents: dict = field(default_factory=dict, repr=False)
    params: Optional[SiteParams] = field(default=None, repr=False)

    @property
    def positions(self) -> list[Position]:
        return [p for m in self.modules for lv in m.levels for p in lv.positions]

    @property
    def total_modules(self) -> int:
        return sum(s.qty for s in self.structs)

    @property
    def structures_cost(self) -> Optional[float]:
        """Custo das estruturas usadas. Estrutura sem custo informado conta como 0 (ver `missing_costs`);
        None só quando nenhuma das usadas tem custo."""
        costs = [s.cost for s in self.structs if s.qty]
        return sum(c or 0 for c in costs) if any(c is not None for c in costs) else None

    @property
    def bins_cost(self) -> Optional[float]:
        """Custo das bins necessárias (tipo sem custo informado conta como 0, como no solver)."""
        costs = [t.cost for t in self.types if t.needed]
        return sum(c or 0 for c in costs) if any(c is not None for c in costs) else None

    @property
    def total_cost(self) -> Optional[float]:
        """Estruturas + bins; None só quando nada usado tem custo informado."""
        parts = [c for c in (self.structures_cost, self.bins_cost) if c is not None]
        return sum(parts) if parts else None

    @property
    def missing_costs(self) -> list[str]:
        """Códigos das estruturas e tipos de bin usados sem custo informado (entram como R$ 0 no total)."""
        return ([s.struct.code for s in self.structs if s.qty and s.cost is None]
                + [t.spec.code if not t.parent_code else t.parent_code for t in self.types if t.needed and t.cost is None])

    @property
    def footprint_m2(self) -> float:
        return sum(s.footprint_m2 for s in self.structs)

    @property
    def front_m(self) -> float:
        """Metros de fileira ocupados (cabeceiras ocupam a profundidade delas em cada fileira da faixa)."""
        if not self.modules:
            return sum(s.front_m for s in self.structs)
        return sum(m.gw * (len(m.zone.blocks[m.row.block].rows) if m.rotated and m.zone and m.zone.blocks else 1)
                   for m in self.modules) / 100.0

    @property
    def end_caps(self) -> int:
        return sum(1 for m in self.modules if m.rotated)

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
    plan = SitePlan(building=b, floor=Layout(b.length_m * 100.0, b.width_m * 100.0), specs=types, parents=parents,
                    params=prm)
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
    floor = build_layout(b, active)
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
        if d.cap is not None and d.units > d.cap:
            d.units = max(0, int(d.cap))
            d.note = f"Limitado a {d.cap} un. (redução da demanda)."
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
    # Peso: o solver usa a capacidade efetiva de cada nível (bins que cabem sem passar da carga máxima, pelo peso
    # médio das bins do tipo). Se mesmo assim a alocação (que confere o peso bin a bin) deixar bins de fora, refaz
    # pelo peso da bin mais pesada do tipo.
    base_bins = {id(d): d.bins for d in demands}
    for attempt, stat in enumerate(("mean", "max") if prm.enforce_weight else ("none",)):
        if attempt:
            plan.modules, plan.structs, plan.notes = [], [], [n for n in plan.notes if "carga" not in n]
            for d in demands:
                d.bins, d.placed_bins = base_bins[id(d)], 0
                if d.status == "falta":
                    d.status, d.note = "ok", ""
        solve_caps = _weight_caps(caps, demands, choices, active, stat)
        sol = _solve(floor, active, fixed_need, solve_caps, prm, types, choices)
        for (d, opts), k in zip(choices, sol.get("picks") or []):
            d.bins = opts[k]
        _finish(plan, floor, structs, active, sol, caps, types, demands, prm, parents)
        if not plan.shortage or sol.get("error") or stat != "mean":
            break
    compute_bin_fill(plan, types, demands)
    if plan.shortage:
        plan.notes.append(f"{plan.shortage} bin(s) ficaram sem lugar: aumente as estruturas (ou libere o modo "
                          "\"Otimizar quantidade\"), o imóvel ou reduza a demanda.")
    return plan


def compute_bin_fill(plan: SitePlan, types: dict, demands: list) -> None:
    """Ocupação das bins por volume (unidades × volume da unidade ÷ bins × volume útil da bin), em cada endereço, por
    SKU, por tipo de bin e no total. É o "enchimento" dos outros modos de otimização: mostra quanto do espaço
    comprado em bins fica de fato ocupado (a ocupação dos níveis diz quanto das posições das estruturas tem bin)."""
    uvol = {d.sku: d.unit.volume_cm3 for d in demands if d.unit is not None}
    acc: dict = {"sku": {}, "type": {}, "all": [0.0, 0.0]}
    for pos in plan.positions:
        uv, spec = uvol.get(pos.sku), types.get(pos.type_id)
        bv = spec.volume_cm3 if spec is not None else None
        if not uv or not bv or not pos.bins:
            continue
        num, den = pos.units * uv, pos.bins * bv
        pos.fill = num / den * 100
        for key, k in (("sku", pos.sku), ("type", pos.type_id)):
            a = acc[key].setdefault(k, [0.0, 0.0])
            a[0] += num
            a[1] += den
        acc["all"][0] += num
        acc["all"][1] += den
    pct = lambda a: a[0] / a[1] * 100 if a and a[1] else None  # noqa: E731
    for d in demands:
        d.bin_fill = pct(acc["sku"].get(d.sku))
    for t in plan.types:
        t.bin_fill = pct(acc["type"].get(t.spec.id))
    plan.bin_fill = pct(acc["all"])


def _weight_caps(caps: dict, demands: list, choices: list, active: list, stat: str) -> dict:
    """Capacidade por nível limitada pela carga máxima da estrutura (peso por bin: média ou máximo do tipo)."""
    if stat == "none":
        return caps
    wts: dict[int, list] = {}
    opts_of = {id(d): opts for d, opts in choices}
    for d in demands:
        if not d.unit.weight:
            continue
        for cfg in opts_of.get(id(d)) or ([d.bins] if d.bins else []):
            nb = sum(cfg.values()) or 1
            for t in cfg:
                wts.setdefault(t, []).append(d.units / nb * d.unit.weight)
    by_id = {s.id: s for s in active}
    out = dict(caps)
    for (t, sid), c in caps.items():
        s = by_id.get(sid)
        if not c or not s or not s.max_load or not wts.get(t):
            continue
        wb = max(wts[t]) if stat == "max" else sum(wts[t]) / len(wts[t])
        if wb > 0:
            out[(t, sid)] = max(0, min(c, int(math.floor(s.max_load / wb + EPS))))
    return out


def _finish(plan, floor, structs, active, sol, caps, types, demands, prm, parents) -> None:
    """Resumo por estrutura, posicionamento/endereçamento e resumo por tipo de bin a partir da solução."""
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
    end_caps = sol.get("caps", [])

    # ---- linhas de resumo ----
    narrow = {s.id: sum(math.floor(ln.length / s.width + EPS) for z in floor.zones if z.allows(s.id) for ln in z.lanes)
              if s.width > 0 else 0 for s in active}
    for s in structs:
        line = StructLine(struct=s, qty=qty.get(s.id, 0), floor_max=narrow.get(s.id, 0))
        line.levels_by_type = {t: n for (t, sid), n in z.items() if sid == s.id and n > 0}
        plan.structs.append(line)

    # ---- posicionamento e endereçamento ----
    _place(plan, floor, active, seg_mods, z, caps, types, demands, prm, end_caps)

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


def _solve(floor: Layout, active: list[StructureSpec], need: dict[int, int], caps: dict, prm: SiteParams,
           types: Optional[dict] = None, choices: Optional[list] = None) -> dict:
    """Modelo CP-SAT: módulos por trecho de fileira e níveis por tipo de bin. `need` = bins já decididas por tipo;
    `choices` = SKUs com várias opções de bins (objetivo de custo total): o modelo escolhe uma opção por SKU."""
    types, choices = types or {}, choices or []
    info = {"status": "", "time": 0.0, "stages": []}
    zones = floor.zones
    # trechos de fileira de todas as zonas (já cortados pelos obstáculos): (zona, trecho) e comprimento (mm)
    segs = [(zi, li) for zi, zn in enumerate(zones) for li in range(len(zn.lanes))]
    glen = [int(math.floor(zones[zi].lanes[li].length * 10 + EPS)) for zi, li in segs]
    if not segs or all(v <= 0 for v in glen):
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
    per = {(s.id, g): (glen[g] // w[s.id] if zones[segs[g][0]].allows(s.id) else 0) for s in active for g in range(len(segs))}
    n = {(s.id, g): m.NewIntVar(0, per[(s.id, g)], f"n_{s.id}_{g}") for s in active for g in range(len(segs))
         if per[(s.id, g)] > 0}
    # cabeceiras: em cada ponta com acesso de cada trecho de cada faixa (fileira simples ou dupla), no máximo uma
    # estrutura girada 90°; cabem lado a lado floor(profundidade da faixa / frente do módulo). Ocupam a
    # profundidade do módulo no comprimento de todas as fileiras da faixa (todas precisam ter a ponta original,
    # sem corte de obstáculo).
    dmm = {s.id: max(1, int(round(s.depth * 10))) for s in active}
    g_of = {(zi, zones[zi].lanes[li].row, zones[zi].lanes[li].seg, e): g for g, (zi, li) in enumerate(segs)
            for e in (0, 1) if (zones[zi].lanes[li].full_start if e == 0 else zones[zi].lanes[li].full_end)}
    cu, cc = {}, {}  # (zona, faixa, trecho, ponta, estrutura) -> escolhida? / quantidade
    cmax_of, cap_lanes = {}, {}
    for zi, zn in enumerate(zones):
        if not zn.end_caps:
            continue
        for bk in zn.blocks:
            for k, ends in enumerate(zn.seg_ends):
                for e, area in enumerate(ends):
                    gs = [g_of.get((zi, r, k, e)) for r in bk.rows]
                    if not area or any(g is None for g in gs):
                        continue
                    cap_lanes[(zi, bk.id, k, e)] = gs
                    for s in active:
                        cmax = int(math.floor(bk.depth / s.width + EPS)) if s.width > 0 else 0
                        if cmax <= 0 or dmm[s.id] > min(glen[g] for g in gs) or not zn.allows(s.id):
                            continue
                        key = (zi, bk.id, k, e, s.id)
                        cmax_of[key] = cmax
                        cu[key] = m.NewBoolVar(f"cu_{zi}_{bk.id}_{k}_{e}_{s.id}")
                        cc[key] = m.NewIntVar(0, cmax, f"cc_{zi}_{bk.id}_{k}_{e}_{s.id}")
                        m.Add(cc[key] <= cmax * cu[key])
                        m.Add(cc[key] >= cu[key])
                    m.Add(sum(cu[kk] for kk in cu if kk[:4] == (zi, bk.id, k, e)) <= 1)
    x = {}
    for s in active:
        floor_cap = sum(per[(s.id, g)] for g in range(len(segs))) + sum(v for kk, v in cmax_of.items() if kk[4] == s.id)
        hi = floor_cap if s.mode != "auto" or s.max_qty is None else min(floor_cap, s.max_qty)
        x[s.id] = m.NewIntVar(0, max(0, hi), f"x_{s.id}")
        m.Add(x[s.id] == sum(v for (sid, _g), v in n.items() if sid == s.id)
              + sum(v for kk, v in cc.items() if kk[4] == s.id))
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
        u = m.NewIntVar(0, max(0, glen[g]), f"u_{g}")
        m.Add(u == sum(w[s.id] * n[(s.id, g)] for s in active if (s.id, g) in n)
              + sum(dmm[kk[4]] * v for kk, v in cu.items() if g in cap_lanes[kk[:4]]))
        used.append(u)
    # simetria entre trechos equivalentes (mesma zona, profundidade de faixa, trecho, comprimento e pontas):
    # os primeiros enchem primeiro. Faixas diferentes (simples × dupla) e trechos cortados não se comparam.
    classes: dict = {}
    for g, (zi, li) in enumerate(segs):
        zn, ln = zones[zi], zones[zi].lanes[li]
        classes.setdefault((zi, zn.blocks[zn.rows[ln.row].block].depth if zn.blocks else 0, ln.seg, glen[g],
                            ln.start, ln.end, ln.full_start, ln.full_end), []).append(g)
    for gs in classes.values():
        for g1, g2 in zip(gs, gs[1:]):
            m.Add(used[g1] >= used[g2])
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
                z[(t, s.id)] = m.NewIntVar(0, s.levels * max(1, sum(per[(s.id, g)] for g in range(len(segs)))
                                                               + sum(v for kk, v in cmax_of.items() if kk[4] == s.id)),
                                           f"z_{t}_{s.id}")
        m.Add(sum(v for (t, sid), v in z.items() if sid == s.id) <= s.levels * x[s.id])
    short = {}
    for t in all_t:
        short[t] = m.NewIntVar(0, need_ub[t], f"short_{t}")
        m.Add(sum(caps[(t, sid)] * v for (tt, sid), v in z.items() if tt == t) + short[t] >= need_ex[t])

    cost_c = {s.id: int(round((s.cost or 0) * 100)) for s in active}
    total_mods = sum(x.values())
    front = sum(used)  # comprimento de fileira ocupado (mm), cabeceiras inclusive
    bin_c = {t: int(round(((types[t].cost if t in types else 0) or 0) * 100)) for t in all_t}
    st_cost = sum(cost_c[s.id] * x[s.id] for s in active)
    bins_cost = sum(bin_c[t] * need_ex[t] for t in all_t)
    levels_used = sum(z.values()) if z else 0
    exprs = {"modules": total_mods, "area": front, "cost": st_cost + bins_cost, "structures_cost": st_cost,
             "bins_cost": bins_cost, "bins": sum(need_ex[t] for t in all_t), "levels": levels_used}
    stages = [("short", sum(short.values()) if short else 0)] + [(c, exprs[c]) for c in prm.order]

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 8
    deadline = t0 + prm.time_limit if prm.time_limit else math.inf
    all_optimal, ok = True, False
    skipped: list[str] = []
    sol: dict = {"qty": {}, "z": {}, "seg_mods": [], "picks": [], "caps": []}
    for i, (label, expr) in enumerate(stages):
        if isinstance(expr, int):  # critério que não depende de nenhuma decisão (ex.: bins já definidas)
            unit, div = CRIT_UNITS.get(label, ("bins", 1))
            info["stages"].append({"criterion": label, "label": SITE_CRITERIA.get(label, "Falta de bins (guardar toda a demanda)"),
                                   "value": expr, "display": expr / div, "unit": unit, "optimal": True, "fixed": True})
            continue
        left = deadline - time.monotonic()
        if left <= 0.05:
            skipped += [SITE_CRITERIA.get(lb, lb) for lb, _e in stages[i:]]
            all_optimal = False
            break
        m.Minimize(expr)
        if left != math.inf:
            solver.parameters.max_time_in_seconds = max(0.1, left / (len(stages) - i) if i < len(stages) - 1 else left)
        else:
            solver.parameters.max_time_in_seconds = 10 ** 9  # sem limite
        st = solver.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if not ok:
                if st == cp_model.UNKNOWN:  # não provou inviável: o tempo acabou antes de achar uma solução
                    return {"info": {**info, "status": "sem solução no tempo limite", "time": time.monotonic() - t0},
                            "error": "O solver não encontrou nenhum plano no tempo limite. Aumente o tempo ou marque "
                                     "\"Sem limite de tempo\" (etapa Objetivo)."}
                return {"info": {**info, "status": "inviável", "time": time.monotonic() - t0},
                        "error": "Não foi possível montar um plano com essas restrições (confira as quantidades "
                                 "fixas/mínimas e o tamanho do imóvel)."}
            skipped += [SITE_CRITERIA.get(lb, lb) for lb, _e in stages[i:]]
            all_optimal = False
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
               "seg_mods": [{s.id: solver.Value(n[(s.id, g)]) for s in active if (s.id, g) in n}
                            for g in range(len(segs))],
               "picks": [next(k for k, v in enumerate(vs) if solver.Value(v)) for vs in y],
               "caps": [(kk, solver.Value(v)) for kk, v in cc.items() if solver.Value(v)]}
        m.ClearHints()
        for k, v in n.items():
            m.AddHint(v, sol["seg_mods"][k[1]].get(k[0], 0))
        for k, v in z.items():
            m.AddHint(v, sol["z"][k])
        cap_val = dict(sol["caps"])
        for kk, v in cc.items():
            m.AddHint(v, cap_val.get(kk, 0))
            m.AddHint(cu[kk], 1 if cap_val.get(kk, 0) else 0)
        for vs, k in zip(y, sol["picks"]):
            for j, v in enumerate(vs):
                m.AddHint(v, 1 if j == k else 0)
    info["status"] = "ótimo comprovado" if all_optimal else "melhor solução no tempo limite (não comprovada ótima)"
    info["optimal"] = all_optimal
    info["unlimited"] = not prm.time_limit
    info["skipped"] = skipped  # critérios que não chegaram a ser otimizados (o tempo acabou antes)
    info["time"] = time.monotonic() - t0
    sol["seg_mods"] = list(zip(segs, sol["seg_mods"])) if sol["seg_mods"] else []
    return {"info": info, **sol}


def _bay_key(m) -> tuple:
    """Ordem das estantes no corredor: numeradas primeiro (01, 02…), depois as cabeceiras (C01…)."""
    b = m.bay or ""
    return (m.aisle, b[:1].isalpha(), int("".join(ch for ch in b if ch.isdigit()) or 0))


def _place(plan: SitePlan, floor: Layout, active: list[StructureSpec], seg_mods: list, z: dict, caps: dict,
           types: dict[int, BinSpec], demands: list[Demand], prm: SiteParams, end_caps: Optional[list] = None) -> None:
    from services.addressing import build_address

    by_id = {s.id: s for s in active}
    zones = floor.zones
    align = plan.building.cap_align if plan.building.cap_align in CAP_ALIGN else "row"
    lv_of = lambda s: [LevelPlaced(n=i + 1) for i in range(s.levels)]  # noqa: E731
    # cabeceiras: (zona, faixa, trecho, ponta) -> (estrutura, qtd); a da ponta inicial empurra as fileiras do trecho
    cap_at = {kk[:4]: (by_id[kk[4]], q) for kk, q in (end_caps or []) if kk[4] in by_id}
    # módulos das fileiras: em cada trecho, as estruturas em ordem de código
    row_end: dict[tuple, float] = {}  # (zona, fileira, trecho) -> fim dos módulos no trecho com a ponta final original
    for (zi, li), mods in seg_mods:
        zn = zones[zi]
        ln = zn.lanes[li]
        row = zn.rows[ln.row]
        start = cap_at.get((zi, row.block, ln.seg, 0)) if ln.full_start else None
        x = ln.x0 + (start[0].depth if start else 0.0)
        for sid in sorted(mods, key=lambda i: by_id[i].code):
            for _ in range(mods[sid]):
                s = by_id[sid]
                plan.modules.append(ModulePlaced(struct=s, row=row, x0=x, levels=lv_of(s), segment=ln.seg, zone=zn))
                x += s.width
        if ln.full_end:
            row_end[(zi, ln.row, ln.seg)] = x
    # cabeceiras: no início do trecho, antes das fileiras; no fim, encostadas na fileira mais longa da faixa
    # ("row") ou alinhadas à borda do trecho ("edge")
    caps_mods = []
    for (zi, bid, k, e), (s, q) in sorted(cap_at.items()):
        zn = zones[zi]
        bk = zn.blocks[bid]
        if e == 0:
            x0 = zn.segments[k][0]
        elif align == "row":
            x0 = max([row_end.get((zi, r, k), zn.segments[k][0]) for r in bk.rows] + [zn.segments[k][0]])
            x0 = min(x0, zn.segments[k][1] - s.depth)
        else:
            x0 = zn.segments[k][1] - s.depth
        row = zn.rows[bk.rows[0]]
        for i in range(q):
            caps_mods.append(ModulePlaced(struct=s, row=row, x0=x0, levels=lv_of(s), rotated=True,
                                          y0=bk.y + i * s.width, faces=zn.seg_ends[k][e], segment=k, zone=zn))
    # endereço. Modo "aisle": a letra é do corredor (única em toda a planta, zona por zona) e a estante é ímpar de um
    # lado e par do outro, na ordem ao longo do corredor. Modo "row": a letra é da rua (fileira), e as fileiras de
    # frente uma para a outra têm letras diferentes; estantes 01, 02, 03… ao longo da fileira
    akey = lambda m: (m.zone.index if m.zone else 0, m.row.aisle)  # noqa: E731
    aisles_used = sorted({akey(m) for m in plan.modules + caps_mods})
    if plan.building.address_mode == "row":
        rkey = lambda m: (m.zone.index if m.zone else 0, m.row.y, m.row.index)  # noqa: E731
        rows_used = sorted({rkey(m) for m in plan.modules + caps_mods})
        row_code = {r: _letters(i) for i, r in enumerate(rows_used)}
        for r in rows_used:
            mods = [m for m in plan.modules if rkey(m) == r]
            # serpentina: a rua do lado "R" do corredor começa onde a do lado "L" termina (o separador desce uma rua e
            # volta pela outra, sem voltar ao início vazio)
            rev = plan.building.serpentine and bool(mods) and mods[0].row.side == "R"
            mods.sort(key=lambda m: m.x0, reverse=rev)
            for i, m in enumerate(mods):
                m.aisle, m.bay = row_code[r], f"{i + 1:02d}"
            caps_r = sorted([m for m in caps_mods if rkey(m) == r], key=lambda m: (m.x0, m.y0), reverse=rev)
            for i, m in enumerate(caps_r):
                m.aisle, m.bay = row_code[r], f"C{i + 1:02d}"
        # rótulo de cada corredor: as letras das ruas dos dois lados (ex.: "B | C")
        side = {}
        for m in plan.modules + caps_mods:
            side.setdefault(akey(m), {}).setdefault(m.row.side, set()).add(m.aisle)
        aisle_code = {a: " | ".join(sorted(side[a].get("L", set())) + sorted(side[a].get("R", set()))) for a in aisles_used}
    else:
        aisle_code = {a: _letters(i) for i, a in enumerate(aisles_used)}
        for a in aisles_used:
            for side, start in (("L", 1), ("R", 2)):
                mods = sorted([m for m in plan.modules if akey(m) == a and m.row.side == side], key=lambda m: m.x0)
                for i, m in enumerate(mods):
                    m.aisle, m.bay = aisle_code[a], f"{start + 2 * i:02d}"
        # cabeceiras: no corredor da 1ª fileira da faixa, estantes C01, C02… (ao longo da fileira, depois na transversal)
        for a in aisles_used:
            mods = sorted([m for m in caps_mods if akey(m) == a], key=lambda m: (m.x0, m.y0))
            for i, m in enumerate(mods):
                m.aisle, m.bay = aisle_code[a], f"C{i + 1:02d}"
    plan.modules += caps_mods
    plan.modules.sort(key=_bay_key)
    plan.aisle_codes = {a: c for a, c in aisle_code.items()}

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
    ref = ref_point(plan, (prm.slotting or {}))
    plan.slotting["ref"] = ref
    mdist = {id(m): _walk_m(access_point(m), ref) for m in plan.modules}
    sl = prm.slotting if prm.slotting and prm.slotting.get("on") and any(d.pop for d in demands) else None
    golden_m = float((sl or {}).get("golden_m") or 0.0)

    def ease(lv) -> float:  # menor = melhor: caminhada + penalidade fora da zona de ouro (quando a estrutura tem uma)
        m = owner[id(lv)]
        out_gold = m.struct.golden_from is not None and not m.struct.is_golden(lv.n)
        return mdist[id(m)] + (golden_m if out_gold else 0.0)

    mpt = {id(m): access_point(m) for m in plan.modules}
    partners: dict[str, dict] = {}  # itens que saem juntos (todos os tipos de bin): sku -> {parceiro: força}
    for (a, b_), st in ((sl or {}).get("affinity") or {}).items():
        if st >= AFFINITY_GROUP:
            partners.setdefault(a, {})[b_] = st
            partners.setdefault(b_, {})[a] = st
    placed_pts: dict[str, list] = {}  # SKU -> pontos das posições já ocupadas

    for t in sorted({t for d in demands for t in (d.bins or {})}, key=lambda k: types[k].code):
        slots = sorted([lv for m in plan.modules for lv in m.levels if lv.type_id == t],
                       key=lambda lv: (lv.n, _bay_key(owner[id(lv)])))
        order = sorted([d for d in demands if (d.bins or {}).get(t)], key=lambda d: (d.material, d.sku))
        if sl:  # slotting: melhores posições primeiro; SKUs mais procurados (por bin) primeiro, vizinhos de quem sai junto
            # faixas de facilidade (SLOT_BUCKET_M): dentro de cada faixa, na ordem das estantes, para que posições
            # seguidas na lista sejam vizinhas na planta (itens que saem juntos ficam lado a lado)
            slots.sort(key=lambda lv: (math.floor(ease(lv) / SLOT_BUCKET_M), _bay_key(owner[id(lv)]), lv.n))
            order = _slot_order(order, sl.get("affinity") or {})
        si = 0
        for d in order:
            k = d.bins[t]
            total_bins = d.total_bins or 1
            units_left = round(d.units * k / total_bins)
            unit_w = d.unit.weight or 0.0
            per_bin_units = units_left / k if k else 0
            w_bin = math.ceil(per_bin_units - EPS) * unit_w  # peso máximo de uma bin (unidades arredondadas para cima)

            def room(lv) -> int:  # bins que ainda cabem no nível (capacidade e carga máxima)
                free = lv.cap - lv.used
                s_ = owner[id(lv)].struct
                if prm.enforce_weight and s_.max_load and w_bin > 0:
                    free = min(free, max(0, int((s_.max_load - lv.load) // w_bin)))
                return free

            while k > 0:
                while si < len(slots) and slots[si].used >= slots[si].cap:  # níveis cheios saem da frente
                    si += 1
                # 1º nível (a partir do primeiro não cheio) que comporta a bin: um nível barrado só pelo peso continua
                # disponível para bins mais leves
                j = next((j for j in range(si, len(slots)) if room(slots[j]) > 0), None)
                if j is None:  # acabaram os níveis do tipo (ex.: limite de carga): usa um nível livre compatível
                    extra = next((lv for m in plan.modules for lv in sorted(m.levels, key=lambda v: v.n)
                                  if lv.type_id is None and caps.get((t, m.struct.id), 0) > 0), None)
                    if extra is None:
                        break
                    extra.type_id, extra.cap = t, caps[(t, owner[id(extra)].struct.id)]
                    slots.append(extra)
                    continue
                if sl and partners.get(d.sku):
                    # parceiro já posicionado: o nível com menor (caminhada até a referência + força × distância até
                    # o parceiro), isto é, equilibra ficar perto da expedição e perto de quem sai junto
                    near = [(st, q) for o, st in partners[d.sku].items() for q in placed_pts.get(o, ())]
                    if near:
                        best = None
                        for jj in range(j, len(slots)):
                            if room(slots[jj]) <= 0:
                                continue
                            pt = mpt[id(owner[id(slots[jj])])]
                            sc = ease(slots[jj]) + min(st * (abs(pt[0] - q[0]) + abs(pt[1] - q[1])) / 100.0 for st, q in near)
                            if best is None or sc < best[0] - EPS:
                                best = (sc, jj)
                        j = best[1] if best else j
                lv = slots[j]
                free = room(lv)
                s = owner[id(lv)].struct
                take = min(free, k)
                units = round(per_bin_units * take) if k > take else units_left
                mod = owner[id(lv)]
                pos = f"{len(lv.positions) + 1:02d}"
                lvl = str(lv.n)
                lv.positions.append(Position(build_address(mod.aisle, mod.bay, lvl, pos), mod.aisle, mod.bay, lvl, pos,
                                             s.code, t, take, d.sku, d.name, units, units * unit_w,
                                             dist_m=round(mdist[id(mod)], 1), golden=s.is_golden(lv.n)))
                lv.used += take
                lv.load += units * unit_w
                placed_pts.setdefault(d.sku, []).append(mpt[id(mod)])
                d.placed_bins += take
                k -= take
                units_left -= units
    for d in demands:  # depois de todos os tipos (um SKU pode usar mais de um tipo de bin)
        if d.bins and d.placed_bins < d.total_bins and d.status == "ok":
            d.status = "falta"
            d.note = f"{d.total_bins - d.placed_bins} de {d.total_bins} bin(s) sem lugar."
    for d in demands:
        if d.status == "ok" and d.total_bins and len({p.address for p in plan.positions if p.sku == d.sku}) > 1:
            d.note = "Dividido em mais de uma posição."
    slotting_kpis(plan, demands, prm, bool(sl))


SLOT_BUCKET_M = 3.0  # largura (m) das faixas de facilidade na ordem das posições do slotting
WALK_GOLDEN_SHARE = 0.2  # zona de ouro da caminhada sem limite informado: as posições mais perto com 20% das bins


# --------------------------------------------------------------------------- #
# Posição dos SKUs (slotting): popularidade, caminhada até a expedição, zona de ouro e itens que saem juntos
# --------------------------------------------------------------------------- #
def ref_point(plan: SitePlan, sl: dict) -> dict:
    """Ponto de referência da separação (onde os pedidos são entregues): um setor (por padrão o de expedição),
    o centro da faixa das docas ou um X/Y informado. {"x", "y" (cm), "label"}."""
    b, fl = plan.building, plan.floor
    L, W = fl.length_cm or b.length_m * 100.0, fl.width_cm or b.width_m * 100.0
    gap, front = b.wall_gap_m * 100.0, b.front_reserved_m * 100.0
    spec = sl.get("ref") or "auto"
    sectors = getattr(fl, "sectors", []) or []
    center = lambda o: (o.x_m * 100 + o.length_m * 50, o.y_m * 100 + o.width_m * 50)  # noqa: E731
    if spec == "xy" and sl.get("ref_xy"):
        x, y = sl["ref_xy"]
        return {"x": x * 100.0, "y": y * 100.0, "label": f"X {x:g} m, Y {y:g} m".replace(".", ",")}
    if spec.startswith("sector:"):
        name = spec[7:]
        o = next((o for o in sectors if o.name == name), None)
        if o is not None:
            x, y = center(o)
            return {"x": x, "y": y, "label": f"setor {o.name}"}
    if spec in ("auto",) or spec.startswith("sector:"):
        o = next((o for o in sectors if "exped" in o.name.casefold()), None)
        if o is not None:
            x, y = center(o)
            return {"x": x, "y": y, "label": f"setor {o.name}"}
    if front > 0:
        return {"x": gap + front / 2, "y": W / 2, "label": "docas (centro da faixa da frente)"}
    return {"x": 0.0, "y": W / 2, "label": "frente do imóvel (meio da parede das docas)"}


def access_point(m: ModulePlaced) -> tuple[float, float]:
    """Onde o separador para em frente ao módulo: meio da frente, no corredor (cabeceira: o centro do módulo)."""
    if m.rotated or m.zone is None:
        X, Y, w, d = m.grect
        return X + w / 2, Y + d / 2
    r = m.row
    y = r.y + r.depth if r.side == "L" else r.y  # fileira "L" fica antes do corredor
    X, Y, _, _ = m.zone.to_global(m.x0 + m.struct.width / 2, y, 0, 0)
    return X, Y


def _walk_m(p: tuple[float, float], ref: dict) -> float:
    """Caminhada aproximada (m): em L (distância de Manhattan), como pelos corredores e pela faixa das docas."""
    return (abs(p[0] - ref["x"]) + abs(p[1] - ref["y"])) / 100.0


def _slot_order(order: list[Demand], affinity: dict) -> list[Demand]:
    """SKUs na ordem de preenchimento (as melhores posições primeiro).
    - Popularidade por bin: quem é muito procurado e ocupa pouco vem antes.
    - Itens que saem juntos (força >= AFFINITY_GROUP) formam grupos de até 6 SKUs, que entram em sequência (posições
      vizinhas). O grupo é ordenado pela popularidade por bin do conjunto, então um item pouco procurado só vai para
      perto da expedição quando sai quase sempre junto com um muito procurado."""
    dens = {d.sku: (d.pop or 0.0) / (d.total_bins or 1) for d in order}
    ranked = sorted(order, key=lambda d: (-dens[d.sku], d.sku))
    if not affinity:
        return ranked
    by_sku = {d.sku: d for d in ranked}
    parent = {s: s for s in by_sku}
    size = {s: 1 for s in by_sku}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), st in sorted(affinity.items(), key=lambda kv: -kv[1]):
        if st < AFFINITY_GROUP or a not in by_sku or b not in by_sku:
            continue
        ra, rb = find(a), find(b)
        if ra != rb and size[ra] + size[rb] <= 6:
            parent[rb] = ra
            size[ra] += size[rb]
    groups: dict[str, list] = {}
    for d in ranked:
        groups.setdefault(find(d.sku), []).append(d)
    gdens = {r: sum(d.pop or 0.0 for d in g) / max(1, sum(d.total_bins or 1 for d in g)) for r, g in groups.items()}
    return [d for r in sorted(groups, key=lambda r: (-gdens[r], groups[r][0].sku)) for d in groups[r]]


AFFINITY_GROUP = 0.3  # força mínima (fração dos pedidos do menos vendido em que saem juntos) para agrupar


def classify_abc(demands: list[Demand]) -> None:
    """Curva ABC pela popularidade: A = SKUs que somam até 80% das saídas, B = até 95%, C = o resto."""
    ranked = sorted([d for d in demands if d.pop], key=lambda d: -d.pop)
    total = sum(d.pop for d in ranked) or 1.0
    acc = 0.0
    for d in ranked:
        d.abc = "A" if acc < 0.80 * total else "B" if acc < 0.95 * total else "C"
        acc += d.pop
    for d in demands:
        if not d.pop:
            d.abc = "C" if ranked else ""


def slotting_kpis(plan: SitePlan, demands: list[Demand], prm: SiteParams, used: bool) -> None:
    """Indicadores da posição dos SKUs: caminhada média por saída (ponderada pela popularidade) comparada à de uma
    posição ao acaso (média de todas as posições usadas), parcela das saídas na zona de ouro e distância entre os
    pares que mais saem juntos."""
    sl = prm.slotting or {}
    k = plan.slotting
    k.update({"used": used, "source": sl.get("source", ""), "metric": sl.get("metric", ""),
              "golden_m": sl.get("golden_m")})
    classify_abc(demands)
    pos = plan.positions
    if not pos:
        return
    by_sku: dict[str, list] = {}
    for p in pos:
        by_sku.setdefault(p.sku, []).append(p)
    nbins = sum(p.bins for p in pos)
    k["random_dist"] = sum(p.dist_m * p.bins for p in pos) / nbins if nbins else None
    has_gold = any(p.golden for p in pos)
    k["golden_positions"] = sum(p.bins for p in pos if p.golden) / nbins * 100 if nbins and has_gold else None
    # zona de ouro da CAMINHADA: até X m da referência (informado) ou, sem valor, as posições mais perto que somam
    # WALK_GOLDEN_SHARE das bins
    lim = sl.get("walk_golden_m")
    if not lim:
        acc, lim = 0, 0.0
        for p in sorted(pos, key=lambda p: p.dist_m):
            acc += p.bins
            lim = p.dist_m
            if acc >= WALK_GOLDEN_SHARE * nbins:
                break
    for p in pos:
        p.walk_golden = p.dist_m <= lim + EPS
    k["walk_golden_m"], k["walk_golden_auto"] = lim, not sl.get("walk_golden_m")
    k["walk_positions"] = sum(p.bins for p in pos if p.walk_golden) / nbins * 100 if nbins else None
    popd = [d for d in demands if d.pop and d.sku in by_sku]
    tot = sum(d.pop for d in popd)
    if not tot:
        return
    dist = {s: sum(p.dist_m * p.bins for p in ps) / max(1, sum(p.bins for p in ps)) for s, ps in by_sku.items()}
    share = lambda ps, f: sum(p.bins for p in ps if f(p)) / max(1, sum(p.bins for p in ps))  # noqa: E731
    gold = {s: share(ps, lambda p: p.golden) for s, ps in by_sku.items()}
    walk = {s: share(ps, lambda p: p.walk_golden) for s, ps in by_sku.items()}
    both = {s: share(ps, lambda p: p.golden and p.walk_golden) for s, ps in by_sku.items()}
    k["avg_dist"] = sum(d.pop * dist[d.sku] for d in popd) / tot
    k["golden_picks"] = sum(d.pop * gold[d.sku] for d in popd) / tot * 100 if has_gold else None
    k["walk_picks"] = sum(d.pop * walk[d.sku] for d in popd) / tot * 100
    k["both_picks"] = sum(d.pop * both[d.sku] for d in popd) / tot * 100 if has_gold else None
    k["abc"] = {c: sum(1 for d in demands if d.abc == c) for c in "ABC"}
    a = [d for d in popd if d.abc == "A"]
    k["a_dist"] = sum(dist[d.sku] for d in a) / len(a) if a else None
    # pares que mais saem juntos: distância entre as posições mais próximas dos dois SKUs
    owner_pt = {}
    for m in plan.modules:
        pt = access_point(m)
        for lv in m.levels:
            for p in lv.positions:
                owner_pt.setdefault(p.sku, []).append(pt)
    pairs = []
    counts = sl.get("pair_counts") or {}
    aff = sl.get("affinity") or {}
    for (sa, sb), c in sorted(counts.items(), key=lambda kv: -kv[1])[:30]:  # os pares mais frequentes
        if sa in owner_pt and sb in owner_pt:
            dmin = min(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p in owner_pt[sa] for q in owner_pt[sb]) / 100.0
            pairs.append({"a": sa, "b": sb, "strength": aff.get((sa, sb), 0.0), "count": c, "dist": dmin})
    k["pairs"] = pairs[:15]
    if pairs:
        k["pairs_avg"] = sum(x["dist"] for x in pairs) / len(pairs)
        # referência: distância média entre duas posições quaisquer do plano (amostra fixa, para ser reproduzível)
        import random as _rnd

        pts = [access_point(m) for m in plan.modules if any(lv.positions for lv in m.levels)]
        rg = _rnd.Random(7)
        smp = [(rg.choice(pts), rg.choice(pts)) for _ in range(2000)] if len(pts) > 1 else []
        k["pairs_random"] = (sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in smp) / len(smp) / 100.0) if smp else None


# --------------------------------------------------------------------------- #
# Planta em SVG (vista de cima)
# --------------------------------------------------------------------------- #
PALETTE = ["#2f7ed8", "#e8743b", "#19a979", "#ed4a7b", "#945ecf", "#13a4b4", "#c3a32c", "#6f7c87"]


SHAPE_STYLE = {  # tipo -> (preenchimento, contorno, tracejado)
    "sector": ("#e3f4e8", "#3b8c5a", "5 3"),
    "reserved": ("#fff3cd", "#e0c068", ""), "avenue": ("#e3ecf7", "#b9cbe3", ""), "aisle": ("#eef2f4", "none", ""),
    "cross": ("#eef2f4", "none", ""), "lane": ("none", "#c9d1d6", "3 2"), "obstacle": ("#2b2f33", "#111111", ""),
    "zone": ("none", "#6f8fb8", "6 3"),
}


def aisle_labels(plan: SitePlan) -> list[tuple]:
    """[(texto, X, Y, vertical?)] no centro de cada corredor usado. Corredores de borda (que usam a avenida ou a
    faixa reservada) não entram aqui: o código vai no rótulo da área (ver `area_label`)."""
    out = []
    for (zi, ai), code in plan.aisle_codes.items():
        if zi >= len(plan.floor.zones):
            continue
        name = aisle_name(plan, code)
        zn = plan.floor.zones[zi]
        y, wdt = zn.aisles[ai]
        if wdt <= 0:
            if "circulação" not in (zn.edge_names.get(ai) or ""):
                continue  # avenida/docas: o código vai no rótulo da área
            # borda da zona de frente para a circulação: o rótulo fica do lado de fora
            yy = y - zn.aisle_cm / 2 if y <= zn.cross_lo + EPS else y + zn.aisle_cm / 2
            X, Y, _, _ = zn.to_global(zn.x_start + 20, yy, 0, 0)
            out.append((name, X, Y, zn.rot))
            continue
        X, Y, _, _ = zn.to_global(zn.x_start + 20, y + wdt / 2, 0, 0)
        out.append((name, X, Y, zn.rot))
    return out


def _same_area(edge: str, label: str) -> bool:
    e, lb = edge.lower(), label.lower()
    if lb.startswith("docas"):
        return "frente" in e
    if lb.startswith("área reservada"):
        return "fundo" in e
    return e.endswith(lb)


def aisle_name(plan: SitePlan, code: str) -> str:
    """"Corredor B" (letra por corredor) ou "Ruas B | C" / "Rua A" (letra por rua: as ruas dos dois lados)."""
    if plan.building.address_mode == "row":
        return ("Ruas " if "|" in code else "Rua ") + code
    return f"Corredor {code}"


def area_label(plan: SitePlan, label: str) -> str:
    """Rótulo de avenida/faixa reservada + os corredores de borda que usam a área (ex.: "Avenida 1 · corredor B")."""
    codes = [code for (zi, ai), code in sorted(plan.aisle_codes.items(), key=lambda kv: kv[1])
             if zi < len(plan.floor.zones) and plan.floor.zones[zi].aisles[ai][1] <= 0
             and _same_area(plan.floor.zones[zi].edge_names.get(ai) or "", label)]
    word = "rua" if plan.building.address_mode == "row" else "corredor"
    return label + (f" · {word} {', '.join(codes)}" if codes else "")


DIM_MODES = {"none": "Ocultar medidas", "summary": "Resumo (C × L)", "full": "Medidas ao longo da planta"}
DIM_KINDS = ("zone", "obstacle", "sector", "reserved", "avenue", "aisle", "cross")  # áreas que entram nas cotas encadeadas


def dim_mode(v) -> str:
    return v if v in DIM_MODES else "summary"


def _chain(points, total: float, min_gap: float) -> list[float]:
    """Pontos de cota de 0 a `total` (cm), sem trechos menores que `min_gap` (o rótulo não caberia)."""
    pts = sorted({round(min(max(p, 0.0), total), 1) for p in points})
    out = [0.0]
    for p in pts:
        if p - out[-1] >= min_gap and total - p >= min_gap:
            out.append(p)
    out.append(float(total))
    return out


def dim_chains(fl, min_gap_x: float, min_gap_y: float) -> tuple[list[float], list[float]]:
    """Cotas encadeadas da planta (cm): bordas de zonas, obstáculos, faixas, avenidas e corredores, ao longo do
    comprimento (X) e da largura (Y)."""
    xs, ys = [], []
    for kind, X, Y, w, d, _ in fl.shapes():
        if kind in DIM_KINDS and w > 0 and d > 0:
            xs += [X, X + w]
            ys += [Y, Y + d]
    return _chain(xs, fl.length_cm, min_gap_x), _chain(ys, fl.width_cm, min_gap_y)


DIM_POSITIONS = {"out": "Por fora da planta", "in": "Por dentro (em cada zona e obstáculo)"}


def dim_pos(v) -> str:
    return v if v in DIM_POSITIONS else "out"


def inner_dims(fl, off: float, min_len: float) -> list[tuple]:
    """Cotas por dentro da planta (cm): comprimento e largura de cada zona e obstáculo, rente à borda de baixo e à da
    direita (o nome fica no canto de cima). [(x1, y1, x2, y2, rótulo, vertical?, "zone" | "obstacle")]; lados menores que
    `min_len` ficam sem cota. (Na tela de edição, a forma selecionada ganha também a distância até a vizinha.)"""
    out = []
    for kind, X, Y, w, d, _ in fl.shapes():
        if kind not in ("zone", "obstacle", "sector") or w <= 0 or d <= 0:
            continue
        if w >= min_len:
            y = Y + d - min(off, d / 2)
            out.append((X, y, X + w, y, _m(w), False, kind))
        if d >= min_len:
            x = X + w - min(off, w / 2)
            out.append((x, Y, x, Y + d, _m(d), True, kind))
    return out


def _m(cm: float) -> str:
    return f"{cm / 100:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def floor_svg(plan: SitePlan, width_px: int = 1000) -> str:
    """Planta vista de cima. As medidas vêm em camadas (classes `nk-dim-s` = resumo, `nk-dim-f` = completas) que a
    tela mostra conforme a escolha do usuário (`data-dims` no SVG; ver base.html); `data-vb-base`/`data-vb-full`
    são as áreas de desenho sem e com as cotas em volta."""
    fl, b = plan.floor, plan.building
    if not fl.length_cm or not fl.width_cm or not fl.zones:
        return ""
    sc = width_px / fl.length_cm
    W, H = fl.length_cm * sc, fl.width_cm * sc
    color = {s.struct.id: PALETTE[i % len(PALETTE)] for i, s in enumerate(plan.structs)}
    vb_base = f"-2 -2 {W + 4:.1f} {H + 22:.1f}"
    vb_full = f"-40 -40 {W + 44:.1f} {H + 60:.1f}"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb_base}" data-vb-base="{vb_base}" data-vb-full="{vb_full}" '
           f'data-dims="summary" data-dims-pos="out" class="w-100 nk-floor" style="max-height:70vh" font-family="sans-serif">',
           f'<rect x="0" y="0" width="{W:.1f}" height="{H:.1f}" fill="#fbfcfd" stroke="#1f2d33" stroke-width="2"/>']
    full = []  # camada de medidas completas (desenhada por cima)
    for kind, X, Y, w, d, label in fl.shapes():
        fill, stroke, dash = SHAPE_STYLE.get(kind, ("none", "#999", ""))
        out.append(f'<rect x="{X * sc:.1f}" y="{Y * sc:.1f}" width="{w * sc:.1f}" height="{d * sc:.1f}" fill="{fill}" '
                   f'stroke="{stroke}"' + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
        size = f"{_m(w)} × {_m(d)} m"
        if label and kind == "zone":
            if w * sc < 60:  # zona estreita: nome na vertical, dentro da zona
                tx, ty = (X + w / 2) * sc, (Y + 12) * sc
                out.append(f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="9" fill="#4a6a94" '
                           f'transform="rotate(90 {tx:.1f} {ty:.1f})">{label}'
                           f'<tspan class="nk-dim-fo"> · {size}</tspan></text>')
            else:  # acima da zona; encostada no topo do imóvel: logo abaixo da borda
                ty = Y * sc - 3 if Y * sc > 14 else Y * sc + 11
                out.append(f'<text x="{(X + 15) * sc:.1f}" y="{ty:.1f}" font-size="10" fill="#4a6a94">{label}'
                           f'<tspan class="nk-dim-fo"> · {size} · X {_m(X)} · Y {_m(Y)}</tspan></text>')
        if label and kind in ("obstacle", "sector"):
            cx, cy = (X + w / 2) * sc, (Y + d / 2) * sc
            rot = f' transform="rotate(-90 {cx:.1f} {cy:.1f})"' if kind == "sector" and d > w * 1.6 else ""
            out.append(f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="11" text-anchor="middle" dominant-baseline="middle" '
                       f'fill="{"#ffffff" if kind == "obstacle" else "#24613a"}"'
                       + (' font-weight="bold"' if kind == "sector" else "") + f'{rot}>{label}'
                       + (f'<tspan class="nk-dim-s" font-weight="normal"> · {_m(w * d / 100)} m²</tspan>' if kind == "sector" else "")
                       + '</text>')
            if d * sc >= 30:
                full.append(f'<text class="nk-dim-fo" x="{cx:.1f}" y="{cy + 12:.1f}" font-size="9" text-anchor="middle" '
                            f'dominant-baseline="middle" fill="{"#dfe5ea" if kind == "obstacle" else "#24613a"}">{size}</text>')
        if label and kind in ("reserved", "avenue"):
            label = area_label(plan, label)
            cx, cy = (X + w / 2) * sc, (Y + d / 2) * sc
            rot = f' transform="rotate(-90 {cx:.1f} {cy:.1f})"' if d > w else ""
            out.append(f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="11" text-anchor="middle" dominant-baseline="middle" '
                       f'fill="{"#8a6d1c" if kind == "reserved" else "#3b5b8c"}"{rot}>{label}'
                       f'<tspan class="nk-dim-f"> · {_m(min(w, d))} m</tspan></text>')
    for txt, X, Y, vertical in aisle_labels(plan):
        rot = f' transform="rotate(90 {X * sc:.1f} {Y * sc:.1f})"' if vertical else ""
        out.append(f'<text x="{X * sc:.1f}" y="{Y * sc:.1f}" font-size="11" fill="#5b6770" font-weight="bold" '
                   f'dominant-baseline="middle"{rot}>{txt}</text>')
    for m in plan.modules:
        used = sum(lv.used for lv in m.levels)
        cap = sum(lv.cap for lv in m.levels) or 1
        op = 0.35 + 0.65 * min(1.0, used / cap)
        title = f"{m.aisle}-{m.bay} · {m.struct.code} · {used}/{cap} bins"
        if fl.mixed and m.zone is not None:
            title += f" · {m.zone.name}"
        if m.rotated:
            title += f" · cabeceira girada 90°, de frente para {m.faces}"
        X, Y, w, d = m.grect
        out.append(f'<rect x="{X * sc:.1f}" y="{Y * sc:.1f}" width="{w * sc:.1f}" height="{d * sc:.1f}" '
                   f'fill="{color[m.struct.id]}" fill-opacity="{op:.2f}" '
                   + ('stroke="#1f2d33" stroke-width="1" stroke-dasharray="2 1"' if m.rotated else 'stroke="#fff" stroke-width="0.6"')
                   + f'><title>{title}</title></rect>')
    out += full
    # zona de ouro da caminhada: as posições até X m (em L) do ponto de referência formam um losango em volta dele
    ref, lim = (plan.slotting or {}).get("ref"), (plan.slotting or {}).get("walk_golden_m")
    if ref and lim:
        rx, ry, r = ref["x"] * sc, ref["y"] * sc, lim * 100 * sc
        out.append(f'<clipPath id="nkBld"><rect x="0" y="0" width="{W:.1f}" height="{H:.1f}"/></clipPath>'
                   f'<polygon clip-path="url(#nkBld)" points="{rx - r:.1f},{ry:.1f} {rx:.1f},{ry - r:.1f} {rx + r:.1f},{ry:.1f} '
                   f'{rx:.1f},{ry + r:.1f}" fill="rgba(245,190,40,.12)" stroke="#d4a017" stroke-width="1.5" '
                   f'stroke-dasharray="6 3"><title>Zona de ouro da caminhada: até {_m(lim * 100)} m da referência</title></polygon>'
                   f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="5" fill="#d62728" stroke="#fff" stroke-width="1.5">'
                   f'<title>Ponto de referência: {ref["label"]}</title></circle>')
    # cotas: total (externa) e encadeada (interna), em cima (X) e à esquerda (Y)
    xs, ys = dim_chains(fl, 30 / sc, 30 / sc)
    g = ['<g class="nk-dim-fo" stroke="#5b6770" stroke-width="0.8" font-size="9" fill="#1f2d33">']

    def hchain(pts, y, label_total=None):
        g.append(f'<line x1="0" y1="{y}" x2="{W:.1f}" y2="{y}"/>')
        for p in pts:
            g.append(f'<line x1="{p * sc:.1f}" y1="{y - 3}" x2="{p * sc:.1f}" y2="{y + 3}"/>')
        if label_total:
            g.append(f'<text x="{W / 2:.1f}" y="{y - 3}" text-anchor="middle" stroke="none" font-weight="bold">{label_total}</text>')
            return
        for a, c in zip(pts, pts[1:]):
            g.append(f'<text x="{(a + c) / 2 * sc:.1f}" y="{y - 3}" text-anchor="middle" stroke="none">{_m(c - a)}</text>')

    def vchain(pts, x, label_total=None):
        g.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{H:.1f}"/>')
        for p in pts:
            g.append(f'<line x1="{x - 3}" y1="{p * sc:.1f}" x2="{x + 3}" y2="{p * sc:.1f}"/>')
        items = [((0, pts[-1]), label_total)] if label_total else [((a, c), _m(c - a)) for a, c in zip(pts, pts[1:])]
        for (a, c), t in items:
            cy = (a + c) / 2 * sc
            g.append(f'<text x="{x - 3}" y="{cy:.1f}" text-anchor="middle" stroke="none" transform="rotate(-90 {x - 3} {cy:.1f})"'
                     + (' font-weight="bold"' if label_total else "") + f'>{t}</text>')

    hchain([0, fl.length_cm], -28, f"{_m(fl.length_cm)} m (comprimento)")
    hchain(xs, -10)
    vchain([0, fl.width_cm], -28, f"{_m(fl.width_cm)} m (largura)")
    vchain(ys, -10)
    g.append("</g>")
    out += g
    # cotas por dentro: tamanho de cada zona/obstáculo e distância às paredes
    g = ['<g class="nk-dim-fi" stroke="#24548c" stroke-width="0.7" font-size="9" fill="#24548c">']
    for x1, y1, x2, y2, label, vertical, kind in inner_dims(fl, 10 / sc, 24 / sc):
        X1, Y1, X2, Y2 = x1 * sc, y1 * sc, x2 * sc, y2 * sc
        ob = kind == "obstacle"  # sobre fundo escuro: linha e texto claros
        ink = ' stroke="#dfe5ea"' if ob else ""
        g.append(f'<line x1="{X1:.1f}" y1="{Y1:.1f}" x2="{X2:.1f}" y2="{Y2:.1f}"{ink}/>')
        for X, Y in ((X1, Y1), (X2, Y2)):
            g.append(f'<line x1="{X - 3 if vertical else X:.1f}" y1="{Y if vertical else Y - 3:.1f}" '
                     f'x2="{X + 3 if vertical else X:.1f}" y2="{Y if vertical else Y + 3:.1f}"{ink}/>')
        cx, cy = (X1 + X2) / 2, (Y1 + Y2) / 2
        rot = f' transform="rotate(-90 {cx - 3:.1f} {cy:.1f})"' if vertical else ""
        style = 'fill="#ffffff"' if ob else 'stroke="#fff" stroke-width="2" paint-order="stroke"'
        g.append(f'<text x="{cx - 3 if vertical else cx:.1f}" y="{cy if vertical else cy - 3:.1f}" text-anchor="middle" '
                 f'{style}{rot}>{label}</text>')
    g.append("</g>")
    out += g
    desc = (f"{len(fl.zones)} zonas" if fl.mixed else
            f'fileiras ao longo do {"comprimento" if fl.along == "length" else "largura"}')
    out.append(f'<text x="0" y="{H + 15:.1f}" font-size="11" fill="#5b6770"><tspan class="nk-dim-s" font-weight="bold" '
               f'fill="#1f2d33">{b.length_m:g} × {b.width_m:g} m · </tspan>{desc} · {len(fl.rows)} fileira(s), '
               f'{fl.real_aisles} corredor(es)'
               + (f' · {plan.end_caps} cabeceira(s) (contorno tracejado)' if plan.end_caps else '') + '</text>')
    out.append("</svg>")
    return "".join(out)


def struct_colors(plan: SitePlan) -> dict:
    return {s.struct.id: PALETTE[i % len(PALETTE)] for i, s in enumerate(plan.structs)}
