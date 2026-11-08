"""
Níveis físicos das estantes: capacidade ("cabem X bins do tipo Y"), uso e alocação de bins
entre o nível do próprio endereço e níveis extras da mesma estante.

Regra de uso de um nível:
    uso = Σ (bins do tipo T no nível ÷ capacidade do tipo T no nível)
    100% = nível cheio. Ex.: capacidade 6× CX-P ou 3× CX-M; com 3 CX-P + 1 CX-M -> 3/6 + 1/3 = 83%.
Se o nível tem bins de um tipo SEM capacidade cadastrada, o uso é "indefinido" para esse tipo.

Bins de um endereço (LocationBin):
    shelf_level_id NULL -> estão no nível do próprio endereço
    shelf_level_id = X  -> estão no nível X (nível extra, sempre da mesma estante)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BinType, LevelCapacity, LocationBin, ShelfLevel, StorageLocation
from services.addressing import normalize_code

EPS = 1e-9
LevelKey = tuple  # (branch_id, warehouse_id|None, aisle, bay, level)


def level_key_of(loc) -> Optional[LevelKey]:
    """Chave do nível físico de um endereço (None se o endereço não chega ao nível)."""
    if not loc.bay or not loc.level:
        return None
    return (loc.branch_id, loc.warehouse_id, loc.aisle, loc.bay, loc.level)


def _eq(col, value):
    """Comparação de código de endereço: com "07 = 7" (Configurações), sem os zeros à esquerda."""
    from sqlalchemy import func

    from services.app_settings import canon, get

    return func.nk_canon(col) == canon(value) if get("addr_zero_eq") else col == value


def _ck(key):
    """Chave de nível para comparar (códigos equivalentes, conforme a configuração)."""
    from services.app_settings import canon

    return tuple(canon(v) if isinstance(v, str) else v for v in key)


def _scope(stmt, model, branch_id, warehouse_id):
    stmt = stmt.where(model.branch_id == branch_id)
    if warehouse_id is None:
        return stmt.where(model.warehouse_id.is_(None))
    return stmt.where(model.warehouse_id == warehouse_id)


def get_level(db: Session, key: LevelKey) -> Optional[ShelfLevel]:
    b, w, a, bay, lv = key
    return db.scalar(_scope(select(ShelfLevel), ShelfLevel, b, w).where(
        _eq(ShelfLevel.aisle, a), _eq(ShelfLevel.bay, bay), _eq(ShelfLevel.level, lv)).limit(1))


def ensure_level(db: Session, key: LevelKey) -> ShelfLevel:
    lvl = get_level(db, key)
    if lvl is None:
        b, w, a, bay, lv = key
        lvl = ShelfLevel(branch_id=b, warehouse_id=w, aisle=a, bay=bay, level=lv, notes="")
        db.add(lvl)
        db.flush()
    return lvl


def bay_levels(db: Session, branch_id: int, warehouse_id: Optional[int], aisle: str, bay: str) -> list[dict]:
    """Níveis de uma estante: os cadastrados (inclusive reservados) + os que têm endereços.
    Retorna [{key, level, shelf_level_id|None, has_locations}] em ordem natural."""
    from services.layout_service import natural_key

    from services.app_settings import canon

    out: dict[str, dict] = {}
    for lv in db.scalars(_scope(select(ShelfLevel), ShelfLevel, branch_id, warehouse_id)
                         .where(_eq(ShelfLevel.aisle, aisle), _eq(ShelfLevel.bay, bay))):
        out.setdefault(canon(lv.level), {"level": lv.level, "shelf_level_id": lv.id, "has_locations": False})
    for (lv,) in db.execute(_scope(select(StorageLocation.level).distinct(), StorageLocation, branch_id, warehouse_id)
                            .where(_eq(StorageLocation.aisle, aisle), _eq(StorageLocation.bay, bay),
                                   StorageLocation.level != "")):
        out.setdefault(canon(lv), {"level": lv, "shelf_level_id": None, "has_locations": True})["has_locations"] = True
    return [dict(v, key=(branch_id, warehouse_id, aisle, bay, v["level"])) for v in
            sorted(out.values(), key=lambda d: natural_key(d["level"]))]


# --------------------------------------------------------------------------- #
# Uso dos níveis
# --------------------------------------------------------------------------- #
@dataclass
class LevelUsage:
    key: LevelKey
    shelf_level_id: Optional[int] = None
    capacity: dict[int, int] = field(default_factory=dict)  # tipo -> máx. bins
    used: dict[int, int] = field(default_factory=dict)  # tipo -> bins no nível (próprias + emprestadas)
    borrowed: dict[int, int] = field(default_factory=dict)  # tipo -> bins de endereços de OUTROS níveis
    lent: int = 0  # bins de endereços deste nível que estão em outros níveis
    height: Optional[float] = None  # distância entre níveis (cm)

    @property
    def has_capacity(self) -> bool:
        return bool(self.capacity)

    @property
    def unknown_types(self) -> list[int]:
        return [t for t, n in self.used.items() if n and t not in self.capacity]

    @property
    def fraction(self) -> Optional[float]:
        """Fração usada (1.0 = cheio). None se não há capacidade ou há tipo sem capacidade."""
        if not self.capacity or self.unknown_types:
            return None
        return sum(n / self.capacity[t] for t, n in self.used.items() if n)

    @property
    def free_fraction(self) -> Optional[float]:
        f = self.fraction
        return None if f is None else max(0.0, 1.0 - f)

    @property
    def over(self) -> bool:
        f = self.fraction
        return f is not None and f > 1.0 + EPS


def usage_for(db: Session, keys: Iterable[LevelKey]) -> dict[LevelKey, LevelUsage]:
    """Calcula capacidade e uso de um conjunto de níveis (consultas agrupadas por estante)."""
    keys = set(keys)
    res: dict[LevelKey, LevelUsage] = {k: LevelUsage(k) for k in keys}
    bays: dict[tuple, set[str]] = {}
    for b, w, a, bay, lv in keys:
        bays.setdefault((b, w, a, bay), set()).add(lv)

    by_ck = {_ck(k): k for k in keys}  # códigos equivalentes ("07" = "7", conforme a configuração)
    for (b, w, a, bay), lvls in bays.items():
        # níveis cadastrados + capacidades
        id_to_key: dict[int, LevelKey] = {}
        for sl in db.scalars(_scope(select(ShelfLevel), ShelfLevel, b, w).where(
                _eq(ShelfLevel.aisle, a), _eq(ShelfLevel.bay, bay))):
            k = by_ck.get(_ck((b, w, a, bay, sl.level)), (b, w, a, bay, sl.level))
            id_to_key[sl.id] = k
            if k in res:
                res[k].shelf_level_id = sl.id
                res[k].height = float(sl.height_cm) if sl.height_cm is not None else None
        if id_to_key:
            for lid, tid, mx in db.execute(select(LevelCapacity.shelf_level_id, LevelCapacity.bin_type_id,
                                                  LevelCapacity.max_bins)
                                           .where(LevelCapacity.shelf_level_id.in_(list(id_to_key)))):
                k = id_to_key[lid]
                if k in res:
                    res[k].capacity[tid] = mx
        # bins dos endereços desta estante (próprias e enviadas a outros níveis) + bins recebidas
        rows = db.execute(
            _scope(select(StorageLocation.level, LocationBin.bin_type_id, LocationBin.quantity, LocationBin.shelf_level_id)
                   .join(LocationBin, LocationBin.location_id == StorageLocation.id), StorageLocation, b, w)
            .where(_eq(StorageLocation.aisle, a), _eq(StorageLocation.bay, bay))
        ).all()
        for own_level, tid, qty, sl_id in rows:
            if sl_id is None:
                if not own_level:
                    continue  # endereço acima do nível (ex.: só estante): não ocupa nível
                k = by_ck.get(_ck((b, w, a, bay, own_level)), (b, w, a, bay, own_level))
                if k in res:
                    res[k].used[tid] = res[k].used.get(tid, 0) + qty
            else:
                k = id_to_key.get(sl_id)
                if k in res:
                    res[k].used[tid] = res[k].used.get(tid, 0) + qty
                    if own_level != k[4]:
                        res[k].borrowed[tid] = res[k].borrowed.get(tid, 0) + qty
                if own_level and own_level != (k[4] if k else None):
                    ok = (b, w, a, bay, own_level)
                    if ok in res:
                        res[ok].lent += qty
    # Compartimentos (frações): sem capacidade própria, valem a da bin pai × compartimentos por pai
    fracs = [(bt.id, bt.parent_id, bt.per_parent) for bt in db.scalars(select(BinType).where(BinType.parent_id.is_not(None)))
             if bt.is_fraction]
    for u in res.values():
        for tid, pid, k in fracs:
            if tid not in u.capacity and pid in u.capacity:
                u.capacity[tid] = u.capacity[pid] * k
    return res


def location_level_options(db: Session, loc: StorageLocation) -> list[dict]:
    """Opções de nível para as bins de um endereço: próprio nível + demais níveis da estante."""
    if not loc.bay:
        return []
    from services.app_settings import canon

    return [lv for lv in bay_levels(db, loc.branch_id, loc.warehouse_id, loc.aisle, loc.bay)
            if canon(lv["level"]) != canon(loc.level or "")]


def validate_bin_levels(db: Session, loc_scope: dict, rows: list[tuple[int, int, Optional[int]]]):
    """Confere se os níveis escolhidos pertencem à mesma estante do endereço.
    Retorna (erro | None, linhas normalizadas) — nível igual ao do próprio endereço vira None."""
    used = {lid for _, _, lid in rows if lid}
    if not used:
        return None, rows
    if not loc_scope.get("bay"):
        return "Este endereço não está em uma estante; as bins só podem ficar no próprio endereço.", rows
    levels = {sl.id: sl for sl in db.scalars(select(ShelfLevel).where(ShelfLevel.id.in_(used)))}
    own: set[int] = set()
    for lid in used:
        sl = levels.get(lid)
        if (sl is None or sl.branch_id != loc_scope["branch_id"] or sl.warehouse_id != loc_scope["warehouse_id"]
                or _ck((sl.aisle, sl.bay)) != _ck((loc_scope["aisle"], loc_scope["bay"]))):
            return "Nível extra inválido: precisa ser um nível da mesma estante do endereço.", rows
        if _ck((sl.level,)) == _ck((loc_scope.get("level") or "",)):
            own.add(lid)
    merged: dict[tuple, int] = {}
    for t, q, lid in rows:
        key = (t, None if lid in own else lid)
        merged[key] = merged.get(key, 0) + q
    return None, [(t, q, lid) for (t, lid), q in merged.items()]


def tallest_bins(db: Session) -> dict[LevelKey, float]:
    """Altura da bin mais alta em cada nível físico (bins próprias + bins vindas de outros níveis)."""
    from models import BinType

    out: dict[LevelKey, float] = {}

    def put(k, h):
        if h is not None and float(h) > out.get(k, 0.0):
            out[k] = float(h)

    for b, w, a, bay, lv, h in db.execute(
        select(StorageLocation.branch_id, StorageLocation.warehouse_id, StorageLocation.aisle, StorageLocation.bay,
               StorageLocation.level, BinType.height)
        .join(LocationBin, LocationBin.location_id == StorageLocation.id)
        .join(BinType, BinType.id == LocationBin.bin_type_id)
        .where(LocationBin.shelf_level_id.is_(None), LocationBin.quantity > 0,
               StorageLocation.bay != "", StorageLocation.level != "")
    ):
        put((b, w, a, bay, lv), h)
    for b, w, a, bay, lv, h in db.execute(
        select(ShelfLevel.branch_id, ShelfLevel.warehouse_id, ShelfLevel.aisle, ShelfLevel.bay, ShelfLevel.level,
               BinType.height)
        .join(LocationBin, LocationBin.shelf_level_id == ShelfLevel.id)
        .join(BinType, BinType.id == LocationBin.bin_type_id)
        .where(LocationBin.quantity > 0)
    ):
        put((b, w, a, bay, lv), h)
    return out


def fit_structure(db: Session, keys: Optional[Iterable[LevelKey]] = None) -> list[tuple[str, float, float]]:
    """A ESTRUTURA se adapta à bin: se uma bin é mais alta que o vão cadastrado do nível,
    o vão (distância até o nível de cima) sobe para a altura da bin. A bin nunca é alterada.
    Retorna [(nível, vão anterior, vão novo)]."""
    from decimal import Decimal

    only = set(keys) if keys is not None else None
    changes = []
    for k, h in tallest_bins(db).items():
        if only is not None and k not in only:
            continue
        sl = get_level(db, k)
        if sl is None or sl.height_cm is None or float(sl.height_cm) >= h - 1e-9:
            continue  # sem vão cadastrado: o nível já usa a altura das bins
        changes.append(("-".join(k[2:]), float(sl.height_cm), h))
        sl.height_cm = Decimal(str(h))
    if changes:
        db.flush()
    return changes


def structure_message(changes: list[tuple[str, float, float]]) -> str:
    if not changes:
        return ""
    parts = [f"{c} {old:g}→{new:g} cm".replace(".", ",") for c, old, new in changes[:8]]
    more = f" e mais {len(changes) - 8}" if len(changes) > 8 else ""
    return f"Estrutura ajustada à altura das bins — vão subiu em {len(changes)} nível(is): {', '.join(parts)}{more}."


def capacity_warnings(db: Session, keys: Iterable[LevelKey], type_codes: dict[int, str]) -> list[str]:
    msgs = []
    for k, u in sorted(usage_for(db, keys).items()):
        code = "-".join(k[2:])
        if u.over:
            parts = " + ".join(f"{n}× {type_codes.get(t, t)}" for t, n in u.used.items() if n)
            caps = " ou ".join(f"{m}× {type_codes.get(t, t)}" for t, m in u.capacity.items())
            msgs.append(f"Nível {code} acima da capacidade: {parts} = {u.fraction * 100:.0f}% (cabem {caps}).")
        elif u.has_capacity and u.unknown_types:
            msgs.append(f"Nível {code} tem bins de tipo sem capacidade cadastrada "
                        f"({', '.join(type_codes.get(t, str(t)) for t in u.unknown_types)}).")
    return msgs


# --------------------------------------------------------------------------- #
# Capacidade em lote (condições no estilo das exceções: '01-04', 'A,C', vazio = todos)
# --------------------------------------------------------------------------- #
def matching_levels(db: Session, branch_id: int, warehouse_id: Optional[int],
                    aisle_expr: str, bay_expr: str, level_expr: str) -> list[LevelKey]:
    from services.batch_generator import ValueMatcher

    ma, mb, ml = (ValueMatcher(aisle_expr, "Corredor"), ValueMatcher(bay_expr, "Estante"),
                  ValueMatcher(level_expr, "Nível"))
    keys: set[LevelKey] = set()
    for a, bay, lv in db.execute(_scope(select(StorageLocation.aisle, StorageLocation.bay, StorageLocation.level)
                                        .distinct(), StorageLocation, branch_id, warehouse_id)
                                 .where(StorageLocation.bay != "", StorageLocation.level != "")):
        keys.add((branch_id, warehouse_id, a, bay, lv))
    for sl in db.scalars(_scope(select(ShelfLevel), ShelfLevel, branch_id, warehouse_id)):
        keys.add((branch_id, warehouse_id, sl.aisle, sl.bay, sl.level))
    return sorted(k for k in keys if ma.matches(k[2]) and mb.matches(k[3]) and ml.matches(k[4]))


def set_height(db: Session, keys: list[LevelKey], height_cm) -> int:
    """Define (ou limpa, com None) a distância entre níveis (cm) dos níveis."""
    for k in keys:
        ensure_level(db, k).height_cm = height_cm
    db.flush()
    return len(keys)


def set_capacity(db: Session, keys: list[LevelKey], bin_type_id: int, max_bins: Optional[int]) -> int:
    """Define (ou remove, se max_bins for None/0) a capacidade de um tipo nos níveis."""
    n = 0
    for k in keys:
        lvl = ensure_level(db, k)
        cap = db.scalar(select(LevelCapacity).where(LevelCapacity.shelf_level_id == lvl.id,
                                                    LevelCapacity.bin_type_id == bin_type_id))
        if max_bins:
            if cap is None:
                db.add(LevelCapacity(shelf_level_id=lvl.id, bin_type_id=bin_type_id, max_bins=max_bins))
            else:
                cap.max_bins = max_bins
        elif cap is not None:
            db.delete(cap)
        n += 1
    db.flush()
    return n


def normalize_level_key(branch_id: int, warehouse_id: Optional[int], aisle: str, bay: str, level: str) -> LevelKey:
    return (branch_id, warehouse_id, normalize_code(aisle), normalize_code(bay), normalize_code(level))


# --------------------------------------------------------------------------- #
# Transbordo: bins que não cabem no nível do endereço ocupam os níveis seguintes da estante
# --------------------------------------------------------------------------- #
def _fraction(caps: dict[int, int], used: dict[int, int]) -> Optional[float]:
    if not caps or any(n and t not in caps for t, n in used.items()):
        return None
    return sum(n / caps[t] for t, n in used.items() if n)


DEDICATED_NOTE = "Nível dedicado a"


def remove_empty_dedicated(db: Session, bay: tuple) -> int:
    """Remove níveis dedicados criados automaticamente que ficaram sem bins e sem endereços."""
    b, w, a, bay_code = bay
    n = 0
    for sl in db.scalars(_scope(select(ShelfLevel), ShelfLevel, b, w).where(
            ShelfLevel.aisle == a, ShelfLevel.bay == bay_code, ShelfLevel.notes.like(DEDICATED_NOTE + "%"))):
        if db.scalar(select(LocationBin.id).where(LocationBin.shelf_level_id == sl.id).limit(1)):
            continue
        if db.scalar(_scope(select(StorageLocation.id), StorageLocation, b, w).where(
                StorageLocation.aisle == a, StorageLocation.bay == bay_code, StorageLocation.level == sl.level).limit(1)):
            continue
        db.delete(sl)
        n += 1
    if n:
        db.flush()
    return n


def next_level_code(code: str, existing: set[str]) -> str:
    """Código do nível logo acima: '03' -> '04' (mesmo preenchimento); não numérico -> '03+'."""
    if code.isdigit():
        n = int(code) + 1
        while True:
            c = str(n).zfill(len(code))
            if c not in existing:
                return c
            n += 1
    c = code + "+"
    while c in existing:
        c += "+"
    return c


def spill_overflow(db: Session, bays: Optional[Iterable[tuple]] = None) -> list[tuple[str, int, int, str, str]]:
    """Regra: um endereço cujas bins não cabem no nível dele ocupa NÍVEIS DEDICADOS logo acima,
    na mesma estante — cada nível recebe o que cabe em um nível (a capacidade cadastrada).
    Ex.: cabem 3× Y e o endereço tem 6× Y -> 3× Y no nível do endereço + 3× Y no nível de cima
    (2 níveis dedicados). Não procura "espaço livre" em outros níveis: é sempre o(s) de cima.
    Se não existir nível acima, ele é criado (a estrutura ganha um nível).
    Linhas que o usuário colocou em outro nível (cadastro do endereço, coluna "Nível") são mantidas — é assim
    que se escolhe outro nível —, exceto se deixarem aquele nível acima da capacidade: o nível pertence aos
    seus endereços, e as bins de fora voltam para o dono e são realocadas pela regra.
    `bays` = [(filial, armazém, corredor, estante)] ou None = todas.
    Retorna [(endereço, qtd, tipo, nível origem, nível destino)]."""
    import math

    from services.layout_service import natural_key

    if bays is None:
        bays = set(db.execute(select(ShelfLevel.branch_id, ShelfLevel.warehouse_id, ShelfLevel.aisle, ShelfLevel.bay)
                              .join(LevelCapacity, LevelCapacity.shelf_level_id == ShelfLevel.id).distinct()).all())
    moves: list[tuple[str, int, int, str, str]] = []
    for b, w, a, bay in sorted(set(tuple(x) for x in bays), key=lambda k: (k[0], k[1] or 0, k[2], k[3])):
        remove_empty_dedicated(db, (b, w, a, bay))
        keys = [lv["key"] for lv in bay_levels(db, b, w, a, bay)]
        keys.sort(key=lambda k: natural_key(k[4]))
        # Nível acima da capacidade por causa de bins de OUTROS endereços (vindas de outros níveis):
        # o nível é dos seus endereços — essas bins voltam para o endereço dono e são realocadas pela regra
        # (nível de cima se suportar; senão nível dedicado).
        usage0 = usage_for(db, keys)
        for k, u in usage0.items():
            if not u.over or not u.borrowed or u.shelf_level_id is None:
                continue
            for lb in list(db.scalars(select(LocationBin).join(StorageLocation, StorageLocation.id == LocationBin.location_id)
                                      .where(LocationBin.shelf_level_id == u.shelf_level_id,
                                             StorageLocation.level != k[4]))):
                own = db.scalar(select(LocationBin).where(LocationBin.location_id == lb.location_id,
                                                          LocationBin.bin_type_id == lb.bin_type_id,
                                                          LocationBin.shelf_level_id.is_(None)))
                if own is None:
                    lb.shelf_level_id = None
                else:
                    own.quantity += lb.quantity
                    db.delete(lb)
            db.flush()
        remove_empty_dedicated(db, (b, w, a, bay))
        keys = [lv["key"] for lv in bay_levels(db, b, w, a, bay)]
        keys.sort(key=lambda k: natural_key(k[4]))
        caps = {k: dict(u.capacity) for k, u in usage_for(db, keys).items()}
        rows_by_loc: dict[int, list[LocationBin]] = {}
        info: dict[int, tuple[str, str]] = {}
        for lb, loc_level, loc_addr in db.execute(
            _scope(select(LocationBin, StorageLocation.level, StorageLocation.address)
                   .join(StorageLocation, StorageLocation.id == LocationBin.location_id), StorageLocation, b, w)
            .where(StorageLocation.aisle == a, StorageLocation.bay == bay, StorageLocation.level != "",
                   LocationBin.shelf_level_id.is_(None), LocationBin.quantity > 0)
            .order_by(StorageLocation.address)
        ):
            rows_by_loc.setdefault(lb.location_id, []).append(lb)
            info[lb.location_id] = (loc_level, loc_addr)
        for lid, rows in rows_by_loc.items():
            level, address = info[lid]
            src = (b, w, a, bay, level)
            cap = caps.get(src) or {}
            if not cap or any(r.bin_type_id not in cap for r in rows):
                continue  # sem capacidade cadastrada para os tipos: não há como saber
            if sum(r.quantity / cap[r.bin_type_id] for r in rows) <= 1 + EPS:
                continue
            # preenche o nível do endereço até 100%; o resto vai para os níveis de cima, um nível cheio por vez
            free = 1.0
            keep: dict[int, int] = {}
            rest: list[list] = []
            for r in sorted(rows, key=lambda r: r.bin_type_id):
                k = min(r.quantity, math.floor(free * cap[r.bin_type_id] + EPS))
                keep[r.bin_type_id] = k
                free -= k / cap[r.bin_type_id]
                if r.quantity - k > 0:
                    rest.append([r.bin_type_id, r.quantity - k])
            last_idx = keys.index(src) if src in keys else len(keys) - 1
            own_sl = get_level(db, src)
            while rest:
                db.flush()
                # o que cabe em UM nível (capacidade do nível do endereço)
                room, chunk = 1.0, []
                for t, n in rest:
                    m = min(n, math.floor(room * cap[t] + EPS))
                    if m > 0:
                        chunk.append((t, m))
                        room -= m / cap[t]
                if not chunk:
                    break
                dst = None
                # 1) o nível logo acima, se ele suporta TODO esse excedente (com o que já tem)
                if last_idx + 1 < len(keys):
                    cand = keys[last_idx + 1]
                    u = usage_for(db, [cand])[cand]
                    dcap = u.capacity or {}
                    f = u.fraction if u.capacity else (0.0 if not any(u.used.values()) else None)
                    if f is not None and all(dcap.get(t) for t, _ in chunk):
                        if sum(m / dcap[t] for t, m in chunk) <= (1.0 - f) + EPS:
                            dst = cand
                # 2) senão, cria um nível DEDICADO logo acima (ex.: 01D1 entre 01 e 02)
                if dst is None:
                    base_code = keys[last_idx][4].split("D")[0] if re.fullmatch(r"\d+D\d+", keys[last_idx][4]) else keys[last_idx][4]
                    n = 1
                    taken = {k[4] for k in keys}
                    while f"{base_code}D{n}" in taken:
                        n += 1
                    dst = (b, w, a, bay, f"{base_code}D{n}")
                    sl_new = ensure_level(db, dst)
                    sl_new.notes = f"{DEDICATED_NOTE} {address}"
                    if own_sl is not None and own_sl.height_cm is not None:
                        sl_new.height_cm = own_sl.height_cm
                    for t, m in cap.items():
                        set_capacity(db, [dst], t, m)
                    keys.insert(last_idx + 1, dst)
                    caps[dst] = dict(cap)
                sl = ensure_level(db, dst)
                for t, m in chunk:
                    tgt = db.scalar(select(LocationBin).where(LocationBin.location_id == lid, LocationBin.bin_type_id == t,
                                                              LocationBin.shelf_level_id == sl.id))
                    if tgt is None:
                        db.add(LocationBin(location_id=lid, bin_type_id=t, quantity=m, shelf_level_id=sl.id))
                    else:
                        tgt.quantity += m
                    moves.append((address, m, t, "-".join(src[2:]), "-".join(dst[2:])))
                placed = dict(chunk)
                rest = [[t, n - placed.get(t, 0)] for t, n in rest if n - placed.get(t, 0) > 0]
                last_idx = keys.index(dst)
            for r in rows:
                if keep.get(r.bin_type_id, 0) <= 0:
                    db.delete(r)
                else:
                    r.quantity = keep[r.bin_type_id]
            db.flush()
    return moves


def spill_message(db: Session, moves) -> str:
    if not moves:
        return ""
    from models import BinType

    codes = dict(db.execute(select(BinType.id, BinType.code)).all())
    parts = [f"{ad}: {n}× {codes.get(t, t)} {s}→{d}" for ad, n, t, s, d in moves[:8]]
    more = f" e mais {len(moves) - 8}" if len(moves) > 8 else ""
    return ("Bins acima da capacidade do nível foram para o nível de cima (quando ele suporta todo o excedente) "
            "ou para um nível dedicado criado ao endereço — "
            + "; ".join(parts) + more + ". Para usar outro nível, escolha-o na linha da bin no cadastro do endereço.")


def bays_of_locations(db: Session, location_ids: Iterable[int]) -> set[tuple]:
    ids = [i for i in location_ids if i]
    out: set[tuple] = set()
    for i in range(0, len(ids), 900):
        out |= set(db.execute(select(StorageLocation.branch_id, StorageLocation.warehouse_id, StorageLocation.aisle,
                                     StorageLocation.bay).where(StorageLocation.id.in_(ids[i:i + 900]),
                                                                StorageLocation.bay != "", StorageLocation.level != "")
                              .distinct()).all())
    return out


def drop_unused_levels(db: Session, keys: Iterable[LevelKey]) -> tuple[list[str], list[str]]:
    """Exclui os níveis físicos (vão, capacidades) que ficaram sem endereço. Chame DEPOIS de excluir os endereços.
    Mantém o nível se ainda houver endereço nele ou bins de outros endereços guardadas nele (nível extra).
    Retorna (códigos excluídos, códigos mantidos com o motivo)."""
    dropped, kept = [], []
    for key in {k for k in keys if k}:
        sl = get_level(db, key)
        if sl is None:
            continue
        b, w, a, bay, lv = key
        if db.scalar(_scope(select(StorageLocation.id), StorageLocation, b, w).where(
                StorageLocation.aisle == a, StorageLocation.bay == bay, StorageLocation.level == lv).limit(1)):
            kept.append(f"{sl.code} (ainda tem endereços)")
            continue
        if db.scalar(select(LocationBin.id).where(LocationBin.shelf_level_id == sl.id).limit(1)):
            kept.append(f"{sl.code} (guarda bins de outros endereços)")
            continue
        dropped.append(sl.code)
        db.delete(sl)
    db.flush()
    return sorted(dropped), sorted(kept)
