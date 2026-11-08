"""
Otimização "cega aos endereços" (pool de bins).

As bins físicas ficam onde estão (cada bin pertence a um endereço); o motor ignora a divisão por endereço e
decide em QUAIS bins cada SKU deve ficar, podendo usar qualquer quantidade das bins de qualquer endereço do
escopo (sempre dentro do mesmo depósito: filial + armazém). Resultado = plano de movimentação.

Objetivos (escolhidos pelo usuário):
  - min_bins   : usar o menor número de bins (bins de maior capacidade primeiro; o resto vai na menor bin que cabe)
  - min_volume : menor volume ocioso (bins com menor volume por unidade do SKU; resto na menor bin que cabe)
  - min_moves  : mínimo de movimentações (usa primeiro as bins dos endereços onde o SKU já está)
Em empate, prefere bins dos endereços onde o SKU já está (menos movimentação).

Algoritmo puro (sem banco) para ser testável.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from services.bin_optimizer import BinSpec, Params, ProductUnit, capacity

EPS = 1e-9
POOL_OBJECTIVES = {
    "min_bins": "Menos bins",
    "min_volume": "Menos volume de bins (menos ar)",
    "min_width": "Menor largura de frente (mais espaço livre na prateleira)",
    "min_moves": "Mínimo de movimentações (mantém o que já está nas bins)",
    "min_cost": "Menor custo de bins (R$)",
}


@dataclass
class PoolBin:
    """Uma bin física (uma unidade de LocationBin)."""
    id: int
    location_id: int
    address: str
    type_id: int
    seq: int  # nº da bin daquele tipo no endereço (1, 2, …)
    level: str = ""  # nível onde a bin está, se não for o do endereço
    used: float = 0.0  # fração ocupada (0–1) no plano
    contents: dict = field(default_factory=dict)  # sku -> unidades no PLANO
    orig: dict = field(default_factory=dict)  # sku -> unidades HOJE (conteúdo atual da bin física)
    type_code: str = ""
    locked: bool = False  # bin ocupada por um produto fora do foco: fica como está (não entra no pool)

    @property
    def label(self) -> str:
        return f"{self.address} #{self.seq}" + (f" (nível {self.level})" if self.level else "")

    @property
    def tag(self) -> str:
        """Identificação da bin física: 'CX-M #2' (+ nível extra)."""
        return f"{self.type_code or self.type_id} #{self.seq}" + (f" ↳{self.level}" if self.level else "")


@dataclass
class Move:
    """Uma movimentação física: N unidades de um SKU de uma bin (ou do endereço, se estava fora de bin) para outra."""
    sku: str
    product_id: int
    qty: float
    src_loc: Optional[int]
    src_bin: Optional[PoolBin]  # None = saldo fora de bin no endereço de origem
    dst_loc: Optional[int]
    dst_bin: Optional[PoolBin]


@dataclass
class SkuDemand:
    sku: str
    product_id: int
    unit: ProductUnit  # dimensões/peso (qty = demanda total)
    qty: float  # unidades a guardar (saldo, min ou max level)
    current: dict  # location_id -> unidades atuais (saldo) no escopo
    default_location_id: Optional[int] = None
    secondary: bool = False  # produto fora do foco, desalojado de uma bin usada pelo foco (realocação de órfãos)


@dataclass
class PoolParams:
    objective: str = "min_bins"
    fill_limit: float = 0.85
    growth: float = 0.0
    use_geometry: bool = True
    rotation: str = "upright"
    enforce_weight: bool = True
    mix_skus: bool = False
    # "existing": só as bins que existem nos endereços do escopo (são uma restrição);
    # "unlimited": bins como recurso infinito — o motor escolhe livremente quantas bins de cada tipo precisaria
    bins_mode: str = "existing"
    allowed_types: Optional[set] = None  # None = todos os tipos
    allow_split: bool = False  # permite o mesmo SKU em mais de um endereço
    single_type: bool = False  # um único tipo de bin por SKU
    max_bins: Optional[int] = None  # máximo de bins por SKU
    engine: str = "heuristic"  # "heuristic" | "or" (pesquisa operacional)
    time_limit: float = 15.0
    priority: Optional[list] = None  # ordem dos critérios ("bins", "volume", "width", "changes")
    # foco nos SKUs filtrados: "free" = só bins livres (sem saldo de outros produtos); "any" = qualquer bin
    bin_source: str = "free"
    orphans: str = "relocate"  # "relocate" = realoca os produtos desalojados; "leave" = ficam fora de bin (órfãos)
    # posição pela saída (slotting, ver services/slotting.py): {"geo": {endereço: Geo}, "pop": {sku: popularidade},
    # "abc": {sku: A/B/C}, "aff": {(a, b): força}}; fora do repr (o cache usa `slot_key`)
    slotting: Optional[dict] = field(default=None, repr=False, compare=False)
    slot_key: str = ""

    @property
    def unlimited(self) -> bool:
        return self.bins_mode == "unlimited"

    def as_params(self) -> Params:
        return Params(fill_limit=self.fill_limit, use_geometry=self.use_geometry, rotation=self.rotation,
                      enforce_weight=self.enforce_weight)


@dataclass
class SkuPlan:
    sku: str
    product_id: int
    qty: float
    needed: int  # unidades planejadas (com margem)
    current: dict  # location_id -> unidades
    allocations: list = field(default_factory=list)  # [(PoolBin, unidades)]
    shortage: int = 0  # unidades sem bin
    note: str = ""
    orig_bins: list = field(default_factory=list)  # [(PoolBin, unidades)] onde o SKU está hoje
    spread: bool = True  # divide o saldo físico entre as bins planejadas (False: mínimo de movimentações)
    unit_vol: float = 0.0  # cm³ por unidade
    secondary: bool = False  # produto desalojado e realocado (fora do foco)
    _moves: Optional[list] = None
    _phys: Optional[dict] = None

    @property
    def moves(self) -> list:
        """Movimentações bin a bin (inclusive dentro do mesmo endereço). Só o saldo existente se move."""
        if self._moves is not None:
            return self._moves
        have: dict[int, float] = {}
        bins: dict[int, PoolBin] = {}
        for b, q in self.orig_bins:
            have[b.id] = have.get(b.id, 0) + q
            bins[b.id] = b
        want: dict[int, float] = {}
        for b, q in self.allocations:
            want[b.id] = want.get(b.id, 0) + q
            bins[b.id] = b
        total = sum(self.current.values())
        if self.spread and want and total < sum(want.values()) - EPS:
            # plano maior que o saldo (min/max level ou margem): o saldo físico é dividido entre as bins planejadas
            # na proporção do planejado — nenhuma bin fica só com reserva enquanto houver unidades para dividir
            from decimal import Decimal

            from services.bin_content_service import split_units

            ids = list(want)
            parts = split_units(Decimal(str(round(total, 4))), [want[i] for i in ids])
            want = {i: float(p) for i, p in zip(ids, parts)}
        sources = []  # (loc, bin|None, qtd)
        in_bins: dict[int, float] = {}
        for bid, q in have.items():
            in_bins[bins[bid].location_id] = in_bins.get(bins[bid].location_id, 0) + q
            s = q - want.get(bid, 0)
            if s > EPS:
                sources.append((bins[bid].location_id, bins[bid], s))
        for loc, q in self.current.items():  # saldo do endereço que não está em nenhuma bin
            loose = q - in_bins.get(loc, 0)
            if loose > EPS:
                sources.append((loc, None, loose))
        deficits = {bid: q - have.get(bid, 0) for bid, q in want.items() if q - have.get(bid, 0) > EPS}
        phys = {bid: min(have.get(bid, 0), q) for bid, q in want.items()}  # o que já está e fica
        key = lambda b: (b.address, b.type_id, b.seq)  # noqa: E731
        out = []
        for loc, sb, s in sorted(sources, key=lambda x: (x[1].address if x[1] else "", x[0] or 0)):
            order = sorted(deficits, key=lambda bid: (0 if bins[bid].location_id == loc else 1, key(bins[bid])))
            for bid in order:
                if s <= EPS:
                    break
                m = min(s, deficits[bid])
                if m > EPS:
                    db_ = bins[bid]
                    # saldo sem bin conhecida indo para uma bin do MESMO endereço: só guarda, não é movimentação
                    if not (sb is None and loc == db_.location_id):
                        out.append(Move(self.sku, self.product_id, m, loc, sb, db_.location_id, db_))
                    s -= m
                    phys[bid] = phys.get(bid, 0) + m
                    deficits[bid] -= m
                    if deficits[bid] <= EPS:
                        del deficits[bid]
        self._moves = out
        self._phys = phys
        return out

    @property
    def physical_after(self) -> dict:
        """{bin id: unidades FÍSICAS do SKU na bin depois das movimentações}. Pode ser menor que o planejado
        (allocations) quando o plano usa nível mín./máx. ou margem de crescimento: a diferença é espaço reservado."""
        self.moves  # noqa: B018 (calcula)
        return self._phys or {}

    @property
    def planned(self) -> dict:
        out: dict[int, int] = {}
        for b, n in self.allocations:
            out[b.location_id] = out.get(b.location_id, 0) + n
        return out

    @property
    def transfers(self) -> list[tuple[int, int, float]]:
        """[(endereço origem, endereço destino, unidades)] — só entre endereços diferentes."""
        agg: dict[tuple, float] = {}
        for mv in self.moves:
            if mv.src_loc != mv.dst_loc:
                agg[(mv.src_loc, mv.dst_loc)] = agg.get((mv.src_loc, mv.dst_loc), 0) + mv.qty
        return [(s, d, q) for (s, d), q in agg.items()]

    @property
    def moved_units(self) -> float:
        return sum(m.qty for m in self.moves)


@dataclass
class PoolResult:
    plans: list
    bins: list
    no_dims: list  # SKUs sem dimensões (ficam onde estão; bins dos seus endereços reservadas)
    reserved_locations: set
    bins_used_before: int = 0
    bins_used_after: int = 0
    addresses_before: int = 0
    addresses_after: int = 0
    unlimited: bool = False
    by_type: dict = field(default_factory=dict)  # tipo -> {"existing": n no escopo, "needed": n no plano}
    volume_before: float = 0.0  # L das bins usadas hoje
    volume_after: float = 0.0  # L das bins usadas no plano
    width_before: float = 0.0  # cm de frente das bins usadas hoje
    width_after: float = 0.0
    units_volume: float = 0.0  # L ocupados pelas unidades (saldo físico)
    units_volume_after: float = 0.0  # L das unidades que o plano guarda em bins (sem as que ficaram sem bin)
    orphans: list = field(default_factory=list)  # [(sku, endereço, PoolBin, unidades)] que ficam fora de bin
    displaced: dict = field(default_factory=dict)  # sku -> unidades desalojadas (realocadas)
    fixed_pids: dict = field(default_factory=dict)  # SKUs fora do foco que ficam nas suas bins: sku -> produto
    focus: Optional[dict] = None  # foco nos SKUs filtrados: {"skus": n, "locked_bins": n, "free_bins": n}
    solver: Optional[dict] = None  # pesquisa operacional: status, tempo, valores por critério, comparação c/ heurística

    @property
    def shortage(self) -> bool:
        return any(p.shortage for p in self.plans)

    @property
    def freed_bins(self) -> int:
        return self.bins_used_before - self.bins_used_after

    @property
    def moves(self) -> int:
        return 0 if self.unlimited else sum(len(p.moves) for p in self.plans)

    @property
    def moved_units(self) -> float:
        return 0.0 if self.unlimited else sum(p.moved_units for p in self.plans)


def optimize_pool(bins: list[PoolBin], demands: list[SkuDemand], types: dict[int, BinSpec], prm: PoolParams,
                  reserved_locations: Optional[set] = None, no_dims: Optional[list] = None,
                  secondary: Optional[list] = None) -> PoolResult:
    """Motor escolhido: pesquisa operacional (OR-Tools CP-SAT) ou a heurística gulosa.
    `secondary` = produtos fora do foco que podem ser desalojados (bins com saldo usadas pelo foco)."""
    if prm.engine == "or":
        from services.or_engine import solve_pool

        res = solve_pool(bins, demands, types, prm, reserved_locations, no_dims)
    else:
        res = greedy_pool(bins, demands, types, prm, reserved_locations, no_dims)
    if secondary is not None:
        res = handle_displaced(res, bins, demands, secondary, types, prm, reserved_locations, no_dims)
    return res


def handle_displaced(res: PoolResult, bins, demands, secondary, types, prm: PoolParams, reserved_locations, no_dims):
    """Bins com saldo de outros produtos usadas pelo foco: esses produtos ficam órfãos (fora de bin, no mesmo
    endereço) ou são realocados — para as próprias bins que sobraram ou para bins livres, com o mínimo de
    movimentação."""
    from dataclasses import replace

    if prm.unlimited:
        return res
    focus = {d.sku for d in demands}
    displaced: dict[str, float] = {}
    hit: list = []
    for b in bins:
        if not b.contents or b.location_id in (reserved_locations or set()):
            continue
        for sku, q in b.orig.items():
            if sku not in focus and q > 0:
                displaced[sku] = displaced.get(sku, 0) + q
                hit.append((sku, b.location_id, b, q))
    if not displaced:
        return res
    if prm.orphans == "leave":
        res.orphans = hit
        return res
    sec = [replace(d, secondary=True) for d in secondary if d.sku in displaced]
    # bins disponíveis para os desalojados: as não usadas pelo foco, sem saldo de produtos que ficam
    temp = []
    for b in bins:
        if b.locked:
            continue
        if b.contents or any(sku not in focus and sku not in displaced for sku in b.orig):
            b.locked = True
            temp.append(b)
    prm2 = replace(prm, objective="min_moves", engine="heuristic", single_type=False, max_bins=None)
    res2 = greedy_pool(bins, sec, types, prm2, reserved_locations, [])
    for b in temp:
        b.locked = False
    for p in res2.plans:
        p.secondary = True
    plans = list(res.plans) + list(res2.plans)
    out = rebuild_pool_result(bins, list(demands) + sec, plans, types, prm, reserved_locations, no_dims)
    out.solver, out.displaced = res.solver, displaced
    out.orphans = [(p.sku, None, None, p.shortage) for p in res2.plans if p.shortage]
    return out


def rebuild_pool_result(bins, demands, plans, types, prm: PoolParams, reserved_locations, no_dims) -> PoolResult:
    """Recalcula os indicadores de um resultado com planos já montados."""
    reserved_locations = reserved_locations or set()
    pool = [b for b in bins if b.location_id not in reserved_locations and not b.locked]
    vol = {t: (s.volume_cm3 or 0.0) for t, s in types.items()}
    wid = {t: (s.fw or 0.0) for t, s in types.items()}
    occupied_before = {l for d in demands for l, q in d.current.items() if q > 0}
    occupied_before |= {d.default_location_id for d in demands if d.default_location_id}
    res = PoolResult([], bins, list(no_dims or []), set(reserved_locations))
    known = any(b.orig for b in pool)
    res.bins_used_before = (sum(1 for b in pool if b.orig) if known
                            else sum(1 for b in pool if b.location_id in occupied_before))
    res.addresses_before = len({b.location_id for b in pool if b.location_id in occupied_before})
    res.unlimited = prm.unlimited
    return finish_pool_result(res, bins, pool, pool, [], plans, prm, vol, wid, known, occupied_before,
                              reserved_locations)


def greedy_pool(bins: list[PoolBin], demands: list[SkuDemand], types: dict[int, BinSpec], prm: PoolParams,
                  reserved_locations: Optional[set] = None, no_dims: Optional[list] = None) -> PoolResult:
    reserved_locations = reserved_locations or set()
    base_prm = prm.as_params()
    pool = [b for b in bins if b.location_id not in reserved_locations and not b.locked]
    pool_all = list(pool)
    vol = {t: (s.volume_cm3 or 0.0) for t, s in types.items()}
    wid = {t: (s.fw or 0.0) for t, s in types.items()}

    # endereços/bins "usados antes": todo endereço com item ocupa todas as suas bins
    occupied_before = {l for d in demands for l, q in d.current.items() if q > 0}
    occupied_before |= {d.default_location_id for d in demands if d.default_location_id}
    res = PoolResult([], bins, list(no_dims or []), set(reserved_locations))
    known = any(b.orig for b in pool)
    # com o conteúdo por bin conhecido, "usada hoje" = bin com algo dentro; senão, todas as bins de endereços ocupados
    res.bins_used_before = (sum(1 for b in pool if b.orig) if known
                            else sum(1 for b in pool if b.location_id in occupied_before))
    res.addresses_before = len({b.location_id for b in pool if b.location_id in occupied_before})

    res.unlimited = prm.unlimited
    plans: list[SkuPlan] = []
    caps: dict[str, dict[int, int]] = {}
    full_caps: dict[str, dict[int, int]] = {}  # capacidade física (100%), para manter o que já está na bin
    full_prm = Params(fill_limit=1.0, use_geometry=prm.use_geometry, rotation=prm.rotation, enforce_weight=prm.enforce_weight)
    for d in demands:
        caps[d.sku] = {t: (capacity(d.unit, types[t], base_prm)
                           if prm.allowed_types is None or t in prm.allowed_types else 0) for t in types}
        full_caps[d.sku] = {t: capacity(d.unit, types[t], full_prm) for t in types}
    if prm.unlimited:
        pool = []  # as bins existentes não limitam: cada tipo tem estoque infinito de bins novas
    virtual: list[PoolBin] = []
    vseq: dict[int, int] = {}

    # Índice das bins livres: (tipo, endereço) -> bins livres; e endereços com bins livres por tipo (em ordem)
    fresh: dict[tuple[int, int], list[PoolBin]] = {}
    dem_skus = {d.sku for d in demands}

    def foreign(b) -> bool:
        return any(s_ not in dem_skus and q_ > 0 for s_, q_ in b.orig.items())
    # bins livres antes das que guardam outros produtos (só usadas com "qualquer bin")
    for b in sorted(pool, key=lambda b: (b.address, any(s not in dem_skus for s in b.orig), b.id)):
        fresh.setdefault((b.type_id, b.location_id), []).append(b)
    locs_by_type: dict[int, list[int]] = {}
    for (t, l) in fresh:
        locs_by_type.setdefault(t, []).append(l)
    partials: list[PoolBin] = []  # bins abertas e não cheias

    only = {"loc": None}  # endereço escolhido para o SKU (quando não pode dividir entre endereços)

    def take_fresh(t: int, here: set, sku: str = "") -> list[PoolBin]:
        """Candidatas livres do tipo t: uma de um endereço onde o SKU já está + uma de outro endereço.
        Modo ilimitado: sempre uma bin nova (virtual) do tipo."""
        if prm.unlimited:
            vseq[t] = vseq.get(t, 0) + 1
            return [PoolBin(-(len(virtual) * 1000 + t) - 1, None, "", t, vseq[t], type_code=types[t].code)]
        out, got_far = [], False
        far_pick = [None]
        if only["loc"] is not None:
            lst = fresh.get((t, only["loc"])) or []
            mine = [b for b in lst if sku in b.orig]
            return (mine[:2] or lst[:1])
        for l in locs_by_type.get(t, []):
            lst = fresh.get((t, l))
            if not lst:
                continue
            if l in here:
                mine = [b for b in lst if sku in b.orig]  # a bin onde o SKU já está vem primeiro
                out.extend(mine[:2] or [lst[0]])
            elif not got_far or (foreign(far_pick[0]) and not foreign(lst[0])):
                # uma candidata de outro endereço (livre, se houver)
                if got_far:
                    out.remove(far_pick[0])
                out.append(lst[0])
                far_pick[0] = lst[0]
                got_far = True
        return out

    # SKUs que precisam de mais bins primeiro (melhor escolha das bins grandes)
    def bins_needed(d: SkuDemand) -> float:
        best = max(caps[d.sku].values() or [0])
        return d.qty / best if best else math.inf
    order = sorted(demands, key=lambda d: (-bins_needed(d), d.sku))
    sl = prm.slotting if prm.slotting and prm.slotting.get("pop") and not prm.unlimited else None
    placed_locs: dict[str, set] = {}  # SKU -> endereços onde já ficou (para aproximar os que saem juntos)
    if sl:
        # posição pela saída: os mais procurados (por bin) escolhem primeiro, os que saem juntos em sequência; os
        # endereços livres de cada tipo ficam em ordem de facilidade (o "outro endereço" candidato é o mais fácil)
        from services.slotting import geo_dist, slot_order

        geo, pop, abc = sl["geo"], sl["pop"], sl.get("abc") or {}
        partners: dict[str, dict] = {}
        for (a_, b_), st in (sl.get("aff") or {}).items():
            partners.setdefault(a_, {})[b_] = st
            partners.setdefault(b_, {})[a_] = st
        need_of = {d.sku: (bins_needed(d) if bins_needed(d) != math.inf else 1e6) for d in demands}
        by_sku = {d.sku: d for d in demands}
        order = [by_sku[s_] for s_ in slot_order([d.sku for d in order], pop, need_of, sl.get("aff") or {})]
        far_ease = lambda l: geo[l].ease if l in geo else 1e9  # noqa: E731
        for t_ in locs_by_type:
            locs_by_type[t_].sort(key=far_ease)

    for d in order:
        need = int(math.ceil(d.qty * (1 + prm.growth) - EPS)) if d.qty > 0 else 0
        plan = SkuPlan(d.sku, d.product_id, d.qty, need, dict(d.current), spread=prm.objective != "min_moves",
                       unit_vol=d.unit.volume_cm3 or 0.0)
        plan.orig_bins = [(b, b.orig[d.sku]) for b in bins if d.sku in b.orig]
        plans.append(plan)
        cap = caps[d.sku]
        if need <= 0:
            continue
        if prm.single_type and any(cap.values()):
            # um único tipo de bin para o SKU: escolhe o tipo antes, pelo objetivo, entre os que têm bins livres suficientes
            free_n = {t: (math.inf if prm.unlimited else sum(len(v) for (tt, _l), v in fresh.items() if tt == t))
                      for t in cap}
            orig_types = {b.type_id for b, _q in plan.orig_bins}

            def tkey(t):
                n = math.ceil(need / cap[t])
                if prm.objective == "min_volume":
                    main = n * vol[t]
                elif prm.objective == "min_width":
                    main = n * wid[t]
                elif prm.objective == "min_cost":
                    main = n * (types[t].cost or 0.0) if t in types else n
                elif prm.objective == "min_moves":
                    main = 0 if t in orig_types else 1
                else:
                    main = n
                return (main, n, vol[t], t)
            ok_t = [t for t, c in cap.items() if c > 0 and free_n[t] * c >= need]
            if ok_t:
                chosen = min(ok_t, key=tkey)
            else:
                chosen = max((t for t, c in cap.items() if c > 0), key=lambda t: free_n[t] * cap[t])
            cap = {t: (c if t == chosen else 0) for t, c in cap.items()}
        if not any(cap.values()):
            plan.shortage, plan.note = need, "Não cabe em nenhum tipo de bin do escopo."
            continue
        here = set(d.current) | ({d.default_location_id} if d.default_location_id else set())

        def sk(l) -> float:
            """Nota do endereço para o SKU (posição pela saída): facilidade para os itens A/B (os C não disputam os
            melhores endereços) + força × distância até os parceiros já posicionados."""
            if not sl or l is None or l not in geo:
                return 0.0
            w = {"A": 1.0, "B": 0.5}.get(abc.get(d.sku, "C"), 0.0)
            v = w * geo[l].ease
            near = [(st, o) for o_, st in partners.get(d.sku, {}).items() for o in placed_locs.get(o_, ()) if o in geo]
            if near:
                v += min(st * geo_dist(geo[l], geo[o]) for st, o in near)
            return round(v, 1)
        mm = prm.objective == "min_moves"
        far2 = (lambda b: (far(b), sk(b.location_id))) if mm else (lambda b: (sk(b.location_id), far(b)))  # noqa: E731

        def eff(b: PoolBin) -> int:
            c = cap.get(b.type_id, 0)
            if not c:
                return 0
            if b.used <= EPS:
                return c
            if not prm.mix_skus and set(b.contents) != {d.sku}:
                return 0  # sem mistura: bin aberta só aceita o mesmo SKU
            return math.floor((1 - b.used) * c + EPS)

        # proximidade (0 = já está na bin, 1 = no mesmo endereço, 2 = outro) e, no empate, bin livre antes de bin
        # com saldo de outro item (só existe no foco com "qualquer bin")
        far = lambda b: (0 if d.sku in b.orig else (1 if b.location_id in here else 2)) * 2 + (1 if foreign(b) else 0)  # noqa: E731
        only["loc"] = None
        if not prm.allow_split and not prm.unlimited:
            # o SKU inteiro precisa caber em UM endereço: escolhe o endereço antes de escolher as bins
            per_loc: dict[int, list[tuple]] = {}  # endereço -> [(capacidade, bin nova?, volume)]
            for (t, l), lst in fresh.items():
                c = cap.get(t, 0)
                if c and lst:
                    per_loc.setdefault(l, []).extend([(c, 1, vol[t], wid[t], 1 if foreign(b) else 0) for b in lst])
            for b in partials:
                c = cap.get(b.type_id, 0)
                if c and (prm.mix_skus or set(b.contents) == {d.sku}):
                    e = math.floor((1 - b.used) * c + EPS)
                    if e > 0:
                        per_loc.setdefault(b.location_id, []).append((e, 0, 0.0, 0.0, 0))
            if per_loc:
                def evaluate(l):
                    # bins já abertas primeiro (não custam bin nova), depois as maiores
                    cs = sorted(per_loc[l], key=lambda x: (x[1], -x[0], x[4]))
                    total, nb, vv, ww, k, no = 0, 0, 0.0, 0.0, 0, 0
                    for c, new_bin, v, w, oc in cs:
                        if total >= need or (prm.max_bins and k >= prm.max_bins):
                            break
                        total += c
                        nb += new_bin
                        vv += v
                        ww += w
                        no += oc
                        k += 1
                    has_orig = any(d.sku in b.orig for b in pool if b.location_id == l)
                    closeness = 0 if has_orig else (1 if l in here else 2)
                    return total, nb, closeness, vv, ww, no
                ev = {l: evaluate(l) for l in per_loc}
                ok_locs = [l for l, e in ev.items() if e[0] >= need]
                addr_of = {b.location_id: b.address for b in pool}
                if ok_locs:
                    # com a posição pela saída, a nota do endereço (sk) vem antes da proximidade de hoje (ev[l][2])
                    if prm.objective == "min_moves":
                        k = lambda l: (ev[l][2], ev[l][1], ev[l][5], sk(l), ev[l][3], addr_of.get(l, ""))  # noqa: E731
                    elif prm.objective == "min_volume":
                        k = lambda l: (round(ev[l][3], 3), ev[l][5], ev[l][1], sk(l), ev[l][2], addr_of.get(l, ""))  # noqa: E731
                    elif prm.objective == "min_width":
                        k = lambda l: (round(ev[l][4], 3), ev[l][5], ev[l][1], sk(l), ev[l][2], addr_of.get(l, ""))  # noqa: E731
                    elif prm.objective == "min_cost":  # bins já existentes: menos bins de maior valor
                        k = lambda l: (ev[l][1], round(ev[l][3], 3), ev[l][5], sk(l), ev[l][2], addr_of.get(l, ""))  # noqa: E731
                    else:
                        k = lambda l: (ev[l][1], ev[l][5], sk(l), ev[l][2], round(ev[l][3], 3), addr_of.get(l, ""))  # noqa: E731
                    only["loc"] = min(ok_locs, key=k)
                else:
                    only["loc"] = max(per_loc, key=lambda l: (ev[l][0], -ev[l][2]))
                    plan.note = ("Nenhum endereço do escopo comporta todo o SKU (o mesmo SKU em mais de um endereço "
                                 "não está permitido).")
        if prm.objective == "min_moves" and not prm.unlimited:
            # mínimo de movimentações: o que já está numa bin fica nela (até a capacidade física da bin)
            for b, q in sorted(plan.orig_bins, key=lambda bq: -bq[1]):
                if need <= 0 or b.location_id in reserved_locations or b.used > EPS or not cap.get(b.type_id):
                    continue
                if only["loc"] is not None and b.location_id != only["loc"]:
                    continue
                if prm.allowed_types is not None and b.type_id not in prm.allowed_types:
                    continue
                m = int(min(need, math.floor(q + EPS), full_caps[d.sku].get(b.type_id, 0)))
                if m <= 0:
                    continue
                fresh[(b.type_id, b.location_id)].remove(b)
                b.contents[d.sku] = m
                b.used = m / cap[b.type_id]
                if b.used < 1 - EPS:
                    partials.append(b)
                plan.allocations.append((b, m))
                need -= m
        partial = lambda b: 0 if b.used > EPS else 1  # noqa: E731 (completar bins já abertas primeiro)
        while need > 0:
            if prm.max_bins and len({b.id for b, _m in plan.allocations}) >= prm.max_bins:
                plan.shortage = need
                plan.note = plan.note or f"Atingiu o máximo de {prm.max_bins} bin(s) por SKU."
                break
            pool_c = [b for b in partials if only["loc"] is None or b.location_id == only["loc"]]
            for t, c in cap.items():
                if c > 0:
                    pool_c += take_fresh(t, here, d.sku)
            cands = [(b, e) for b, e in ((b, eff(b)) for b in pool_c) if e > 0]
            if not cands:
                plan.shortage = need
                plan.note = plan.note or "Faltam bins no escopo para este SKU."
                break
            fits = [(b, e) for b, e in cands if e >= need]
            if prm.objective == "min_moves":
                local = [be for be in cands if far(be[0]) <= 3]
                lfits = [be for be in fits if far(be[0]) <= 3]
                if lfits:
                    b, e = min(lfits, key=lambda be: (far2(be[0]), partial(be[0]), be[1], vol[be[0].type_id], be[0].id))
                elif local:
                    b, e = min(local, key=lambda be: (far2(be[0]), partial(be[0]), -be[1], be[0].id))
                elif fits:
                    b, e = min(fits, key=lambda be: (partial(be[0]), be[1], vol[be[0].type_id], be[0].id))
                else:
                    b, e = min(cands, key=lambda be: (partial(be[0]), -be[1], be[0].id))
            elif prm.objective == "min_width":
                if fits:
                    b, e = min(fits, key=lambda be: (partial(be[0]), wid[be[0].type_id], far2(be[0]), be[0].id))
                else:
                    b, e = min(cands, key=lambda be: (partial(be[0]), wid[be[0].type_id] / max(cap[be[0].type_id], 1),
                                                      far2(be[0]), -be[1], be[0].id))
            elif prm.objective == "min_cost":
                cst = lambda t: (types[t].cost or 0.0) if t in types else 0.0  # noqa: E731
                if fits:
                    b, e = min(fits, key=lambda be: (partial(be[0]), cst(be[0].type_id) * (1 - be[0].used),
                                                     vol[be[0].type_id], far2(be[0]), be[0].id))
                else:
                    b, e = min(cands, key=lambda be: (partial(be[0]), cst(be[0].type_id) / max(cap[be[0].type_id], 1),
                                                      far2(be[0]), -be[1], be[0].id))
            elif prm.objective == "min_volume":
                if fits:
                    b, e = min(fits, key=lambda be: (partial(be[0]), vol[be[0].type_id] * (1 - be[0].used),
                                                     far2(be[0]), be[0].id))
                else:
                    b, e = min(cands, key=lambda be: (partial(be[0]), vol[be[0].type_id] / max(cap[be[0].type_id], 1),
                                                      far2(be[0]), -be[1], be[0].id))
            else:  # min_bins
                if fits:
                    b, e = min(fits, key=lambda be: (partial(be[0]), be[1], far2(be[0]), vol[be[0].type_id], be[0].id))
                else:
                    b, e = min(cands, key=lambda be: (partial(be[0]), -be[1], far2(be[0]), be[0].id))
            m = min(need, e)
            if b.used <= EPS:
                if b.location_id is None:
                    virtual.append(b)
                else:
                    fresh[(b.type_id, b.location_id)].remove(b)
                partials.append(b)
            b.contents[d.sku] = b.contents.get(d.sku, 0) + m
            b.used += m / cap[b.type_id]
            if b.used >= 1 - EPS and b in partials:
                partials.remove(b)
            plan.allocations.append((b, m))
            if b.location_id is not None:
                placed_locs.setdefault(d.sku, set()).add(b.location_id)
            need -= m

    return finish_pool_result(res, bins, pool_all, pool, virtual, plans, prm, vol, wid, known, occupied_before,
                              reserved_locations)


def finish_pool_result(res: PoolResult, bins, pool_all, pool, virtual, plans, prm: PoolParams, vol, wid, known,
                       occupied_before, reserved_locations) -> PoolResult:
    """Indicadores do resultado (bins/endereços/volume/largura antes e depois, bins por tipo)."""
    # numeração das bins virtuais por tipo, na ordem de uso
    seqs: dict[int, int] = {}
    for b in virtual:
        seqs[b.type_id] = seqs.get(b.type_id, 0) + 1
        b.seq = seqs[b.type_id]
    res.plans = sorted(plans, key=lambda p: p.sku)
    for b in bins:
        if b.location_id not in reserved_locations and not b.locked:
            res.by_type.setdefault(b.type_id, {"existing": 0, "needed": 0})["existing"] += 1
    used_before = [b for b in pool_all if (b.orig if known else b.location_id in occupied_before)]
    res.volume_before = sum(vol[b.type_id] for b in used_before) / 1000.0
    res.width_before = sum(wid[b.type_id] for b in used_before)
    res.units_volume = sum(p.unit_vol * sum(p.current.values()) for p in plans) / 1000.0
    # ocupação depois: só as unidades que ficaram em bins (SKU sem bin não entra, senão passaria de 100%)
    res.units_volume_after = sum(p.unit_vol * min(sum(p.current.values()), sum(m for _b, m in p.allocations))
                                 for p in plans) / 1000.0
    if prm.unlimited:
        for b in virtual:
            res.by_type.setdefault(b.type_id, {"existing": 0, "needed": 0})["needed"] += 1
        res.bins_used_after = len(virtual)
        res.addresses_after = 0
        res.volume_after = sum(vol[b.type_id] for b in virtual) / 1000.0
        res.width_after = sum(wid[b.type_id] for b in virtual)
        return res
    for b in pool:
        if b.contents:
            res.by_type.setdefault(b.type_id, {"existing": 0, "needed": 0})["needed"] += 1
    used_after = [b for b in pool if b.contents]
    res.volume_after = sum(vol[b.type_id] for b in used_after) / 1000.0
    res.width_after = sum(wid[b.type_id] for b in used_after)
    res.bins_used_after = len(used_after)
    res.addresses_after = len({b.location_id for b in used_after})
    return res
