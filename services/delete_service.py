"""
Exclusão em massa dos cadastros, com ou sem os registros relacionados.

Para cada tipo: o impacto (quantos registros relacionados cada item tem), o motivo que impede a exclusão simples
(ex.: filial com endereços) e a exclusão. Com `cascade`, os relacionados saem junto, na ordem que o banco aceita:
  - filial / armazém: endereços (com saldos, bins e conteúdo), níveis e estruturas das estantes, planta, políticas,
    histórico de pedidos e resultados de otimização da filial;
  - tipo de bin: as bins desse tipo nos endereços, as frações filhas (e as bins delas) e as capacidades dos níveis;
  - estrutura: a atribuição às estantes;
  - produto: saldos (e as políticas, que já saem com o produto);
  - projeto de filial nova: os resultados de otimização guardados do projeto.
Sem `cascade`, os itens com relacionados que impedem a exclusão ficam de fora (e aparecem na prévia).
"""
from __future__ import annotations

from typing import Callable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from models import (BayStructure, BinContent, BinType, Branch, BranchLayout, LevelCapacity, LocationBin,
                    OptimizationRun, PickHistory, Product, ShelfLevel, SiteProject, StockItem, StockPolicy,
                    StorageLocation, StructureType, Warehouse)

CHUNK = 800


def _chunks(ids: list[int]):
    for i in range(0, len(ids), CHUNK):
        yield ids[i:i + CHUNK]


def _count_by(db: Session, col, ids: list[int], *where) -> dict[int, int]:
    out: dict[int, int] = {}
    for ch in _chunks(ids):
        for k, n in db.execute(select(col, func.count()).where(col.in_(ch), *where).group_by(col)):
            out[k] = out.get(k, 0) + n
    return out


def _loc_ids(db: Session, col, ids: list[int]) -> list[int]:
    out = []
    for ch in _chunks(ids):
        out += list(db.scalars(select(StorageLocation.id).where(col.in_(ch))))
    return out


def _delete_locations(db: Session, loc_ids: list[int]) -> list[int]:
    """Apaga endereços (saldos, bins e conteúdo saem pelo ON DELETE CASCADE). Retorna os produtos afetados."""
    prods = set()
    for ch in _chunks(loc_ids):
        prods |= set(db.scalars(select(StockItem.product_id).where(StockItem.location_id.in_(ch))))
        db.execute(delete(StorageLocation).where(StorageLocation.id.in_(ch)))
    return sorted(prods)


# --------------------------------------------------------------------------- #
# Impacto: {id: {"name", "rel": {rótulo: n}, "block": motivo|None}}
# --------------------------------------------------------------------------- #
def _impact_branches(db, ids):
    locs = _count_by(db, StorageLocation.branch_id, ids)
    stock = {}
    for ch in _chunks(ids):
        for b, n in db.execute(select(StorageLocation.branch_id, func.count()).join(StockItem, StockItem.location_id == StorageLocation.id)
                               .where(StorageLocation.branch_id.in_(ch)).group_by(StorageLocation.branch_id)):
            stock[b] = n
    lv = _count_by(db, ShelfLevel.branch_id, ids)
    bs = _count_by(db, BayStructure.branch_id, ids)
    pol = _count_by(db, StockPolicy.branch_id, ids)
    lay = _count_by(db, BranchLayout.branch_id, ids)
    hist = _count_by(db, PickHistory.branch_id, ids)
    runs = _count_by(db, OptimizationRun.branch_id, ids)
    out = {}
    for b in db.scalars(select(Branch).where(Branch.id.in_(ids))):
        out[b.id] = {"name": f"{b.code} - {b.name}",
                     "rel": {"endereços": locs.get(b.id, 0), "saldos": stock.get(b.id, 0), "níveis": lv.get(b.id, 0),
                             "estantes com estrutura": bs.get(b.id, 0), "políticas de estoque": pol.get(b.id, 0),
                             "plantas": lay.get(b.id, 0), "histórico de pedidos": hist.get(b.id, 0),
                             "resultados de otimização": runs.get(b.id, 0)},
                     "block": f"tem {locs[b.id]} endereço(s)" if locs.get(b.id) else None}
    return out


def _run_branches(db, ids, cascade):
    loc_ids = _loc_ids(db, StorageLocation.branch_id, ids)
    if loc_ids:
        _delete_locations(db, loc_ids)
    if cascade:
        db.execute(delete(OptimizationRun).where(OptimizationRun.branch_id.in_(ids)))
    # níveis, estruturas, planta, políticas, histórico e vínculos com armazéns: ON DELETE CASCADE
    db.execute(delete(Branch).where(Branch.id.in_(ids)))


def _impact_warehouses(db, ids):
    locs = _count_by(db, StorageLocation.warehouse_id, ids)
    stock = {}
    for ch in _chunks(ids):
        for w, n in db.execute(select(StorageLocation.warehouse_id, func.count()).join(StockItem, StockItem.location_id == StorageLocation.id)
                               .where(StorageLocation.warehouse_id.in_(ch)).group_by(StorageLocation.warehouse_id)):
            stock[w] = n
    lv = _count_by(db, ShelfLevel.warehouse_id, ids)
    bs = _count_by(db, BayStructure.warehouse_id, ids)
    lay = _count_by(db, BranchLayout.warehouse_id, ids)
    out = {}
    for w in db.scalars(select(Warehouse).where(Warehouse.id.in_(ids))):
        out[w.id] = {"name": f"{w.code} - {w.name}",
                     "rel": {"endereços": locs.get(w.id, 0), "saldos": stock.get(w.id, 0), "níveis": lv.get(w.id, 0),
                             "estantes com estrutura": bs.get(w.id, 0), "plantas": lay.get(w.id, 0)},
                     "block": f"tem {locs[w.id]} endereço(s)" if locs.get(w.id) else None}
    return out


def _run_warehouses(db, ids, cascade):
    from services.stock_service import sync_default_locations

    loc_ids = _loc_ids(db, StorageLocation.warehouse_id, ids)
    prods = _delete_locations(db, loc_ids) if loc_ids else []
    db.execute(delete(Warehouse).where(Warehouse.id.in_(ids)))
    db.flush()
    if prods:
        sync_default_locations(db, prods)


def _type_tree(db, ids) -> list[int]:
    """Os tipos e as frações filhas (de qualquer nível)."""
    allids, front = set(ids), set(ids)
    while front:
        kids = set(db.scalars(select(BinType.id).where(BinType.parent_id.in_(list(front))))) - allids
        allids |= kids
        front = kids
    return sorted(allids)


def _impact_bin_types(db, ids):
    used = {}
    for ch in _chunks(ids):
        for t, n in db.execute(select(LocationBin.bin_type_id, func.count(func.distinct(LocationBin.location_id)))
                               .where(LocationBin.bin_type_id.in_(ch)).group_by(LocationBin.bin_type_id)):
            used[t] = n
    kids = _count_by(db, BinType.parent_id, ids)
    caps = _count_by(db, LevelCapacity.bin_type_id, ids)
    out = {}
    for t in db.scalars(select(BinType).where(BinType.id.in_(ids))):
        why = [f"usado em {used[t.id]} endereço(s)" if used.get(t.id) else "",
               f"é bin pai de {kids[t.id]} fração(ões)" if kids.get(t.id) else ""]
        out[t.id] = {"name": f"{t.code} - {t.name}",
                     "rel": {"endereços com a bin": used.get(t.id, 0), "frações filhas": kids.get(t.id, 0),
                             "capacidades de nível": caps.get(t.id, 0)},
                     "block": "; ".join(w for w in why if w) or None}
    return out


def _run_bin_types(db, ids, cascade):
    from services.bin_content_service import refresh_contents

    ids = _type_tree(db, ids) if cascade else ids
    locs = set()
    for ch in _chunks(ids):
        locs |= set(db.scalars(select(LocationBin.location_id).where(LocationBin.bin_type_id.in_(ch))))
        db.execute(delete(LocationBin).where(LocationBin.bin_type_id.in_(ch)))
        db.execute(delete(BinContent).where(BinContent.bin_type_id.in_(ch)))
        db.execute(update(BinType).where(BinType.id.in_(ch)).values(parent_id=None))
    db.execute(delete(BinType).where(BinType.id.in_(ids)))  # capacidades dos níveis: ON DELETE CASCADE
    db.flush()
    if locs:
        refresh_contents(db, sorted(locs), force=True)


def _impact_structures(db, ids):
    used = _count_by(db, BayStructure.structure_type_id, ids)
    return {s.id: {"name": f"{s.code} - {s.name}", "rel": {"estantes com a estrutura": used.get(s.id, 0)},
                   "block": f"está em {used[s.id]} estante(s)" if used.get(s.id) else None}
            for s in db.scalars(select(StructureType).where(StructureType.id.in_(ids)))}


def _run_structures(db, ids, cascade):
    db.execute(delete(BayStructure).where(BayStructure.structure_type_id.in_(ids)))
    db.execute(delete(StructureType).where(StructureType.id.in_(ids)))


def _impact_products(db, ids):
    stock = _count_by(db, StockItem.product_id, ids, StockItem.quantity != 0)
    zero = _count_by(db, StockItem.product_id, ids, StockItem.quantity == 0)
    pol = _count_by(db, StockPolicy.product_id, ids)
    out = {}
    for ch in _chunks(ids):
        for p in db.scalars(select(Product).where(Product.id.in_(ch))):
            out[p.id] = {"name": f"{p.sku} - {p.name}",
                         "rel": {"saldos": stock.get(p.id, 0), "atribuições sem saldo": zero.get(p.id, 0),
                                 "políticas de estoque": pol.get(p.id, 0)},
                         "block": f"tem saldo em {stock[p.id]} endereço(s)" if stock.get(p.id) else None}
    return out


def _run_products(db, ids, cascade):
    from services.bin_content_service import refresh_contents
    from services.stock_service import refresh_occupancy

    locs = set()
    for ch in _chunks(ids):
        locs |= set(db.scalars(select(StockItem.location_id).where(StockItem.product_id.in_(ch))))
        db.execute(delete(StockItem).where(StockItem.product_id.in_(ch)))
        db.execute(delete(Product).where(Product.id.in_(ch)))  # políticas e conteúdo: ON DELETE CASCADE
    db.flush()
    if locs:
        refresh_occupancy(db, sorted(locs))
        refresh_contents(db, sorted(locs))


def _impact_stock(db, ids):
    out = {}
    for ch in _chunks(ids):
        for s, sku, addr, q in db.execute(select(StockItem.id, Product.sku, StorageLocation.address, StockItem.quantity)
                                          .join(Product, Product.id == StockItem.product_id)
                                          .join(StorageLocation, StorageLocation.id == StockItem.location_id)
                                          .where(StockItem.id.in_(ch))):
            out[s] = {"name": f"{sku} em {addr} ({q:g})", "rel": {}, "block": None}
    return out


def _run_stock(db, ids, cascade):
    from services.bin_content_service import refresh_contents
    from services.stock_service import refresh_occupancy, sync_default_locations

    locs, prods = set(), set()
    for ch in _chunks(ids):
        for lid, pid in db.execute(select(StockItem.location_id, StockItem.product_id).where(StockItem.id.in_(ch))):
            locs.add(lid)
            prods.add(pid)
        db.execute(delete(StockItem).where(StockItem.id.in_(ch)))
    db.flush()
    refresh_occupancy(db, sorted(locs))
    sync_default_locations(db, sorted(prods))
    refresh_contents(db, sorted(locs))


def _impact_projects(db, ids):
    runs = _count_by(db, OptimizationRun.project_id, ids)
    return {p.id: {"name": p.name, "rel": {"resultados de otimização": runs.get(p.id, 0)}, "block": None}
            for p in db.scalars(select(SiteProject).where(SiteProject.id.in_(ids)))}


def _run_projects(db, ids, cascade):
    if cascade:
        db.execute(delete(OptimizationRun).where(OptimizationRun.project_id.in_(ids)))
    db.execute(delete(SiteProject).where(SiteProject.id.in_(ids)))


KINDS: dict[str, dict] = {
    "branches": {"label": "filiais", "back": "/branches", "impact": _impact_branches, "run": _run_branches,
                 "cascade": "endereços (com saldos e bins), níveis, estruturas das estantes, planta, políticas, histórico "
                            "de pedidos e resultados de otimização da filial"},
    "warehouses": {"label": "armazéns", "back": "/warehouses", "impact": _impact_warehouses, "run": _run_warehouses,
                   "cascade": "endereços do armazém (com saldos e bins), níveis, estruturas das estantes e planta"},
    "bin_types": {"label": "tipos de bin", "back": "/bin-types", "impact": _impact_bin_types, "run": _run_bin_types,
                  "cascade": "as bins desse tipo nos endereços, as frações filhas e as capacidades dos níveis"},
    "structures": {"label": "estruturas", "back": "/structures", "impact": _impact_structures, "run": _run_structures,
                   "cascade": "a atribuição da estrutura às estantes"},
    "products": {"label": "produtos", "back": "/products", "impact": _impact_products, "run": _run_products,
                 "cascade": "os saldos dos produtos (e as políticas de estoque)"},
    "stock": {"label": "registros de saldo", "back": "/stock", "impact": _impact_stock, "run": _run_stock,
              "cascade": ""},
    "projects": {"label": "projetos de filial nova", "back": "/optimization/site", "impact": _impact_projects,
                 "run": _run_projects, "cascade": "os resultados de otimização guardados dos projetos"},
}


def impact(db: Session, kind: str, ids: list[int]) -> dict:
    """Prévia: itens, relacionados (por item e totais) e os que a exclusão simples deixaria de fora."""
    items: dict = {}
    fn: Callable = KINDS[kind]["impact"]
    for ch in _chunks(ids):
        items.update(fn(db, ch))
    totals: dict[str, int] = {}
    for it in items.values():
        for k, n in it["rel"].items():
            totals[k] = totals.get(k, 0) + n
    blocked = {i: it for i, it in items.items() if it["block"]}
    return {"items": items, "totals": {k: n for k, n in totals.items() if n}, "blocked": blocked}


def run(db: Session, kind: str, ids: list[int], cascade: bool) -> tuple[int, int]:
    """Exclui. Retorna (excluídos, ignorados por terem relacionados que impedem a exclusão simples)."""
    imp = impact(db, kind, ids)
    todo = [i for i in imp["items"] if cascade or i not in imp["blocked"]]
    if todo:
        KINDS[kind]["run"](db, todo, cascade)
    return len(todo), len(imp["items"]) - len(todo)


def filter_ids(db: Session, kind: str, params) -> Optional[list[int]]:
    """Ids de TODOS os itens do filtro da lista (produtos e saldos), com os mesmos parâmetros da tela."""
    if kind == "products":
        from routers.products import filtered_products

        stmt = filtered_products(params.get("q", ""), params.get("material", ""), params.get("field", ""),
                                 params.get("mode", "contains"))
        return list(db.scalars(stmt.with_only_columns(Product.id).order_by(None)))
    if kind == "stock":
        from routers.stock import filtered_stock

        stmt = filtered_stock(params)
        return list(db.scalars(stmt.with_only_columns(StockItem.id).order_by(None)))
    return None
