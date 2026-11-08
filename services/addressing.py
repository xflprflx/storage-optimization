"""
Regras de endereçamento: normalização, montagem/parse de códigos de endereço
e validação de conflitos de hierarquia (posição genérica x compartimentos).
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from models import StorageLocation

SEPARATOR = "-"
_SPLIT_RE = re.compile(r"[-./|\\\s]+")

AddressParts = tuple[str, str, str, str, str]  # aisle, bay, level, position, compartment


def normalize_code(value: object) -> str:
    """Remove espaços nas pontas e converte para maiúsculas."""
    if value is None:
        return ""
    return str(value).strip().upper()


def build_address(aisle: str, bay: str, level: str, position: str, compartment: str = "") -> str:
    """Monta o código textual do endereço. Ex.: A-01-02-03 ou A-01-02-03-B.

    Segmentos vazios ao final da hierarquia são omitidos (ex.: 'DOCA' apenas corredor).
    """
    parts = [aisle or "", bay or "", level or "", position or ""]
    while parts and parts[-1] == "":
        parts.pop()
    code = SEPARATOR.join(parts)
    if compartment:
        code = f"{code}{SEPARATOR}{compartment}" if code else compartment
    return code


def parse_address(text: object) -> AddressParts:
    """Converte 'A-01-02-03-B' (ou 'A.01.02.03', 'A 01 02 03') em suas 5 partes.

    Segmentos ausentes viram string vazia. Segmentos excedentes (>5) são
    agregados ao compartimento.
    """
    raw = normalize_code(text)
    if not raw:
        raise ValueError("Endereço vazio")
    pieces = [p for p in _SPLIT_RE.split(raw) if p != ""]
    if not pieces:
        raise ValueError(f"Endereço inválido: {text!r}")
    if len(pieces) > 5:
        pieces = pieces[:4] + [SEPARATOR.join(pieces[4:])]
    while len(pieces) < 5:
        pieces.append("")
    aisle, bay, level, position, compartment = pieces
    for seg in pieces:
        if len(seg) > 20:
            raise ValueError(f"Segmento de endereço muito longo (máx. 20): {seg!r}")
    return aisle, bay, level, position, compartment


def scope_filter(stmt, branch_id: int, warehouse_id: Optional[int]):
    """Aplica o filtro de escopo (filial + armazém, tratando armazém nulo)."""
    stmt = stmt.where(StorageLocation.branch_id == branch_id)
    if warehouse_id is None:
        return stmt.where(StorageLocation.warehouse_id.is_(None))
    return stmt.where(StorageLocation.warehouse_id == warehouse_id)


def check_location_conflict(
    db: Session,
    branch_id: int,
    warehouse_id: Optional[int],
    parts: AddressParts,
    exclude_id: Optional[int] = None,
) -> Optional[str]:
    """Retorna mensagem de conflito ou None se o endereço puder ser gravado.

    Os endereços formam uma árvore (Corredor > Estante > Nível > Posição > Compartimento),
    e só o Corredor é obrigatório. Um endereço é "genérico" quando para antes do último nível.
    Regras:
      1. Endereço idêntico já existe -> conflito.
      2. Já existe um endereço genérico ACIMA deste (ex.: A-05 ao criar A-05-01) -> conflito.
      3. Já existem sub-endereços ABAIXO deste (ex.: A-05-01 ao criar A-05) -> conflito.
    """
    from sqlalchemy import func

    from services.app_settings import canon, get

    seg = segments(parts)
    n = len(seg)
    cols = level_columns()
    # com "07 = 7" (Configurações), compara os códigos sem os zeros à esquerda
    zeq = bool(get("addr_zero_eq"))
    C = (lambda i: func.nk_canon(cols[i])) if zeq else (lambda i: cols[i])  # noqa: E731
    V = canon if zeq else (lambda v: v)  # noqa: E731
    full = lambda values: and_(*[C(i) == V(values[i] if i < len(values) else "") for i in range(5)])  # noqa: E731
    conds = [full(seg)]
    conds += [full(seg[:k]) for k in range(1, n)]  # ancestrais
    if n < 5:
        conds.append(and_(*[C(i) == V(seg[i]) for i in range(n)], cols[n] != ""))  # descendentes
    stmt = scope_filter(select(StorageLocation.address, *cols), branch_id, warehouse_id).where(or_(*conds))
    if exclude_id is not None:
        stmt = stmt.where(StorageLocation.id != exclude_id)
    rows = db.execute(stmt.limit(50)).all()
    if not rows:
        return None
    by_kind: dict[str, list[str]] = {"exact": [], "ancestor": [], "descendant": []}
    for r in rows:
        other = segments((r.aisle, r.bay, r.level, r.position, r.compartment))
        if tuple(map(V, other)) == tuple(map(V, seg)):
            by_kind["exact"].append(r.address)
        elif len(other) < n:
            by_kind["ancestor"].append(r.address)
        else:
            by_kind["descendant"].append(r.address)
    if by_kind["exact"]:
        return f"O endereço {by_kind['exact'][0]} já existe neste depósito."
    if by_kind["ancestor"]:
        return (
            f"Conflito de hierarquia: já existe o endereço genérico {by_kind['ancestor'][0]} acima deste; "
            "não é possível criar sub-endereços nele."
        )
    sample = ", ".join(sorted(by_kind["descendant"])[:5])
    return (
        f"Conflito de hierarquia: este endereço já possui sub-endereços ({sample}); "
        "não é possível criá-lo como genérico."
    )


def segments(parts) -> tuple[str, ...]:
    """Partes preenchidas, a partir do corredor, até o primeiro nível vazio."""
    out = []
    for p in parts:
        if not p:
            break
        out.append(p)
    return tuple(out)


def level_columns():
    return [
        StorageLocation.aisle,
        StorageLocation.bay,
        StorageLocation.level,
        StorageLocation.position,
        StorageLocation.compartment,
    ]


def find_or_create_location(
    db: Session,
    branch_id: int,
    warehouse_id: Optional[int],
    address_text: str,
    cache: Optional[dict] = None,
) -> tuple[Optional[int], bool, Optional[str]]:
    """Localiza (ou cria) um endereço no escopo informado.

    Retorna (location_id, criado?, erro). Usa `cache` (dict) opcional para
    evitar consultas repetidas durante importações em lote.
    """
    try:
        parts = parse_address(address_text)
    except ValueError as exc:
        return None, False, str(exc)
    address = build_address(*parts)
    key = (branch_id, warehouse_id, address)
    if cache is not None and key in cache:
        return cache[key], False, None

    stmt = scope_filter(select(StorageLocation.id), branch_id, warehouse_id).where(
        StorageLocation.aisle == parts[0],
        StorageLocation.bay == parts[1],
        StorageLocation.level == parts[2],
        StorageLocation.position == parts[3],
        StorageLocation.compartment == parts[4],
    )
    loc_id = db.scalar(stmt)
    created = False
    if loc_id is None:
        conflict = check_location_conflict(db, branch_id, warehouse_id, parts)
        if conflict:
            return None, False, conflict
        loc = StorageLocation(
            branch_id=branch_id,
            warehouse_id=warehouse_id,
            aisle=parts[0],
            bay=parts[1],
            level=parts[2],
            position=parts[3],
            compartment=parts[4],
            address=address,
            is_occupied=False,
        )
        db.add(loc)
        db.flush()
        loc_id = loc.id
        created = True
    if cache is not None:
        cache[key] = loc_id
    return loc_id, created, None


# --------------------------------------------------------------------------- #
# Resolução tolerante de endereços (usada pelas importações)
# --------------------------------------------------------------------------- #
def equivalence_key(seg: tuple[str, ...]) -> tuple[str, ...]:
    """Chave de equivalência: segmentos numéricos sem zeros à esquerda ('01' == '1')."""
    return tuple(s.lstrip("0") or "0" if s.isdigit() else s for s in seg)


def compact_key(text: str) -> str:
    """Endereço sem separadores ('A-01-01-02' -> 'A010102')."""
    return _SPLIT_RE.sub("", normalize_code(text))


class LocationRef:
    __slots__ = ("id", "branch_id", "warehouse_id", "address")

    def __init__(self, id, branch_id, warehouse_id, address):
        self.id, self.branch_id, self.warehouse_id, self.address = id, branch_id, warehouse_id, address


class LocationResolver:
    """Encontra o endereço existente que corresponde ao texto da planilha antes de criar um novo.

    Ordem de busca, no depósito (filial + armazém) informado:
      1. endereço idêntico                 (A-01-01-02)
      2. equivalente sem zeros à esquerda  (A-1-1-2  -> A-01-01-02)
      3. mesmo endereço sem separadores     (A010102  -> A-01-01-02)
    Se o ARMAZÉM não foi informado e nada for achado sem armazém, procura o endereço nos
    armazéns da filial e usa-o quando houver exatamente uma correspondência.
    Sem filial (somente na importação de produtos), procura em todas as filiais.
    Só cria um endereço novo quando nada corresponder (e se `create=True`).

    Índices são carregados por depósito uma única vez e mantidos em memória durante a importação.
    """

    def __init__(self, db: Session):
        self.db = db
        self._scopes: dict = {}  # (branch_id, warehouse_id) -> índices
        self._by_branch: dict = {}  # branch_id -> lista de depósitos carregados daquela filial

    # ---------- índices ----------
    def _new_index(self):
        return {"exact": {}, "equiv": {}, "compact": {}}

    def _add(self, idx, ref: LocationRef, seg: tuple[str, ...]):
        idx["exact"][ref.address] = ref
        idx["equiv"].setdefault(equivalence_key(seg), []).append(ref)
        idx["compact"].setdefault(compact_key(ref.address), []).append(ref)

    def _index(self, branch_id: int, warehouse_id: Optional[int]):
        key = (branch_id, warehouse_id)
        if key not in self._scopes:
            idx = self._new_index()
            cols = level_columns()
            stmt = scope_filter(select(StorageLocation.id, StorageLocation.address, *cols), branch_id, warehouse_id)
            for r in self.db.execute(stmt):
                seg = segments((r.aisle, r.bay, r.level, r.position, r.compartment))
                self._add(idx, LocationRef(r.id, branch_id, warehouse_id, r.address), seg)
            self._scopes[key] = idx
        return self._scopes[key]

    def _branch_scopes(self, branch_id: int) -> list:
        if branch_id not in self._by_branch:
            whs = self.db.scalars(select(StorageLocation.warehouse_id).where(
                StorageLocation.branch_id == branch_id).distinct()).all()
            self._by_branch[branch_id] = list(whs)
        return [(branch_id, w) for w in self._by_branch[branch_id]]

    @staticmethod
    def _lookup(idx, address: str, seg: tuple[str, ...], raw: str) -> tuple[list, str]:
        if address in idx["exact"]:
            return [idx["exact"][address]], ""
        eq = idx["equiv"].get(equivalence_key(seg), [])
        if eq:
            return eq, "equivalente"
        if len(seg) == 1:  # texto sem separadores: compara com endereços compactados
            cp = idx["compact"].get(compact_key(raw), [])
            if cp:
                return cp, "sem separadores"
        return [], ""

    # ---------- API ----------
    def resolve(self, branch_id: Optional[int], warehouse_id: Optional[int], text: str,
                create: bool = True) -> tuple[Optional[LocationRef], bool, Optional[str], Optional[str]]:
        """Retorna (endereço, criado?, erro, observação)."""
        try:
            parts = parse_address(text)
        except ValueError as exc:
            return None, False, str(exc), None
        seg = segments(parts)
        address = build_address(*parts)

        if branch_id is None:
            # Sem filial: busca em todos os depósitos (não cria).
            rows = self.db.execute(select(StorageLocation.branch_id, StorageLocation.warehouse_id).distinct()).all()
            found = []
            for b, w in rows:
                hits, _ = self._lookup(self._index(b, w), address, seg, text)
                found += hits
            if len(found) == 1:
                ref = found[0]
                return ref, False, None, None if ref.address == address else f"{text} associado a {ref.address}"
            if len(found) > 1:
                return None, False, f"Endereço {address} existe em mais de um depósito; informe a filial/armazém.", None
            return None, False, f"Endereço {address} não encontrado (informe a filial para criá-lo).", None

        idx = self._index(branch_id, warehouse_id)
        hits, _ = self._lookup(idx, address, seg, text)
        if not hits and warehouse_id is None:
            # Armazém não informado: procura nos armazéns da filial
            for b, w in self._branch_scopes(branch_id):
                if w is None:
                    continue
                h, _ = self._lookup(self._index(b, w), address, seg, text)
                hits += h
        if len(hits) > 1:
            opts = ", ".join(sorted({h.address for h in hits})[:4])
            return None, False, f"Endereço {text} é ambíguo neste depósito ({opts}).", None
        if hits:
            ref = hits[0]
            note = None
            if ref.address != address or ref.warehouse_id != warehouse_id:
                note = f"Endereço {text} associado ao existente {ref.address}"
                if ref.warehouse_id != warehouse_id:
                    note += " (armazém encontrado na filial)"
            return ref, False, None, note

        if not create:
            return None, False, f"Endereço {address} não cadastrado.", None
        conflict = check_location_conflict(self.db, branch_id, warehouse_id, parts)
        if conflict:
            return None, False, conflict, None
        loc = StorageLocation(branch_id=branch_id, warehouse_id=warehouse_id, aisle=parts[0], bay=parts[1],
                              level=parts[2], position=parts[3], compartment=parts[4], address=address,
                              is_occupied=False)
        self.db.add(loc)
        self.db.flush()
        ref = LocationRef(loc.id, branch_id, warehouse_id, address)
        self._add(idx, ref, seg)
        if branch_id in self._by_branch and warehouse_id not in self._by_branch[branch_id]:
            self._by_branch[branch_id].append(warehouse_id)
        return ref, True, None, None

    def reset(self) -> None:
        """Descarta os índices (após rollback de um lote); serão recarregados do banco."""
        self._scopes.clear()
        self._by_branch.clear()
