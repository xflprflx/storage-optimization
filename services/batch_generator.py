"""
Geração de endereços em lote.

Hierarquia estrita: Aisle -> Bay -> Level -> Position -> Compartment.
Somente o Corredor (Aisle) é obrigatório; os demais níveis podem ser "Não utilizar"
(e, quando um nível não é utilizado, nenhum nível abaixo dele é gerado).

Cada nível recebe uma regra (RangeType):
    ALPHABETIC : faixa de letras  (A..Z, também AA..AZ estilo planilha)
    NUMERIC    : faixa numérica   (01..10, com zero à esquerda preservado)
    EXACT      : um único valor fixo
    NONE       : nível não utilizado (endereço termina no nível anterior)

EXCEÇÕES: substituem a regra de um nível para determinados "pais".
    Ex.: "Nível = 01..03 quando Corredor = B e Estante = 05" (o padrão era 01..08).
    Condições aceitam: vazio (qualquer), valor único (05), lista (01,03,05),
    faixa (01-05 / A-C) ou combinações (A,C-E). Se várias exceções servirem para o
    mesmo caminho, a ÚLTIMA da lista prevalece.

Resolução de conflitos (por depósito = filial + armazém):
    EXACT              -> endereço idêntico já existe
    GENERIC_EXISTS     -> já existe endereço genérico acima (ex.: A-05 ao gerar A-05-01)
    COMPARTMENTS_EXIST -> já existem sub-endereços abaixo (ex.: A-05-01 ao gerar A-05)

Opção "Substituir posições desocupadas": remove as posições antigas conflitantes e
grava as novas, SOMENTE se todas as antigas estiverem desocupadas
(sem nenhum item atrelado: saldo ou endereço padrão de produto). Posições ocupadas nunca são sobrescritas.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from models import LocationBin, StorageLocation
from services.addressing import build_address, normalize_code, scope_filter, segments
from services.stock_service import has_items_expr
from services.i18n import N_, _

MAX_COMBINATIONS = 50_000
INSERT_CHUNK = 1_000
LEVELS = ("aisle", "bay", "level", "position", "compartment")
LEVEL_LABELS = {
    "aisle": N_("Corredor (Aisle)"),
    "bay": N_("Estante (Bay)"),
    "level": N_("Nível (Level)"),
    "position": N_("Posição (Position)"),
    "compartment": N_("Compartimento"),
}
SHORT_LABELS = {"aisle": N_("Corredor"), "bay": N_("Estante"), "level": N_("Nível"), "position": N_("Posição"),
                "compartment": N_("Compartimento")}


class RangeType(str, Enum):
    ALPHABETIC = "ALPHABETIC"
    NUMERIC = "NUMERIC"
    EXACT = "EXACT"
    NONE = "NONE"


class ConflictKind(str, Enum):
    EXACT = "EXACT"
    GENERIC_EXISTS = "GENERIC_EXISTS"
    COMPARTMENTS_EXIST = "COMPARTMENTS_EXIST"


CONFLICT_LABELS = {
    ConflictKind.EXACT: N_("Endereço idêntico já existe"),
    ConflictKind.GENERIC_EXISTS: N_("Já existe endereço genérico acima deste"),
    ConflictKind.COMPARTMENTS_EXIST: N_("Já existem sub-endereços abaixo deste"),
}


# --------------------------------------------------------------------------- #
# Conversões alfabéticas (A=1 ... Z=26, AA=27 ...)
# --------------------------------------------------------------------------- #
def letters_to_int(text: str) -> int:
    text = text.strip().upper()
    if not text or not text.isalpha() or not text.isascii():
        raise ValueError(_("Valor alfabético inválido: {text}", text=f"{text!r}"))
    n = 0
    for ch in text:
        n = n * 26 + (ord(ch) - 64)
    return n


def int_to_letters(n: int) -> str:
    if n <= 0:
        raise ValueError(_("Índice alfabético deve ser positivo"))
    out = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


# --------------------------------------------------------------------------- #
# Regras por nível
# --------------------------------------------------------------------------- #
@dataclass
class LevelRule:
    name: str
    range_type: RangeType
    start: str = ""
    end: str = ""
    value: str = ""
    padding: Optional[int] = None

    def values(self, label: Optional[str] = None) -> list[str]:
        """Valores do nível. NONE -> [] (nível não utilizado)."""
        label = label or LEVEL_LABELS.get(self.name, self.name)
        if self.range_type == RangeType.NONE:
            if self.name == "aisle":
                raise ValueError(_("{label}: o Corredor é obrigatório.", label=label))
            return []
        if self.range_type == RangeType.EXACT:
            val = normalize_code(self.value)
            if not val:
                raise ValueError(_("{label}: informe o valor fixo.", label=label))
            if len(val) > 20:
                raise ValueError(_("{label}: valor excede 20 caracteres.", label=label))
            return [val]

        start, end = normalize_code(self.start), normalize_code(self.end)
        if not start or not end:
            raise ValueError(_("{label}: informe início e fim da faixa.", label=label))

        if self.range_type == RangeType.ALPHABETIC:
            a, b = letters_to_int(start), letters_to_int(end)
            if a > b:
                raise ValueError(_("{label}: início ({start}) maior que fim ({end}).", label=label, start=start, end=end))
            return [int_to_letters(i) for i in range(a, b + 1)]

        if self.range_type == RangeType.NUMERIC:
            if not (start.isdigit() and end.isdigit()):
                raise ValueError(_("{label}: faixa numérica deve conter apenas dígitos.", label=label))
            a, b = int(start), int(end)
            if a > b:
                raise ValueError(_("{label}: início ({start}) maior que fim ({end}).", label=label, start=start, end=end))
            if self.padding is not None:
                width = max(0, int(self.padding))
            elif len(start) > 1 and start.startswith("0"):
                width = max(len(start), len(end))  # '01'..'10' => 2 dígitos
            else:
                width = 0
            return [str(i).zfill(width) for i in range(a, b + 1)]

        raise ValueError(_("{label}: tipo de faixa desconhecido.", label=label))  # pragma: no cover

    def describe(self) -> str:
        if self.range_type == RangeType.NONE:
            return _("não utilizar")
        if self.range_type == RangeType.EXACT:
            return f"= {normalize_code(self.value)}"
        return f"{normalize_code(self.start)} a {normalize_code(self.end)}"


# --------------------------------------------------------------------------- #
# Exceções
# --------------------------------------------------------------------------- #
def _cmp_key(value: str):
    """Chave para comparar valores em faixas: número, letras, ou texto."""
    if value.isdigit():
        return (0, int(value))
    if value.isalpha() and value.isascii():
        return (1, letters_to_int(value))
    return (2, value)


class ValueMatcher:
    """Condição de um nível-pai: '', '05', '01,03', '01-05', 'A-C', 'A,C-E', '*'."""

    def __init__(self, expr: str, label: str):
        self.expr = normalize_code(expr)
        self.any = self.expr in ("", "*")
        self.items: list[tuple] = []
        if self.any:
            return
        for token in self.expr.replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                lo, _x, hi = token.partition("-")
                lo, hi = lo.strip(), hi.strip()
                if not lo or not hi:
                    raise ValueError(_("{label}: faixa inválida '{token}'.", label=label, token=token))
                klo, khi = _cmp_key(lo), _cmp_key(hi)
                if klo[0] != khi[0] or klo[0] == 2:
                    raise ValueError(_("{label}: faixa '{token}' deve ser só números ou só letras.", label=label, token=token))
                if klo > khi:
                    raise ValueError(_("{label}: faixa '{token}' com início maior que o fim.", label=label, token=token))
                self.items.append(("range", klo, khi))
            else:
                self.items.append(("eq", token))
        if not self.items:
            self.any = True

    def matches(self, value: str) -> bool:
        if self.any:
            return True
        key = None
        for item in self.items:
            if item[0] == "eq":
                if value == item[1] or (value.isdigit() and item[1].isdigit() and int(value) == int(item[1])):
                    return True
            else:
                if key is None:
                    key = _cmp_key(value)
                if item[1][0] == key[0] and item[1] <= key <= item[2]:
                    return True
        return False


@dataclass
class BatchException:
    target: str  # nível afetado (bay, level, position, compartment)
    conditions: dict[str, str]  # nível-pai -> expressão (vazio = qualquer)
    rule: LevelRule
    index: int = 0
    _matchers: dict = field(default_factory=dict, repr=False)

    def prepare(self) -> None:
        if self.target not in LEVELS[1:]:
            raise ValueError(_("Exceção {v}: nível afetado inválido.", v=self.index + 1))
        parents = LEVELS[: LEVELS.index(self.target)]
        self._matchers = {}
        for lvl in parents:
            expr = self.conditions.get(lvl, "")
            m = ValueMatcher(expr, _("Exceção {v} ({v2})", v=self.index + 1, v2=SHORT_LABELS[lvl]))
            if not m.any:
                self._matchers[lvl] = m
        # valida a regra já na preparação (mensagem com o número da exceção)
        self.rule.values(label=_("Exceção {v} ({v2})", v=self.index + 1, v2=SHORT_LABELS[self.target]))

    def matches(self, path: tuple[str, ...]) -> bool:
        for lvl, m in self._matchers.items():
            if not m.matches(path[LEVELS.index(lvl)]):
                return False
        return True

    def describe(self) -> str:
        conds = [f"{SHORT_LABELS[l]} = {m.expr}" for l, m in self._matchers.items()]
        when = " e ".join(conds) if conds else _("todos os caminhos")
        return f"{SHORT_LABELS[self.target]}: {self.rule.describe()} quando {when}"


@dataclass
class BatchRequest:
    branch_id: int
    warehouse_id: Optional[int]
    rules: dict[str, LevelRule]
    replace_unoccupied: bool = False
    exceptions: list[BatchException] = field(default_factory=list)
    bins: list[tuple[int, int]] = field(default_factory=list)  # (bin_type_id, quantidade) p/ cada endereço novo


@dataclass
class BatchConflict:
    address: str
    kind: ConflictKind
    existing: list[str]
    blocked_by_occupied: bool = False

    @property
    def message(self) -> str:
        base = CONFLICT_LABELS[self.kind]
        if self.blocked_by_occupied:
            return _("{base} — posição OCUPADA, não pode ser substituída", base=base)
        return base


@dataclass
class BatchResult:
    structure_note: str = ""  # estrutura de armazenagem aplicada às estantes geradas
    total_combinations: int = 0
    to_create: int = 0
    created: int = 0
    replaced_exact: int = 0  # idênticas desocupadas mantidas (mesmo ID)
    deleted_old: int = 0  # posições antigas removidas na substituição
    conflicts: list[BatchConflict] = field(default_factory=list)
    conflict_count: int = 0
    blocked_count: int = 0
    sample: list[str] = field(default_factory=list)
    executed: bool = False
    elapsed_ms: int = 0
    values_per_level: dict[str, list[str]] = field(default_factory=dict)
    exception_usage: list[tuple[str, int]] = field(default_factory=list)  # (descrição, nº de caminhos)
    bins_assigned: int = 0  # endereços novos que receberam bins
    # para a pré-visualização no mapa
    new_paths: list = field(default_factory=list)  # [(segmentos, endereço)] a criar
    removed_ids: set = field(default_factory=set)  # endereços antigos que seriam removidos
    conflict_addresses: dict = field(default_factory=dict)  # endereço existente -> motivo

    MAX_CONFLICTS_LISTED = 300

    def add_conflict(self, c: BatchConflict) -> None:
        if c.blocked_by_occupied:
            self.blocked_count += 1
        else:
            self.conflict_count += 1
        if len(self.conflicts) < self.MAX_CONFLICTS_LISTED:
            self.conflicts.append(c)


@dataclass
class _Existing:
    id: Optional[int]
    address: str
    occupied: bool
    seg: tuple = ()


def validate_rules(rules: dict[str, LevelRule]) -> None:
    """Corredor obrigatório; nível 'não utilizado' implica todos os de baixo não utilizados."""
    aisle = rules.get("aisle")
    if aisle is None or aisle.range_type == RangeType.NONE:
        raise ValueError(_("O Corredor é obrigatório."))
    unused_from = None
    for name in LEVELS:
        rule = rules.get(name)
        is_none = rule is None or rule.range_type == RangeType.NONE
        if is_none and unused_from is None:
            unused_from = name
        elif not is_none and unused_from is not None:
            raise ValueError(
                _("{v} não pode ser utilizado porque {v2} está como 'Não utilizar' (hierarquia estrita).", v=LEVEL_LABELS[name], v2=LEVEL_LABELS[unused_from])
            )


def expand(rules: dict[str, LevelRule], exceptions: list[BatchException],
           usage: Optional[list[int]] = None) -> Iterator[tuple[str, ...]]:
    """Gera os caminhos (tuplas de segmentos) aplicando regras e exceções."""
    cache: dict[int, list[str]] = {}

    def values_of(rule: LevelRule) -> list[str]:
        key = id(rule)
        if key not in cache:
            cache[key] = rule.values()
        return cache[key]

    def rec(depth: int, path: tuple[str, ...]):
        if depth == len(LEVELS):
            yield path
            return
        name = LEVELS[depth]
        rule = rules.get(name) or LevelRule(name, RangeType.NONE)
        for exc in reversed(exceptions):  # a última exceção aplicável prevalece
            if exc.target == name and exc.matches(path):
                rule = exc.rule
                if usage is not None:
                    usage[exc.index] += 1
                break
        vals = values_of(rule)
        if not vals:  # nível não utilizado: o endereço termina aqui
            yield path
            return
        for v in vals:
            yield from rec(depth + 1, path + (v,))

    yield from rec(0, ())


def _to_parts(path: tuple[str, ...]) -> tuple[str, str, str, str, str]:
    p = list(path) + [""] * (5 - len(path))
    return p[0], p[1], p[2], p[3], p[4]


def run_batch(db: Session, req: BatchRequest, execute: bool) -> BatchResult:
    """Simula (execute=False) ou executa (execute=True) a geração em lote."""
    t0 = time.perf_counter()
    result = BatchResult()
    validate_rules(req.rules)
    for i, exc in enumerate(req.exceptions):
        exc.index = i
        exc.prepare()
    result.values_per_level = {n: (r.values() if (r := req.rules.get(n)) else []) for n in LEVELS}

    usage = [0] * len(req.exceptions)
    paths: list[tuple[str, ...]] = []
    for path in expand(req.rules, req.exceptions, usage):
        paths.append(path)
        if len(paths) > MAX_COMBINATIONS:
            raise ValueError(
                _("A geração passa de {MAX_COMBINATIONS} endereços (limite por lote). Divida a geração em lotes menores.", MAX_COMBINATIONS=f"{MAX_COMBINATIONS:,}")
            )
    result.total_combinations = len(paths)
    result.exception_usage = [(e.describe(), usage[e.index]) for e in req.exceptions]

    # ---- Carrega endereços existentes do depósito (corredores do lote) ----
    has_items = has_items_expr(StorageLocation.id, correlate=StorageLocation)
    aisles = sorted({p[0] for p in paths})
    existing: dict[tuple[str, ...], _Existing] = {}
    # descendentes: prefixo próprio -> conjunto de endereços (tuplas) abaixo dele
    below: dict[tuple[str, ...], set[tuple[str, ...]]] = {}

    def index_add(seg: tuple[str, ...], item: _Existing) -> None:
        existing[seg] = item
        for k in range(1, len(seg)):
            below.setdefault(seg[:k], set()).add(seg)

    def index_remove(seg: tuple[str, ...]) -> None:
        existing.pop(seg, None)
        for k in range(1, len(seg)):
            s = below.get(seg[:k])
            if s:
                s.discard(seg)

    for i in range(0, len(aisles), 900):
        stmt = scope_filter(
            select(StorageLocation.id, StorageLocation.aisle, StorageLocation.bay, StorageLocation.level,
                   StorageLocation.position, StorageLocation.compartment, StorageLocation.address,
                   StorageLocation.is_occupied, has_items.label("has_items")),
            req.branch_id, req.warehouse_id,
        ).where(StorageLocation.aisle.in_(aisles[i : i + 900]))
        for row in db.execute(stmt):
            seg = segments((row.aisle, row.bay, row.level, row.position, row.compartment))
            index_add(seg, _Existing(row.id, row.address, bool(row.is_occupied or row.has_items), seg))

    to_insert: list[dict] = []
    to_delete: set[int] = set()

    for path in paths:
        parts = _to_parts(path)
        address = build_address(*parts)

        kind: Optional[ConflictKind] = None
        conflicting: list[_Existing] = []
        if path in existing:
            kind, conflicting = ConflictKind.EXACT, [existing[path]]
        else:
            ancestors = [existing[path[:k]] for k in range(1, len(path)) if path[:k] in existing]
            if ancestors:
                kind, conflicting = ConflictKind.GENERIC_EXISTS, ancestors
            else:
                descendants = below.get(path)
                if descendants:
                    kind = ConflictKind.COMPARTMENTS_EXIST
                    conflicting = [existing[d] for d in descendants]

        if kind is not None:
            old = [e for e in conflicting if e.id is not None]
            conflict = BatchConflict(address, kind, sorted(e.address for e in old))
            if not req.replace_unoccupied:
                result.add_conflict(conflict)
                for e in old:
                    result.conflict_addresses.setdefault(e.address, _("conflito com {address}: {message}", address=address, message=conflict.message))
                continue
            if any(e.occupied for e in old):
                conflict.blocked_by_occupied = True
                result.add_conflict(conflict)
                for e in old:
                    result.conflict_addresses.setdefault(e.address, f"bloqueado (ocupado): {address}")
                continue
            if kind == ConflictKind.EXACT:
                # Idêntico e desocupado: mantém o registro (mesmo ID) para não perder referências.
                result.replaced_exact += 1
                continue
            for e in old:
                to_delete.add(e.id)
                index_remove(e.seg)

        to_insert.append({
            "branch_id": req.branch_id,
            "warehouse_id": req.warehouse_id,
            "aisle": parts[0], "bay": parts[1], "level": parts[2], "position": parts[3], "compartment": parts[4],
            "address": address,
            "is_occupied": False,
        })
        index_add(path, _Existing(None, address, False, path))
        result.new_paths.append((path, address))
        if len(result.sample) < 40:
            result.sample.append(address)

    result.to_create = len(to_insert)
    result.deleted_old = len(to_delete)
    result.removed_ids = set(to_delete)

    if execute:
        if to_insert or to_delete:
            try:
                ids = sorted(to_delete)
                for i in range(0, len(ids), 900):
                    # ON DELETE CASCADE remove bins/saldos do endereço.
                    db.execute(delete(StorageLocation).where(StorageLocation.id.in_(ids[i : i + 900])))
                for i in range(0, len(to_insert), INSERT_CHUNK):
                    db.execute(insert(StorageLocation), to_insert[i : i + INSERT_CHUNK])
                if req.bins and to_insert:
                    addresses = [r["address"] for r in to_insert]
                    for i in range(0, len(addresses), 900):
                        new_ids = db.scalars(scope_filter(select(StorageLocation.id), req.branch_id, req.warehouse_id)
                                             .where(StorageLocation.address.in_(addresses[i : i + 900]))).all()
                        rows = [{"location_id": lid, "bin_type_id": t, "quantity": q}
                                for lid in new_ids for t, q in req.bins]
                        if rows:
                            db.execute(insert(LocationBin), rows)
                    result.bins_assigned = len(to_insert)
                db.commit()
            except Exception:
                db.rollback()
                raise
            result.created = len(to_insert)
        result.executed = True

    result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return result
