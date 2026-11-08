"""
Serviços de consistência de estoque:
- recálculo de ocupação dos endereços (is_occupied): ocupado = tem algum item atrelado, mesmo sem saldo
- cache de cadastros mestres (Filial/Armazém) com criação automática por código
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import exists, func, insert, or_, select, update
from sqlalchemy.orm import Session

from models import Branch, StockItem, StorageLocation, Warehouse, branch_warehouse
from services.addressing import normalize_code

CHUNK = 900  # abaixo do limite de variáveis do SQLite


def _chunks(values: Iterable[int], size: int = CHUNK):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


def has_items_expr(location_id, correlate=None):
    """REGRA ÚNICA DE OCUPAÇÃO: o endereço está ocupado se tiver QUALQUER item atrelado,
    mesmo sem saldo:
      - registro de saldo (StockItem), inclusive com quantidade zero;
      - produto com este endereço como endereço padrão (na política de estoque da filial).
    `location_id` pode ser uma coluna (ex.: StorageLocation.id) ou um id fixo.
    """
    from models import StockPolicy

    parts = [
        exists().where(StockItem.location_id == location_id),
        exists().where(StockPolicy.default_location_id == location_id),
    ]
    if correlate is not None:
        parts = [p.correlate(correlate) for p in parts]
    return or_(*parts)


def location_has_items(db: Session, location_id: int) -> bool:
    return bool(db.scalar(select(has_items_expr(location_id))))


def refresh_occupancy(db: Session, location_ids: Optional[Iterable[int]] = None) -> None:
    """is_occupied = endereço tem algum item atrelado (ver has_items_expr). None => recalcula todos."""
    occupied_expr = has_items_expr(StorageLocation.id, correlate=StorageLocation)
    if location_ids is None:
        db.execute(update(StorageLocation).values(is_occupied=occupied_expr))
        return
    ids = {i for i in location_ids if i}
    for chunk in _chunks(ids):
        db.execute(
            update(StorageLocation).where(StorageLocation.id.in_(chunk)).values(is_occupied=occupied_expr)
        )


class MasterDataCache:
    """Cache de Filiais/Armazéns/associações por código, criando registros ausentes.

    Usado pelas importações para evitar uma consulta por linha.
    """

    def __init__(self, db: Session):
        self.db = db
        self.branches: dict[str, int] = dict(db.execute(select(Branch.code, Branch.id)).all())
        self.warehouses: dict[str, int] = dict(db.execute(select(Warehouse.code, Warehouse.id)).all())
        self.links: set[tuple[int, int]] = set(
            db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)).all()
        )
        self.created_branches = 0
        self.created_warehouses = 0
        self.created_links = 0
        self.notes: list[str] = []  # códigos associados por equivalência (ex.: '1' -> '01')

    # Snapshot/restore para rollback de um lote com falha
    def snapshot(self):
        return (
            dict(self.branches),
            dict(self.warehouses),
            set(self.links),
            self.created_branches,
            self.created_warehouses,
            self.created_links,
            list(self.notes),
        )

    def restore(self, snap) -> None:
        (
            self.branches,
            self.warehouses,
            self.links,
            self.created_branches,
            self.created_warehouses,
            self.created_links,
            self.notes,
        ) = snap

    def _alias(self, code: str, existing: dict[str, int], label: str) -> bool:
        """Código numérico sem zeros à esquerda (comum no Excel: '01' vira '1'):
        associa ao código existente equivalente, se houver exatamente um."""
        if not code.isdigit():
            return False
        target = code.lstrip("0") or "0"
        matches = [c for c in existing if c.isdigit() and (c.lstrip("0") or "0") == target]
        if len(matches) == 1:
            existing[code] = existing[matches[0]]
            self.notes.append(f"{label} '{code}' associado(a) ao código existente '{matches[0]}'.")
            return True
        return False

    def ensure_branches(self, codes: Iterable[str]) -> None:
        missing = sorted({normalize_code(c) for c in codes if c} - self.branches.keys())
        missing = [c for c in missing if not self._alias(c, self.branches, "Filial")]
        for code in missing:
            if len(code) > 30:
                raise ValueError(f"Código de filial muito longo: {code}")
            b = Branch(code=code, name=code)
            self.db.add(b)
            self.db.flush()
            self.branches[code] = b.id
            self.created_branches += 1

    def ensure_warehouses(self, codes: Iterable[str]) -> None:
        missing = sorted({normalize_code(c) for c in codes if c} - self.warehouses.keys())
        missing = [c for c in missing if not self._alias(c, self.warehouses, "Armazém")]
        for code in missing:
            if len(code) > 30:
                raise ValueError(f"Código de armazém muito longo: {code}")
            w = Warehouse(code=code, name=code)
            self.db.add(w)
            self.db.flush()
            self.warehouses[code] = w.id
            self.created_warehouses += 1

    def ensure_links(self, pairs: Iterable[tuple[int, int]]) -> None:
        missing = [p for p in set(pairs) if p not in self.links]
        if missing:
            self.db.execute(
                insert(branch_warehouse), [{"branch_id": b, "warehouse_id": w} for b, w in missing]
            )
            self.links.update(missing)
            self.created_links += len(missing)


def resolve_skus(db: Session, skus: Iterable[str]) -> tuple[dict[str, int], list[str]]:
    """SKU -> product_id. SKUs numéricos sem correspondência exata são associados ao SKU
    existente equivalente sem zeros à esquerda (Excel: '00123' vira '123'), se for único."""
    from models import Product

    skus = set(skus)
    found: dict[str, int] = {}
    for chunk in _chunks(sorted(skus)):
        found.update(dict(db.execute(select(Product.sku, Product.id).where(Product.sku.in_(chunk))).all()))
    notes: list[str] = []
    numeric = [s for s in skus - found.keys() if s.isdigit()]
    if numeric:
        stripped = {s: (s.lstrip("0") or "0") for s in numeric}
        for chunk in _chunks(sorted(set(stripped.values()))):
            rows = db.execute(select(Product.sku, Product.id).where(func.ltrim(Product.sku, "0").in_(chunk))).all()
            by_key: dict[str, list] = {}
            for sku, pid in rows:
                if sku.isdigit():
                    by_key.setdefault(sku.lstrip("0") or "0", []).append((sku, pid))
            for s, key in stripped.items():
                cands = by_key.get(key, [])
                if len(cands) == 1:
                    found[s] = cands[0][1]
                    notes.append(f"SKU '{s}' associado ao SKU existente '{cands[0][0]}'.")
    return found, notes


def sync_default_locations(db: Session, product_ids: Optional[Iterable[int]] = None) -> int:
    """ENDEREÇO PADRÃO = endereço onde o produto normalmente está, EM CADA FILIAL (política de estoque).

    Para cada produto e filial onde ele tem saldo lançado:
      - se o endereço padrão da filial está vazio, ou não é um dos endereços onde o produto está nela,
        passa a ser o endereço onde o produto tem a MAIOR quantidade (a política é criada se faltar);
      - se o padrão já é um dos endereços onde o produto está, é mantido (escolha do usuário).
    "Onde está" = registros com quantidade > 0; se todos estiverem zerados, qualquer registro de saldo.
    Produto sem saldo lançado na filial mantém o padrão informado (endereço previsto).
    Retorna quantas políticas tiveram o endereço padrão alterado (a ocupação é recalculada).
    """
    from models import StockPolicy

    ids = None if product_ids is None else {i for i in product_ids if i}
    if ids is not None and not ids:
        return 0
    stmt = (select(StockItem.product_id, StorageLocation.branch_id, StockItem.location_id, StockItem.quantity,
                   StorageLocation.address)
            .join(StorageLocation, StorageLocation.id == StockItem.location_id))
    rows_by_key: dict[tuple, list] = {}
    chunks = [None] if ids is None else list(_chunks(ids))
    for chunk in chunks:
        q = stmt if chunk is None else stmt.where(StockItem.product_id.in_(chunk))
        for pid, bid, lid, qty, addr in db.execute(q):
            rows_by_key.setdefault((pid, bid), []).append((lid, qty or 0, addr))
    if not rows_by_key:
        return 0
    pols: dict[tuple, StockPolicy] = {}
    pids = {k[0] for k in rows_by_key}
    for chunk in _chunks(pids):
        for pol in db.scalars(select(StockPolicy).where(StockPolicy.product_id.in_(chunk))):
            pols[(pol.product_id, pol.branch_id)] = pol
    changed, touched = 0, set()
    for (pid, bid), rows in rows_by_key.items():
        here = [r for r in rows if r[1] > 0] or rows
        pol = pols.get((pid, bid))
        if pol is not None and pol.default_location_id in {r[0] for r in here}:
            continue
        best = sorted(here, key=lambda r: (-r[1], r[2]))[0][0]
        if pol is None:
            pol = StockPolicy(branch_id=bid, product_id=pid)
            db.add(pol)
            pols[(pid, bid)] = pol
        touched.update((pol.default_location_id, best))
        pol.default_location_id = best
        changed += 1
    if changed:
        db.flush()
    if changed:
        refresh_occupancy(db, touched)
    return changed
