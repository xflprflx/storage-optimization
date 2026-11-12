"""
Otimização da configuração de bins por endereço (algoritmo puro, sem banco de dados).

Para cada endereço, dado o saldo atual (SKUs × quantidade), procura a combinação de bins
(tipo × quantidade) que melhor atende ao OBJETIVO escolhido, respeitando as RESTRIÇÕES.

Capacidade de uma bin para um SKU (modo "um SKU por bin"):
    capacidade = mínimo entre
      - geometria: maior encaixe em grade entre as orientações permitidas
                   ⌊A_bin/a⌋ × ⌊L_bin/l⌋ × ⌊C_bin/c⌋
      - volume:    ⌊volume_bin × taxa máx. de enchimento ÷ volume da unidade⌋
      - peso:      ⌊peso máx. da bin ÷ peso da unidade⌋
Modo "SKUs misturados": as unidades de vários SKUs dividem a mesma bin, limitadas por
volume (× taxa máx.) e peso; cada unidade precisa caber geometricamente na bin.

A busca combina, por SKU, opções de um tipo de bin ou de dois tipos (ex.: 3 grandes +
1 pequena) e depois combina os SKUs do endereço. Para endereços com muitos SKUs a
busca é limitada (heurística) — o resultado continua válido, mas pode não ser o ótimo
absoluto.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

EPS = 1e-9
MAX_COMBOS = 20_000
OPTIONS_PER_SKU = 10
PAIR_SPAN = 40  # quantas variações de "n bins grandes + resto em pequenas" testar

OBJECTIVES = {
    "min_bins": "Menos bins",
    "min_volume": "Menos volume de bins (menos ar)",
    "max_fill": "Maior ocupação das bins (volume das unidades ÷ volume das bins)",
    "min_width": "Menor largura de frente (mais espaço livre na prateleira)",
    "min_changes": "Mínimo de mudanças (só corrige o que não comporta o saldo)",
    "min_cost": "Menor custo de bins (R$)",
}

STATUS_LABELS = {
    "melhoria": "Pode melhorar",
    "corrigir": "Bins atuais não comportam o saldo",
    "liberar": "Sem saldo: liberar bins",
    "ok": "Já está ótimo",
    "inviavel": "Inviável com as restrições",
    "sem_dados": "Faltam dimensões",
    "sem_saldo": "Sem saldo",
    "realocar": "Cede bins e é rearranjado",
    "orfao": "Cede bins: itens ficam fora de bin",
}
APPLICABLE = {"melhoria", "corrigir", "liberar", "realocar", "orfao"}


# --------------------------------------------------------------------------- #
# Entradas
# --------------------------------------------------------------------------- #
@dataclass
class ProductUnit:
    sku: str
    qty: float
    h: Optional[float] = None
    w: Optional[float] = None
    l: Optional[float] = None
    weight: Optional[float] = None

    @property
    def has_dims(self) -> bool:
        return all(v is not None and v > 0 for v in (self.h, self.w, self.l))

    @property
    def volume_cm3(self) -> Optional[float]:
        return self.h * self.w * self.l if self.has_dims else None


@dataclass
class BinSpec:
    id: int
    code: str
    h: Optional[float] = None
    w: Optional[float] = None
    l: Optional[float] = None
    volume_l: Optional[float] = None
    max_weight: Optional[float] = None
    front_w: Optional[float] = None  # largura de frente (compartimento: fração da largura da bin pai)
    cost: Optional[float] = None  # custo de uma bin (R$); None = não informado (conta como 0)

    @property
    def fw(self) -> Optional[float]:
        """Largura que o tipo ocupa na frente do nível (rateada para compartimentos)."""
        return self.front_w if self.front_w is not None else self.w

    @property
    def has_dims(self) -> bool:
        return all(v is not None and v > 0 for v in (self.h, self.w, self.l))

    @property
    def volume_cm3(self) -> Optional[float]:
        if self.volume_l is not None:
            return self.volume_l * 1000.0
        return self.h * self.w * self.l if self.has_dims else None


@dataclass
class AddressIn:
    id: int
    address: str
    current: dict[int, int]  # bin_type_id -> qtd
    items: list[ProductUnit]
    linked_without_stock: bool = False  # produto com endereço padrão aqui, sem saldo
    own_level: Optional[object] = None  # chave do nível físico do próprio endereço
    current_placement: dict = field(default_factory=dict)  # nível -> {tipo: qtd} (bins atuais por nível)
    bay_levels: list = field(default_factory=list)  # todos os níveis da estante (ordem natural)
    notes: list = field(default_factory=list)  # observações da carga (ex.: SKU sem max level -> usou saldo)


@dataclass
class LevelSlot:
    """Nível físico com capacidade (None = sem capacidade cadastrada: não limita)."""
    id: object
    label: str
    caps: Optional[dict[int, int]] = None
    height: Optional[float] = None  # vão do nível (cm): bin mais alta faz a estrutura subir
    derived: set = field(default_factory=set)  # tipos com capacidade estimada pela largura do nível


class LevelLedger:
    """Uso corrente de cada nível (todas as bins de todos os endereços), atualizado à medida
    que as sugestões são decididas — assim dois endereços não disputam o mesmo espaço livre."""

    def __init__(self, slots: dict, usage: dict):
        self.slots = slots  # id -> LevelSlot
        self.usage = {k: dict(v) for k, v in usage.items()}  # id -> {tipo: qtd}

    def free(self, slot_id, exclude: Optional[dict[int, int]] = None) -> Optional[float]:
        slot = self.slots.get(slot_id)
        if slot is None or not slot.caps:
            return None
        used = dict(self.usage.get(slot_id, {}))
        for t, n in (exclude or {}).items():
            used[t] = used.get(t, 0) - n
        frac = sum(n / slot.caps[t] for t, n in used.items() if n > 0 and slot.caps.get(t))
        return max(0.0, 1.0 - frac)

    def over(self, slot_id) -> bool:
        f = self.free(slot_id)
        if f is None:
            return False
        slot = self.slots[slot_id]
        used = self.usage.get(slot_id, {})
        return sum(n / slot.caps[t] for t, n in used.items() if n > 0 and slot.caps.get(t)) > 1.0 + EPS

    def commit(self, old: dict, new: dict) -> None:
        for sid, cfg in old.items():
            u = self.usage.setdefault(sid, {})
            for t, n in cfg.items():
                u[t] = u.get(t, 0) - n
        for sid, cfg in new.items():
            u = self.usage.setdefault(sid, {})
            for t, n in cfg.items():
                u[t] = u.get(t, 0) + n


BASES = {
    "stock": "Saldo atual",
    "min": "Nível mínimo (min level)",
    "max": "Nível máximo (max level)",
}


@dataclass
class Params:
    objective: str = "min_volume"
    allowed_types: Optional[set[int]] = None  # None = todos
    fill_limit: float = 0.85  # taxa máxima de enchimento por volume (0–1)
    growth: float = 0.0  # margem sobre o saldo (0.2 = +20%)
    use_geometry: bool = True
    rotation: str = "upright"  # "upright" (altura fixa) | "any" (6 orientações)
    enforce_weight: bool = True
    mix_skus: bool = False  # vários SKUs na mesma bin
    single_type: bool = False  # um único tipo de bin por endereço
    max_bins: Optional[int] = None
    keep_space: bool = False  # não exceder largura/altura atuais do endereço
    max_width_cm: Optional[float] = None
    max_height_cm: Optional[float] = None
    clear_empty: bool = False  # endereços sem saldo e sem produto: sugerir remover as bins
    respect_levels: bool = True  # bins precisam caber na capacidade dos níveis (sempre ligado na tela)
    allow_other_levels: bool = False  # pode sugerir usar outros níveis da estante com espaço livre
    mixed_level_types: bool = True  # permite tipos de bin diferentes no mesmo nível
    basis: str = "stock"  # quantidade considerada: "stock" (saldo atual) | "min" (min level) | "max" (max level)
    # True ("bins existentes"): o total de bins de cada tipo no plano não passa do que existe hoje no escopo
    # (as bins só trocam de endereço); False ("recurso infinito"): pode sugerir bins que não existem no escopo
    limit_inventory: bool = False
    # motor: "heuristic" (o atual) | "or" (pesquisa operacional, OR-Tools CP-SAT) e o tempo limite (s) do solver
    engine: str = "heuristic"
    time_limit: Optional[float] = 15.0  # None = sem limite (solução do solver a qualquer custo)
    # ordem de prioridade dos critérios ("bins", "volume", "width", "changes"); None = padrão do motor
    priority: Optional[list] = None
    # foco nos SKUs filtrados (bins existentes): de onde vêm as bins além das dos próprios endereços —
    # "free" = só de endereços sem saldo; "any" = também de endereços com saldo de outros itens, que são
    # rearranjados ("relocate") ou ficam com os itens fora de bin ("leave")
    bin_source: str = "free"
    orphans: str = "relocate"
    extra_inventory: Optional[dict] = None  # interno: bins de outros endereços disponíveis ao plano {tipo: qtd}

    def validate(self) -> None:
        if self.objective not in OBJECTIVES:
            raise ValueError("Objetivo inválido.")
        if not (0.05 <= self.fill_limit <= 1.0):
            raise ValueError("Taxa máxima de enchimento deve estar entre 5% e 100%.")
        if self.growth < 0 or self.growth > 10:
            raise ValueError("Margem de crescimento deve estar entre 0% e 1000%.")
        if self.rotation not in ("upright", "any"):
            raise ValueError("Rotação inválida.")
        if self.basis not in BASES:
            raise ValueError("Base de cálculo inválida.")
        if self.max_bins is not None and self.max_bins < 1:
            raise ValueError("Máximo de bins por endereço deve ser ao menos 1.")
        if self.engine not in ("heuristic", "or"):
            raise ValueError("Motor inválido.")
        if self.time_limit is not None and not (1 <= self.time_limit <= 3600):  # None = sem limite
            raise ValueError("Tempo limite deve estar entre 1 e 3600 segundos (ou sem limite).")


# --------------------------------------------------------------------------- #
# Saídas
# --------------------------------------------------------------------------- #
@dataclass
class Metrics:
    bins: int = 0
    volume_l: float = 0.0
    width_cm: float = 0.0
    height_cm: float = 0.0
    cost: float = 0.0  # R$ (tipos sem custo contam 0)
    unknown_dims: bool = False


@dataclass
class AddressResult:
    id: int
    address: str
    status: str
    current: dict[int, int]
    current_metrics: Metrics
    suggested: Optional[dict[int, int]] = None
    suggested_metrics: Optional[Metrics] = None
    used_volume_l: float = 0.0
    fill_now: Optional[float] = None  # %
    fill_after: Optional[float] = None  # %
    current_fits: Optional[bool] = None
    notes: list[str] = field(default_factory=list)
    placement: Optional[dict] = None  # nível -> {tipo: qtd} da sugestão
    own_level: Optional[object] = None
    current_placement: dict = field(default_factory=dict)
    removed_bins: Optional[list] = None  # doador "órfão": bins cedidas [(tipo, nº)] (o conteúdo fica fora de bin)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def applicable(self) -> bool:
        return self.status in APPLICABLE and self.suggested is not None

    @property
    def bins_delta(self) -> int:
        return (self.suggested_metrics.bins - self.current_metrics.bins) if self.suggested_metrics else 0

    @property
    def volume_delta(self) -> float:
        return (self.suggested_metrics.volume_l - self.current_metrics.volume_l) if self.suggested_metrics else 0.0

    @property
    def width_delta(self) -> float:
        return (self.suggested_metrics.width_cm - self.current_metrics.width_cm) if self.suggested_metrics else 0.0


@dataclass
class Summary:
    addresses: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    bins_before: int = 0
    bins_after: int = 0
    volume_before: float = 0.0
    volume_after: float = 0.0
    width_before: float = 0.0
    width_after: float = 0.0
    used_volume: float = 0.0
    solver: Optional[dict] = None  # pesquisa operacional: status, tempo, valores por critério
    orphans: list = field(default_factory=list)  # (sku, endereço, bin, qtd) que ficam fora de bin
    donors: Optional[dict] = None  # foco: endereços doadores {"free": n, "stocked": n, "uncovered": {tipo: n}}

    @property
    def fill_before(self) -> Optional[float]:
        return self.used_volume / self.volume_before * 100 if self.volume_before else None

    @property
    def fill_after(self) -> Optional[float]:
        return self.used_volume / self.volume_after * 100 if self.volume_after else None


# --------------------------------------------------------------------------- #
# Capacidade
# --------------------------------------------------------------------------- #
def _orientations(p: ProductUnit, rotation: str) -> list[tuple[float, float, float]]:
    if rotation == "any":
        return list(set(itertools.permutations((p.h, p.w, p.l))))
    return list({(p.h, p.w, p.l), (p.h, p.l, p.w)})  # altura sempre na vertical


def _grid(p: ProductUnit, b: BinSpec, rotation: str) -> int:
    best = 0
    for oh, ow, ol in _orientations(p, rotation):
        n = math.floor(b.h / oh + EPS) * math.floor(b.w / ow + EPS) * math.floor(b.l / ol + EPS)
        best = max(best, n)
    return best


def unit_fits(p: ProductUnit, b: BinSpec, prm: Params) -> bool:
    if not (prm.use_geometry and p.has_dims and b.has_dims):
        return True
    return _grid(p, b, prm.rotation) >= 1


def capacity(p: ProductUnit, b: BinSpec, prm: Params) -> int:
    """Unidades de um SKU que cabem em UMA bin do tipo (modo um SKU por bin). 0 = não cabe."""
    uv = p.volume_cm3
    if uv is None:
        return 0
    limits = []
    if prm.use_geometry and b.has_dims:
        limits.append(_grid(p, b, prm.rotation))
    bv = b.volume_cm3
    if bv is not None:
        limits.append(math.floor(bv * prm.fill_limit / uv + EPS))
    if not limits:
        return 0  # tipo sem dimensões nem volume: não dá para calcular
    if prm.enforce_weight and p.weight and b.max_weight:
        limits.append(math.floor(b.max_weight / p.weight + EPS))
    return max(0, min(limits))


# --------------------------------------------------------------------------- #
# Métricas, restrições e objetivo
# --------------------------------------------------------------------------- #
def metrics(cfg: dict[int, int], types: dict[int, BinSpec]) -> Metrics:
    m = Metrics()
    for tid, n in cfg.items():
        if n <= 0:
            continue
        t = types.get(tid)
        m.bins += n
        if t is None:
            m.unknown_dims = True
            continue
        m.cost += (t.cost or 0.0) * n
        if t.volume_cm3 is not None:
            m.volume_l += t.volume_cm3 / 1000.0 * n
        if t.fw is not None and t.h is not None:
            m.width_cm += t.fw * n
            m.height_cm = max(m.height_cm, t.h)
        else:
            m.unknown_dims = True
    return m


CRITERIA = {"bins": "Menos bins", "volume": "Menos volume de bins", "width": "Menor largura de frente",
            "changes": "Mínimo de mudanças / movimentações", "cost": "Menor custo de bins"}
OBJ_CRITERION = {"min_bins": "bins", "min_volume": "volume", "min_width": "width", "min_changes": "changes",
                 "min_moves": "changes", "min_cost": "cost"}


def canonical_objective(objective: str) -> str:
    """"Maior ocupação" é o mesmo cálculo de "menos volume de bins": as unidades guardadas são as mesmas, então
    ocupação = volume das unidades ÷ volume das bins sobe exatamente quando o volume das bins cai. O nome existe para
    quem pensa em ocupação; o motor usa o menor volume."""
    return "min_volume" if objective == "max_fill" else objective


def priority_list(objective: str, custom: Optional[list] = None) -> list[str]:
    """Ordem completa dos critérios: o do objetivo primeiro; depois a ordem escolhida pelo usuário (ou o padrão:
    mínimo de mudanças e os demais na ordem bins, volume, largura)."""
    first = OBJ_CRITERION.get(objective, "volume")
    rest = [c for c in (custom or []) if c in CRITERIA and c != first]
    default = ["changes", "bins", "volume", "width", "cost"]
    out = [first] + rest
    return out + [c for c in default if c not in out]


def prio_key(m: Metrics, prio: list, changes: int = 0) -> tuple:
    r = lambda v: round(v, 4)  # noqa: E731
    val = {"bins": m.bins, "volume": r(m.volume_l), "width": r(m.width_cm), "changes": changes, "cost": r(m.cost)}
    return tuple(val[c] for c in prio)


def objective_key(m: Metrics, objective: str, changes: int = 0) -> tuple:
    r = lambda v: round(v, 4)  # noqa: E731
    if objective == "min_bins":
        return (m.bins, r(m.volume_l), r(m.width_cm), changes)
    if objective == "min_width":
        return (r(m.width_cm), m.bins, r(m.volume_l), changes)
    if objective == "min_cost":
        return (r(m.cost), r(m.volume_l), m.bins, r(m.width_cm), changes)
    return (r(m.volume_l), m.bins, r(m.width_cm), changes)  # min_volume (e base do min_changes)


def changes_between(a: dict[int, int], b: dict[int, int]) -> int:
    return sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b))


def _space_limits(current_m: Metrics, prm: Params) -> tuple[float, float, list[str]]:
    notes = []
    max_w, max_h = math.inf, math.inf
    if prm.keep_space:
        if current_m.bins and not current_m.unknown_dims and current_m.width_cm > 0:
            max_w, max_h = current_m.width_cm, current_m.height_cm
        else:
            notes.append("Espaço atual desconhecido (sem bins dimensionadas): restrição de espaço ignorada.")
    if prm.max_width_cm:
        max_w = min(max_w, prm.max_width_cm)
    if prm.max_height_cm:
        max_h = min(max_h, prm.max_height_cm)
    return max_w, max_h, notes


def violations(cfg: dict[int, int], m: Metrics, prm: Params, max_w: float, max_h: float) -> list[str]:
    v = []
    if prm.allowed_types is not None and any(t not in prm.allowed_types for t, n in cfg.items() if n > 0):
        v.append("usa tipo de bin não permitido")
    if prm.single_type and len([1 for n in cfg.values() if n > 0]) > 1:
        v.append("mais de um tipo de bin")
    if prm.max_bins is not None and m.bins > prm.max_bins:
        v.append(f"mais de {prm.max_bins} bin(s)")
    if max_w < math.inf or max_h < math.inf:
        if m.unknown_dims:
            v.append("tipo de bin sem dimensões (não dá para checar o espaço)")
        else:
            if m.width_cm > max_w + 0.01:
                v.append(f"largura {m.width_cm:g} cm > {max_w:g} cm")
            if m.height_cm > max_h + 0.01:
                v.append(f"altura {m.height_cm:g} cm > {max_h:g} cm")
    return v


# --------------------------------------------------------------------------- #
# Geração de candidatos
# --------------------------------------------------------------------------- #
def _units(qty: float, growth: float) -> int:
    return max(0, math.ceil(qty * (1.0 + growth) - EPS))


def _merge(cfgs: Iterable[dict[int, int]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for c in cfgs:
        for k, n in c.items():
            if n > 0:
                out[k] = out.get(k, 0) + n
    return out


def _key(cfg: dict[int, int]) -> tuple:
    return tuple(sorted((k, n) for k, n in cfg.items() if n > 0))


def _sku_options(units: int, caps: dict[int, int], types: dict[int, BinSpec], prm: Params) -> list[dict[int, int]]:
    opts: dict[tuple, dict[int, int]] = {}
    usable = [t for t, c in caps.items() if c > 0]
    for t in usable:
        cfg = {t: math.ceil(units / caps[t])}
        opts[_key(cfg)] = cfg
    if not prm.single_type:
        for t1, t2 in itertools.permutations(usable, 2):
            n1max = math.ceil(units / caps[t1])
            for n1 in range(max(1, n1max - PAIR_SPAN), n1max):
                rest = units - n1 * caps[t1]
                if rest <= 0:
                    continue
                cfg = {t1: n1, t2: math.ceil(rest / caps[t2])}
                opts[_key(cfg)] = cfg
    ranked = sorted(opts.values(), key=lambda c: objective_key(metrics(c, types), prm.objective))
    keep = ranked[:OPTIONS_PER_SKU]
    # garante diversidade: melhor de cada métrica (ajuda quando há restrição de espaço / nº de bins)
    for obj in ("min_bins", "min_volume", "min_width", "min_cost"):
        best = min(opts.values(), key=lambda c: objective_key(metrics(c, types), obj))
        if best not in keep:
            keep.append(best)
    return keep


def _fill_count(items: list[tuple[ProductUnit, int]], sequence, prm: Params) -> Optional[dict[int, int]]:
    """Preenche bins em sequência com unidades de vários SKUs (volume × taxa e peso).
    `sequence(k)` devolve o BinSpec da k-ésima bin (ou None = acabou). Retorna contagem por tipo."""
    order = sorted(items, key=lambda it: -(it[0].volume_cm3 or 0))
    counts: dict[int, int] = {}
    k = 0
    cur: Optional[BinSpec] = None
    rem_v = rem_w = 0.0
    for p, u in order:
        uv = p.volume_cm3
        uw = p.weight if (prm.enforce_weight and p.weight) else 0.0
        while u > 0:
            fits_cur = cur is not None and uv <= rem_v + EPS and (not uw or not cur.max_weight or uw <= rem_w + EPS)
            if not fits_cur:
                cur = sequence(k)
                k += 1
                if cur is None:
                    return None
                if not unit_fits(p, cur, prm):
                    return None
                rem_v = (cur.volume_cm3 or 0) * prm.fill_limit
                rem_w = cur.max_weight or math.inf
                counts[cur.id] = counts.get(cur.id, 0) + 1
                if uv > rem_v + EPS or (uw and cur.max_weight and uw > rem_w + EPS):
                    return None  # unidade maior que a bin inteira
            by_v = math.floor(rem_v / uv + EPS)
            by_w = math.floor(rem_w / uw + EPS) if (uw and cur.max_weight) else u
            n = min(u, by_v, by_w)
            u -= n
            rem_v -= n * uv
            if uw and cur.max_weight:
                rem_w -= n * uw
            if n == 0:
                cur = None  # força nova bin
    return counts


def _mixed_candidates(items, types: dict[int, BinSpec], prm: Params) -> list[dict[int, int]]:
    usable = [t for t in types.values()
              if t.volume_cm3 and all(unit_fits(p, t, prm) for p, _ in items)]
    out: dict[tuple, dict[int, int]] = {}
    for t in usable:
        c = _fill_count(items, lambda k, t=t: t, prm)
        if c:
            out[_key(c)] = c
    if not prm.single_type:
        for t1, t2 in itertools.permutations(usable, 2):
            full = _fill_count(items, lambda k, t=t1: t, prm)
            if not full:
                continue
            n1max = full.get(t1.id, 0)
            for n1 in range(max(1, n1max - 6), n1max):
                c = _fill_count(items, lambda k, n1=n1, a=t1, b=t2: a if k < n1 else b, prm)
                if c:
                    out[_key(c)] = c
    return list(out.values())


def _current_fits(items, current: dict[int, int], types: dict[int, BinSpec], prm: Params) -> Optional[bool]:
    """As bins atuais comportam o saldo (com a margem)? None = não dá para saber."""
    if not current:
        return False
    if any(types.get(t) is None or types[t].volume_cm3 is None for t in current):
        return None
    if prm.mix_skus:
        pool = sorted((types[t] for t, n in current.items() for _ in range(n)), key=lambda b: -(b.volume_cm3 or 0))
        return _fill_count(items, lambda k: pool[k] if k < len(pool) else None, prm) is not None
    pool = dict(current)
    for p, u in sorted(items, key=lambda it: -(it[0].volume_cm3 or 0) * it[1]):
        while u > 0:
            best_t, best_c = None, 0
            for t, n in pool.items():
                if n > 0:
                    c = capacity(p, types[t], prm)
                    if c > best_c:
                        best_t, best_c = t, c
            if best_t is None:
                return False
            pool[best_t] -= 1
            u -= best_c
    return True


# --------------------------------------------------------------------------- #
# Níveis: alocação gulosa das bins (mesma regra de level_service.place)
# --------------------------------------------------------------------------- #
def place_in_levels(cfg: dict[int, int], slots: list[tuple], heights: Optional[dict[int, float]] = None):
    """Distribui as bins pelos níveis em ordem de preferência.
    slots = [(id, capacidade{tipo:max}|None, fração livre|None, vão_cm|None)]."""
    heights = heights or {}
    remaining = {t: n for t, n in cfg.items() if n > 0}
    alloc: dict[object, dict[int, int]] = {}
    for slot in slots:
        sid, caps, free = slot[0], slot[1], slot[2]
        max_h = slot[3] if len(slot) > 3 else None
        if not remaining:
            break
        fits_h = lambda t: max_h is None or heights.get(t) is None or heights[t] <= max_h + EPS  # noqa: E731
        if not caps or free is None:
            dst = None
            for t, n in list(remaining.items()):
                if fits_h(t):
                    dst = alloc.setdefault(sid, {})
                    dst[t] = dst.get(t, 0) + n
                    del remaining[t]
            continue
        for t in sorted(remaining, key=lambda t: -(1.0 / caps[t]) if caps.get(t) else 0):
            mx = caps.get(t)
            if not mx or not fits_h(t):
                continue
            k = min(remaining[t], math.floor(free * mx + EPS))
            if k > 0:
                dst = alloc.setdefault(sid, {})
                dst[t] = dst.get(t, 0) + k
                remaining[t] -= k
                free -= k / mx
        remaining = {t: n for t, n in remaining.items() if n > 0}
    return alloc, remaining


def _level_slots(a: AddressIn, ledger: Optional[LevelLedger], prm: Params):
    """Níveis disponíveis para o endereço, em ordem de preferência, com a fração livre
    (descontando as bins do próprio endereço, que serão substituídas)."""
    if not (prm.respect_levels and ledger and a.own_level is not None):
        return None
    order = [a.own_level] + [k for k in a.current_placement if k != a.own_level]
    if prm.allow_other_levels:
        order += [k for k in a.bay_levels if k not in order]
    out = []
    for sid in order:
        slot = ledger.slots.get(sid)
        caps = slot.caps if slot else None
        out.append((sid, caps, ledger.free(sid, exclude=a.current_placement.get(sid)), slot.height if slot else None))
    return out


def _placement_key(pl: Optional[dict]) -> tuple:
    if not pl:
        return ()
    return tuple(sorted((str(sid), tuple(sorted((t, n) for t, n in cfg.items() if n))) for sid, cfg in pl.items()
                        if any(cfg.values())))


# --------------------------------------------------------------------------- #
# Otimização de um endereço
# --------------------------------------------------------------------------- #
def optimize_address(a: AddressIn, all_types: dict[int, BinSpec], prm: Params,
                     ledger: Optional[LevelLedger] = None, limits: Optional[dict[int, int]] = None,
                     collect: Optional[dict] = None, force: Optional[tuple] = None) -> AddressResult:
    """`limits` = máximo de bins de cada tipo que este endereço pode ter (bins disponíveis no escopo).
    Pesquisa operacional: `collect` (dict) recebe as configurações candidatas que respeitam as restrições do próprio
    endereço (sem os níveis, que o modelo global trata) e a função retorna logo; `force` = (configuração, níveis)
    escolhida pelo modelo — o resultado (situação, observações) é montado como na heurística."""
    cur_m = metrics(a.current, all_types)
    res = AddressResult(a.id, a.address, "ok", dict(a.current), cur_m)
    res.own_level, res.current_placement = a.own_level, {k: dict(v) for k, v in a.current_placement.items()}
    types = {t: b for t, b in all_types.items() if prm.allowed_types is None or t in prm.allowed_types}
    res.notes.extend(a.notes)

    items = [(p, _units(p.qty, prm.growth)) for p in a.items if p.qty and p.qty > 0]
    items = [(p, u) for p, u in items if u > 0]
    used = sum((p.volume_cm3 or 0) * p.qty for p, _ in items) / 1000.0
    res.used_volume_l = used
    if cur_m.volume_l and items:
        res.fill_now = used / cur_m.volume_l * 100.0

    if not items:
        if prm.clear_empty and a.current and not a.linked_without_stock:
            res.status, res.suggested, res.suggested_metrics = "liberar", {}, Metrics()
            res.notes.append("Endereço sem saldo e sem produto atrelado.")
        else:
            res.status = "sem_saldo"
        return res

    missing = [p.sku for p, _ in items if not p.has_dims]
    if missing:
        res.status = "sem_dados"
        res.notes.append("SKU sem dimensões: " + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
        return res
    if not types:
        res.status = "inviavel"
        res.notes.append("Nenhum tipo de bin permitido.")
        return res

    slots = _level_slots(a, ledger, prm)
    heights = {t: b.h for t, b in all_types.items() if b.h is not None}

    def level_types(sid) -> set:
        """Tipos que OUTROS endereços usam no nível (no estado atual do plano)."""
        used = dict(ledger.usage.get(sid, {})) if ledger else {}
        for t, n in a.current_placement.get(sid, {}).items():
            used[t] = used.get(t, 0) - n
        return {t for t, n in used.items() if n > 0}

    if slots is not None and not slots[0][1]:
        res.notes.append("Nível sem capacidade cadastrada: não dá para garantir que as bins cabem "
                         "(cadastre em Capacidade dos níveis).")
    if slots is not None and (slots[0][1] or slots[0][3] is not None):
        # O nível do endereço tem capacidade cadastrada: ela define o espaço (substitui "espaço atual")
        max_w, max_h, _ = _space_limits(cur_m, Params(keep_space=False, max_width_cm=prm.max_width_cm,
                                                      max_height_cm=prm.max_height_cm))
    else:
        max_w, max_h, space_notes = _space_limits(cur_m, prm)
        res.notes.extend(space_notes)

    # ---- candidatos ----
    candidates: list[dict[int, int]] = []
    if prm.mix_skus:
        candidates = _mixed_candidates(items, types, prm)
        if not candidates:
            res.status = "inviavel"
            res.notes.append("Algum SKU não cabe (geometria/peso) em nenhum tipo de bin permitido.")
            return res
    else:
        per_sku = []
        for p, u in items:
            caps = {t: capacity(p, b, prm) for t, b in types.items()}
            if not any(c > 0 for c in caps.values()):
                res.status = "inviavel"
                res.notes.append(f"SKU {p.sku} não cabe em nenhum tipo de bin permitido.")
                return res
            per_sku.append((p, u, caps))
        if prm.single_type:
            for t in types:
                if all(caps[t] > 0 for _, _, caps in per_sku):
                    candidates.append({t: sum(math.ceil(u / caps[t]) for _, u, caps in per_sku)})
        else:
            options = [_sku_options(u, caps, types, prm) for _, u, caps in per_sku]
            k = max(1, int(MAX_COMBOS ** (1.0 / len(options))))
            options = [o[:k] for o in options]
            for combo in itertools.product(*options):
                candidates.append(_merge(combo))

    if collect is not None:
        collect["cands"] = []
        for c in candidates:
            m = metrics(c, all_types)
            if not violations(c, m, prm, max_w, max_h):
                collect["cands"].append((c, m))
        # a configuração atual também é opção válida quando comporta o saldo e respeita as restrições do endereço
        if a.current and _current_fits(items, a.current, all_types, prm) and not violations(a.current, cur_m, prm, max_w, max_h):
            if all(_key(c) != _key(a.current) for c, _ in collect["cands"]):
                collect["cands"].append((dict(a.current), cur_m))
        collect["slots"] = [sl[0] for sl in slots] if slots is not None else None
        collect["res"] = res
        return res
    if force is not None:
        candidates = [force[0]]

    # ---- filtra restrições e escolhe ----
    feasible = []
    reasons: dict[str, int] = {}
    for c in candidates:
        m = metrics(c, all_types)
        v = violations(c, m, prm, max_w, max_h)
        if not v and limits is not None and any(n > limits.get(t, 0) for t, n in c.items()):
            v = ["faltam bins desse tipo no escopo (bins existentes)"]
        pl = None
        if force is not None:
            v, pl = [], force[1]
        elif not v and slots is not None:
            # Altura não restringe: se a bin for mais alta que o vão, a estrutura (vão) é que sobe.
            pl, rest = place_in_levels(c, slots)
            if rest:
                v = ["não cabe na capacidade do(s) nível(is)"]
            elif not prm.mixed_level_types and pl:
                for sid, cfg in pl.items():
                    placed = {t for t, n in cfg.items() if n > 0}
                    if len(placed | level_types(sid)) > 1:
                        v = ["tipos de bin diferentes no mesmo nível"]
                        break
        if v:
            for r in v:
                reasons[r] = reasons.get(r, 0) + 1
        else:
            feasible.append((c, m, pl))

    res.current_fits = _current_fits(items, a.current, all_types, prm)
    cur_ok = not violations(a.current, cur_m, prm, max_w, max_h) if a.current else False
    if cur_ok and slots is not None:
        cur_over = [sid for sid in a.current_placement if ledger.over(sid)]
        if cur_over:
            cur_ok = False
            res.notes.append("Nível atual acima da capacidade.")

    if not feasible:
        res.status = "inviavel"
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        res.notes.append("Nenhuma combinação respeita as restrições" + (": " + "; ".join(r for r, _ in top) if top else "."))
        return res

    base_obj = "min_volume" if prm.objective == "min_changes" else prm.objective
    n_levels = lambda pl: len([1 for cfg in (pl or {}).values() if any(cfg.values())])  # noqa: E731
    if prm.priority:  # ordem de prioridade escolhida pelo usuário
        prio = priority_list(prm.objective, prm.priority)
        best_cfg, best_m, best_pl = min(
            feasible, key=lambda cm: prio_key(cm[1], prio, changes_between(cm[0], a.current)) + (n_levels(cm[2]),))
    else:
        best_cfg, best_m, best_pl = min(
            feasible, key=lambda cm: objective_key(cm[1], base_obj, changes_between(cm[0], a.current)) + (n_levels(cm[2]),))

    if prm.objective == "min_changes" and res.current_fits and cur_ok and force is None:
        best_cfg, best_m, best_pl = dict(a.current), cur_m, (res.current_placement if slots is not None else None)

    res.suggested, res.suggested_metrics, res.placement = best_cfg, best_m, best_pl
    if best_pl and ledger is not None:
        est = sorted({all_types[t].code for sid, cfg in best_pl.items() for t, n in cfg.items()
                      if n and ledger.slots.get(sid) and t in ledger.slots[sid].derived and t in all_types})
        if est:
            res.notes.append("Capacidade de " + ", ".join(est) + " no nível estimada pela largura (não cadastrada).")
    if best_pl:
        extra_lv = [str(k[4]) if isinstance(k, tuple) else str(k) for k, cfg in best_pl.items()
                    if k != a.own_level and any(cfg.values())]
        if extra_lv:
            res.notes.append("Usa nível(is) extra(s) da estante: " + ", ".join(extra_lv) + ".")
    if ledger is not None:
        lifts = []
        for sid, cfg in (best_pl or ({a.own_level: best_cfg} if a.own_level is not None else {})).items():
            slot = ledger.slots.get(sid)
            top = max((heights.get(t) or 0 for t, n in cfg.items() if n), default=0)
            if slot and slot.height is not None and top > slot.height + EPS:
                lifts.append(f"{slot.label} {slot.height:g}→{top:g} cm".replace(".", ","))
        if lifts:
            res.notes.append("Estrutura sobe para acomodar a bin (vão): " + ", ".join(lifts) + ".")
    res.fill_after = used / best_m.volume_l * 100.0 if best_m.volume_l else None
    same_place = slots is None or _placement_key(best_pl) == _placement_key(res.current_placement)

    if _key(best_cfg) == _key(a.current) and same_place:
        res.status = "ok"
    elif _key(best_cfg) == _key(a.current):
        res.status = "melhoria"
        res.notes.append("Mesmas bins, redistribuídas entre os níveis.")
    elif res.current_fits is False:
        res.status = "corrigir"
    elif force is None and res.current_fits and cur_ok and (
            prio_key(best_m, priority_list(prm.objective, prm.priority), changes_between(best_cfg, a.current))
            >= prio_key(cur_m, priority_list(prm.objective, prm.priority), 0) if prm.priority
            else objective_key(best_m, base_obj) >= objective_key(cur_m, base_obj)):
        res.status = "ok"  # atual já é tão bom quanto
        res.suggested, res.suggested_metrics, res.fill_after = dict(a.current), cur_m, res.fill_now
        res.placement = res.current_placement if slots is not None else None
    else:
        res.status = "melhoria"
        if not cur_ok and a.current:
            res.notes.append("Configuração atual viola as restrições escolhidas.")
    return res


def _after(r: AddressResult) -> dict[int, int]:
    return dict(r.suggested or {}) if (r.applicable and r.status != "ok") or r.status == "liberar" else dict(r.current or {})


def _limit_inventory(addresses: list[AddressIn], results: list[AddressResult], types: dict[int, BinSpec], prm: Params,
                     ledger: Optional[LevelLedger]) -> None:
    """Bins existentes: o plano só pode usar as bins que existem hoje no escopo (elas trocam de endereço).
    Se algum tipo passar do estoque, refaz os endereços que ganham bins desse tipo, limitando cada um ao que sobra:
    primeiro as correções (o saldo não cabe nas bins atuais), contando que as melhorias opcionais ainda não
    decididas abrem mão do que ganhariam; depois as melhorias, com o que restou."""
    budget: dict[int, int] = dict(prm.extra_inventory or {})
    for r in results:
        for t, n in (r.current or {}).items():
            budget[t] = budget.get(t, 0) + n
    by_id = {a.id: a for a in addresses}
    done: set[int] = set()

    def used() -> dict[int, int]:
        u: dict[int, int] = {}
        for r in results:
            for t, n in _after(r).items():
                u[t] = u.get(t, 0) + n
        return u

    while True:
        u = used()
        over = [t for t, n in u.items() if n > budget.get(t, 0)]
        if not over:
            return
        prio = {"corrigir": 0, "melhoria": 1}
        gainers = [i for i, r in enumerate(results) if r.id not in done and r.status in prio
                   and any(_after(r).get(t, 0) > (r.current or {}).get(t, 0) for t in over)]
        if not gainers:
            # ainda passa do estoque (uma melhoria ficou com o que outra devolveria): desfaz melhorias que ganham o tipo
            for t in over:
                for j in sorted(range(len(results)), key=lambda j: results[j].address):
                    if used().get(t, 0) <= budget.get(t, 0):
                        break
                    o = results[j]
                    if o.status == "melhoria" and _after(o).get(t, 0) > (o.current or {}).get(t, 0):
                        a_ = by_id[o.id]
                        if ledger is not None and o.placement is not None:
                            ledger.commit(o.placement, a_.current_placement)
                        nr = AddressResult(a_.id, a_.address, "ok", dict(a_.current), o.current_metrics,
                                           suggested=dict(a_.current), suggested_metrics=o.current_metrics,
                                           used_volume_l=o.used_volume_l, fill_now=o.fill_now, fill_after=o.fill_now,
                                           current_fits=o.current_fits, own_level=o.own_level,
                                           current_placement=o.current_placement, placement=o.current_placement)
                        nr.notes = list(a_.notes) + ["Haveria melhoria, mas faltam bins desse tipo no escopo (bins existentes)."]
                        results[j] = nr
            return
        i = min(gainers, key=lambda i: (prio[results[i].status], results[i].address))
        r, a = results[i], by_id[results[i].id]
        done.add(r.id)
        others: dict[int, int] = {}
        for j, o in enumerate(results):
            if j == i:
                continue
            oa = _after(o)
            if o.status == "melhoria" and o.id not in done:  # melhoria ainda não decidida: pode abrir mão do ganho
                oa = {t: min(n, (o.current or {}).get(t, 0)) for t, n in oa.items()}
            for t, n in oa.items():
                others[t] = others.get(t, 0) + n
        limits = {t: max(budget.get(t, 0) - others.get(t, 0), 0) for t in set(budget) | set(types)}
        if ledger is not None and r.applicable and r.placement is not None:
            ledger.commit(r.placement, a.current_placement)  # desfaz a sugestão antes de refazer
        nr = optimize_address(a, types, prm, ledger, limits)
        if nr.status == "inviavel" and r.status == "melhoria":
            # a melhoria precisaria de bins que não existem no escopo: fica como está
            nr = AddressResult(a.id, a.address, "ok", dict(a.current), r.current_metrics, suggested=dict(a.current),
                               suggested_metrics=r.current_metrics, used_volume_l=r.used_volume_l, fill_now=r.fill_now,
                               fill_after=r.fill_now, current_fits=r.current_fits, own_level=r.own_level,
                               current_placement=r.current_placement, placement=r.current_placement)
            nr.notes = list(a.notes) + ["Haveria melhoria, mas faltam bins desse tipo no escopo (bins existentes)."]
        elif nr.status == "inviavel":
            nr.notes.append("Faltam bins no escopo para corrigir (bins existentes) — simule com recurso infinito.")
        elif nr.status == "ok" and r.status == "melhoria":
            nr.notes.append("Haveria melhoria, mas faltam bins desse tipo no escopo (bins existentes).")
        elif nr.applicable and _key(nr.suggested or {}) != _key(r.suggested or {}):
            nr.notes.append("Sugestão ajustada às bins existentes no escopo.")
        if ledger is not None and nr.applicable and nr.placement is not None:
            ledger.commit(a.current_placement, nr.placement)
        results[i] = nr


def optimize(addresses: list[AddressIn], types: dict[int, BinSpec], prm: Params,
             ledger: Optional[LevelLedger] = None) -> tuple[list[AddressResult], Summary]:
    prm.validate()
    solver_info = None
    if prm.engine == "or":
        from services.or_engine import solve_addresses

        results, solver_info = solve_addresses(addresses, types, prm, ledger)
    else:
        results = greedy_addresses(addresses, types, prm, ledger)
    s = summarize(results)
    s.solver = solver_info
    return results, s


def summarize(results: list[AddressResult]) -> "Summary":
    s = Summary(addresses=len(results))
    for r in results:
        s.by_status[r.status] = s.by_status.get(r.status, 0) + 1
        s.bins_before += r.current_metrics.bins
        s.volume_before += r.current_metrics.volume_l
        s.width_before += r.current_metrics.width_cm
        after = r.suggested_metrics if r.applicable else r.current_metrics
        s.bins_after += after.bins
        s.volume_after += after.volume_l
        s.width_after += after.width_cm
        s.used_volume += r.used_volume_l
    return s


def greedy_addresses(addresses: list[AddressIn], types: dict[int, BinSpec], prm: Params,
                     ledger: Optional[LevelLedger] = None) -> list[AddressResult]:
    """Heurística: endereço por endereço, cada um com a melhor configuração que cabe no que sobrou."""
    results = []
    for a in addresses:
        r = optimize_address(a, types, prm, ledger)
        if ledger is not None and r.applicable and r.placement is not None:
            ledger.commit(a.current_placement, r.placement)
        elif ledger is not None and r.status == "liberar":
            ledger.commit(a.current_placement, {})
        results.append(r)
    if prm.limit_inventory:
        _limit_inventory(addresses, results, types, prm, ledger)
    return results


# --------------------------------------------------------------------------- #
# Foco nos SKUs filtrados: bins de outros endereços (doadores)
# --------------------------------------------------------------------------- #
@dataclass
class Donor:
    """Endereço fora do foco que pode ceder bins. `bins` = bins físicas [(tipo, nº, {sku: qtd})] (conteúdo atual)."""
    a: AddressIn
    bins: list = field(default_factory=list)

    @property
    def stocked(self) -> bool:
        return any(p.qty and p.qty > 0 for p in self.a.items)


def _remove_from_placement(pl: dict, own, take: dict[int, int]) -> dict:
    """Tira `take` bins do arranjo por nível: primeiro dos níveis extras, por último do nível do próprio endereço."""
    out = {k: dict(v) for k, v in (pl or {}).items()}
    for t, n in take.items():
        for k in sorted(out, key=lambda k: k == own):
            if n <= 0:
                break
            d = min(n, out[k].get(t, 0))
            if d:
                out[k][t] -= d
                n -= d
    return {k: {t: n for t, n in v.items() if n > 0} for k, v in out.items() if any(v.values())}


def _emptiest(d: Donor, t: int, k: int) -> list:
    """As `k` bins do tipo `t` com menos conteúdo (são as que o doador cede)."""
    bins = [b for b in d.bins if b[0] == t]
    bins.sort(key=lambda b: (sum(b[2].values()), -b[1]))
    return bins[:k]


def donors_inventory(donors: list[Donor], prm: Params) -> dict[int, int]:
    """Bins dos doadores que o plano dos itens filtrados pode usar."""
    inv: dict[int, int] = {}
    for d in donors:
        if d.stocked and prm.bin_source != "any":
            continue
        for t, n in (d.a.current or {}).items():
            inv[t] = inv.get(t, 0) + n
    return inv


def assign_donors(results: list[AddressResult], donors: list[Donor], types: dict[int, BinSpec], prm: Params,
                  ledger: Optional[LevelLedger] = None) -> tuple[list[AddressResult], dict[int, int], list]:
    """Depois do plano dos endereços filtrados, decide DE ONDE vêm as bins que eles ganham: primeiro de endereços
    sem saldo; com "qualquer bin", também de endereços com saldo de outros itens — que são rearranjados nas bins
    que sobram (realocar) ou cedem as bins mais vazias e o conteúdo delas fica fora de bin (órfãos).
    Retorna (resultados dos doadores, bins que faltaram {tipo: qtd}, órfãos [(sku, endereço, bin, qtd)])."""
    need: dict[int, int] = {}
    spare: dict[int, int] = {}
    for r in results:
        after = _after(r)
        for t in set(after) | set(r.current or {}):
            d = after.get(t, 0) - (r.current or {}).get(t, 0)
            if d > 0:
                need[t] = need.get(t, 0) + d
            elif d < 0:
                spare[t] = spare.get(t, 0) - d
    # o que os próprios endereços filtrados liberam cobre primeiro
    for t in list(need):
        c = min(need[t], spare.get(t, 0))
        need[t] -= c
        spare[t] = spare.get(t, 0) - c
    need = {t: n for t, n in need.items() if n > 0}
    out: dict[int, AddressResult] = {}
    taken: dict[int, dict[int, int]] = {}
    orphans: list = []
    code = lambda t: types[t].code if t in types else str(t)  # noqa: E731

    def give(d: Donor, t: int, k: int) -> None:
        taken.setdefault(d.a.id, {})[t] = taken.get(d.a.id, {}).get(t, 0) + k
        need[t] -= k

    def remaining(d: Donor) -> dict[int, int]:
        tk = taken.get(d.a.id, {})
        return {t: n - tk.get(t, 0) for t, n in d.a.current.items() if n - tk.get(t, 0) > 0}

    # 1) endereços sem saldo
    for d in sorted((d for d in donors if not d.stocked), key=lambda d: d.a.address):
        for t in list(need):
            k = min(need[t], remaining(d).get(t, 0))
            if k > 0:
                give(d, t, k)
        if not any(need.values()):
            break
    for d in donors:
        if d.a.id in taken and not d.stocked:
            left = remaining(d)
            pl = _remove_from_placement(d.a.current_placement, d.a.own_level, taken[d.a.id])
            r = AddressResult(d.a.id, d.a.address, "liberar", dict(d.a.current), metrics(d.a.current, types),
                              suggested=left, suggested_metrics=metrics(left, types), own_level=d.a.own_level,
                              current_placement={k: dict(v) for k, v in d.a.current_placement.items()},
                              placement=pl if d.a.current_placement else None)
            r.notes.append("Sem saldo: cede " + ", ".join(f"{n}× {code(t)}" for t, n in sorted(taken[d.a.id].items()))
                           + " para os itens filtrados.")
            if ledger is not None and d.a.current_placement:
                ledger.commit(d.a.current_placement, pl)
            out[d.a.id] = r

    # 2) endereços com saldo de outros itens
    if prm.bin_source == "any" and any(n > 0 for n in need.values()):
        stocked = [d for d in donors if d.stocked]
        for t in sorted(need, key=lambda t: -need[t]):
            # quem desaloja menos unidades primeiro (bins mais vazias), depois quem tem mais bins do tipo
            def cost(d):
                k = min(need[t], d.a.current.get(t, 0))
                return (sum(sum(b[2].values()) for b in _emptiest(d, t, k)) / max(k, 1), -d.a.current.get(t, 0), d.a.address)
            for d in sorted((d for d in stocked if d.a.current.get(t, 0) > 0 and d.a.id not in out), key=cost):
                if need[t] <= 0:
                    break
                k = min(need[t], d.a.current.get(t, 0))
                if prm.orphans == "leave":
                    give(d, t, k)
                    gone = _emptiest(d, t, k)
                    left = remaining(d)
                    pl = _remove_from_placement(d.a.current_placement, d.a.own_level, {t: k})
                    r = AddressResult(d.a.id, d.a.address, "orfao", dict(d.a.current), metrics(d.a.current, types),
                                      suggested=left, suggested_metrics=metrics(left, types), own_level=d.a.own_level,
                                      current_placement={kk: dict(v) for kk, v in d.a.current_placement.items()},
                                      placement=pl if d.a.current_placement else None)
                    r.removed_bins = [(b[0], b[1]) for b in gone]
                    loose = [(sku, f"{code(b[0])} #{b[1]}", q) for b in gone for sku, q in b[2].items() if q > 0]
                    orphans += [(sku, d.a.address, tag, q) for sku, tag, q in loose]
                    r.notes.append(f"Cede {k}× {code(t)} para os itens filtrados"
                                   + ("; ficam fora de bin: " + ", ".join(f"{sku} {q:g} un." for sku, _tg, q in loose)
                                      if loose else " (bins vazias).") )
                    if ledger is not None and d.a.current_placement:
                        ledger.commit(d.a.current_placement, pl)
                    out[d.a.id] = r
                    continue
                # realocar: o doador é refeito só com as bins que ficam com ele + as que sobraram no plano
                # (o ponto de partida é "atual menos as bins cedidas": menos trocas = só tirar as bins)
                rest = {tt: n for tt, n in d.a.current.items()}
                rest[t] = rest.get(t, 0) - k
                rest = {tt: n for tt, n in rest.items() if n > 0}
                rest_pl = _remove_from_placement(d.a.current_placement, d.a.own_level, {t: k})
                lim = dict(rest)
                for tt, n in spare.items():
                    lim[tt] = lim.get(tt, 0) + n
                a2 = AddressIn(d.a.id, d.a.address, rest, d.a.items, d.a.linked_without_stock, d.a.own_level,
                               rest_pl, d.a.bay_levels, [])
                if ledger is not None and d.a.current_placement:
                    ledger.commit(d.a.current_placement, rest_pl)
                nr = optimize_address(a2, types, prm, ledger, lim)
                cfg = nr.suggested if nr.suggested is not None else dict(rest)
                ok = nr.status in ("melhoria", "corrigir", "ok") and all(n <= lim.get(tt, 0) for tt, n in cfg.items() if n)
                if not ok:
                    if ledger is not None and d.a.current_placement:
                        ledger.commit(rest_pl, d.a.current_placement)
                    continue
                give(d, t, k)
                for tt in set(cfg) | set(rest):
                    spare[tt] = spare.get(tt, 0) - (cfg.get(tt, 0) - rest.get(tt, 0))  # usa / devolve sobras
                pl = nr.placement if nr.placement is not None else (rest_pl if d.a.current_placement else None)
                if ledger is not None and nr.placement is not None:
                    ledger.commit(rest_pl, nr.placement)
                nr.status = "realocar"
                nr.current, nr.current_metrics = dict(d.a.current), metrics(d.a.current, types)
                nr.current_placement = {kk: dict(v) for kk, v in d.a.current_placement.items()}
                nr.suggested, nr.suggested_metrics, nr.placement = dict(cfg), metrics(cfg, types), pl
                if nr.used_volume_l and nr.current_metrics.volume_l:
                    nr.fill_now = nr.used_volume_l / nr.current_metrics.volume_l * 100.0
                if nr.used_volume_l and nr.suggested_metrics.volume_l:
                    nr.fill_after = nr.used_volume_l / nr.suggested_metrics.volume_l * 100.0
                nr.notes.append(f"Cede {k}× {code(t)} para os itens filtrados"
                                + (" e é rearranjado com o que sobra." if _key(cfg) != _key(rest) else
                                   "; os itens cabem nas bins que ficam."))
                out[d.a.id] = nr
    uncovered = {t: n for t, n in need.items() if n > 0}
    return list(out.values()), uncovered, orphans
