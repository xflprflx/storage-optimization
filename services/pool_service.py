"""Ponte entre o banco e a otimização cega (pool de bins): carrega o escopo, executa e aplica o plano."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import Product, StockItem, StockPolicy, StorageLocation
from services.bin_optimizer import ProductUnit
from services.optimization_service import MAX_SCOPE, load_types
from services.pool_optimizer import PoolBin, PoolParams, PoolResult, SkuDemand, optimize_pool
from services.stock_service import refresh_occupancy, sync_default_locations
from services.i18n import _


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def _chunks(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def load_pool(db: Session, location_ids: list[int], basis: str = "stock", focus: Optional[set] = None,
              bin_source: str = "free"):
    """Retorna {(filial, armazém): (bins, demandas, endereços reservados, SKUs sem dimensão)} e notas.
    `focus` = ids dos produtos a otimizar (filtro de SKU/material): os demais produtos do escopo ficam onde estão e as
    bins que eles ocupam ficam travadas (fora do pool); as bins livres do escopo inteiro ficam disponíveis."""
    locs: dict[int, tuple] = {}
    for ch in _chunks(location_ids):
        for lid, addr, b, w in db.execute(select(StorageLocation.id, StorageLocation.address, StorageLocation.branch_id,
                                                 StorageLocation.warehouse_id).where(StorageLocation.id.in_(ch))):
            locs[lid] = (addr, b, w)
    groups: dict[tuple, dict] = {}

    def g(lid):
        _x, b, w = locs[lid]
        return groups.setdefault((b, w), {"bins": [], "demands": {}, "reserved": set(), "no_dims": [], "fixed": {}})

    # bins físicas (mesma numeração do conteúdo por bin) + o que cada uma guarda hoje
    from models import BinContent, BinType
    from services.bin_content_service import physical_bins

    codes = dict(db.execute(select(BinType.id, BinType.code)).all())
    by_key: dict[tuple, PoolBin] = {}
    nid = 0
    for lid, plist in physical_bins(db, locs).items():
        for pb in plist:
            nid += 1
            b = PoolBin(nid, lid, locs[lid][0], pb.type_id, pb.seq, pb.level, type_code=codes.get(pb.type_id, ""))
            g(lid)["bins"].append(b)
            by_key[(lid, pb.type_id, pb.seq)] = b
    for ch in _chunks(locs):
        for bc_loc, bc_t, bc_seq, sku, q in db.execute(
            select(BinContent.location_id, BinContent.bin_type_id, BinContent.seq, Product.sku, BinContent.quantity)
            .join(Product, Product.id == BinContent.product_id).where(BinContent.location_id.in_(ch))
        ):
            b = by_key.get((bc_loc, bc_t, bc_seq))
            if b is not None and q > 0:
                b.orig[sku] = b.orig.get(sku, 0) + float(q)

    # SKUs: saldo nos endereços do escopo + produtos cujo endereço padrão está no escopo
    prods: dict[int, Product] = {}
    stock: dict[tuple, dict[int, float]] = {}  # (grupo, produto) -> {endereço: saldo}
    for ch in _chunks(locs):
        for si in db.scalars(select(StockItem).where(StockItem.location_id.in_(ch), StockItem.quantity > 0)):
            gk = (locs[si.location_id][1], locs[si.location_id][2])
            stock.setdefault((gk, si.product_id), {})[si.location_id] = float(si.quantity)
            prods[si.product_id] = None
        # produtos cujo endereço padrão (política de estoque da filial) está no escopo
        for pol in db.scalars(select(StockPolicy).where(StockPolicy.default_location_id.in_(ch))):
            prods.setdefault(pol.product_id, None)
            gk = (locs[pol.default_location_id][1], locs[pol.default_location_id][2])
            stock.setdefault((gk, pol.product_id), {})
    missing = [pid for pid, p in prods.items() if p is None]
    for ch in _chunks(missing):
        for p in db.scalars(select(Product).where(Product.id.in_(ch))):
            prods[p.id] = p
    # política de estoque (endereço padrão, min/max) de cada produto nas filiais do escopo
    pols: dict[tuple, StockPolicy] = {}
    branch_ids = {b for _a, b, _w in locs.values()}
    for ch in _chunks(list(prods)):
        for pol in db.scalars(select(StockPolicy).where(StockPolicy.product_id.in_(ch),
                                                        StockPolicy.branch_id.in_(branch_ids))):
            pols[(pol.product_id, pol.branch_id)] = pol
    notes: list[str] = []
    no_level: list[str] = []

    def dflt_of(p, gk):
        pol = pols.get((p.id, gk[0]))
        d = pol.default_location_id if pol is not None else None
        return d if d in locs and (locs[d][1], locs[d][2]) == gk else None
    for (gk, pid), cur in stock.items():
        p = prods[pid]
        grp = groups.setdefault(gk, {"bins": [], "demands": {}, "reserved": set(), "no_dims": [], "fixed": {}})
        if focus is not None and pid not in focus:
            grp["fixed"][p.sku] = pid  # fora do foco: fica como está, nas bins de hoje
            u_ = ProductUnit(p.sku, 0, _f(p.height), _f(p.width), _f(p.length), _f(p.weight))
            if bin_source == "any" and u_.has_dims and sum(cur.values()) > 0:
                # pode ser desalojado se o foco usar a bin dele (realocado ou órfão, conforme a opção)
                grp.setdefault("secondary", []).append(SkuDemand(p.sku, pid, u_, sum(cur.values()), dict(cur), dflt_of(p, gk)))
            else:
                grp.setdefault("pinned", set()).add(p.sku)
            continue
        unit = ProductUnit(p.sku, 0, _f(p.height), _f(p.width), _f(p.length), _f(p.weight))
        dflt = dflt_of(p, gk)
        if not unit.has_dims:
            grp["no_dims"].append(p.sku)
            grp["reserved"] |= set(cur) | ({dflt} if dflt else set())
            continue
        qty = sum(cur.values())
        if basis in ("min", "max") and dflt:
            pol = pols.get((pid, gk[0]))
            lvl = (pol.min_level if basis == "min" else pol.max_level) if pol is not None else None
            if lvl is not None and float(lvl) > 0:  # 0 = não cadastrado
                # nunca menos espaço que o saldo físico do endereço padrão
                qty = max(float(lvl), float(cur.get(dflt, 0))) + sum(q for l, q in cur.items() if l != dflt)
            else:
                no_level.append(p.sku)
        if qty <= 0:
            continue
        grp["demands"][pid] = SkuDemand(p.sku, pid, unit, qty, dict(cur), dflt)
    if focus is not None:
        for grp in groups.values():
            # "só bins livres": toda bin com produto fora do foco fica travada; "qualquer bin": só as de produtos que
            # não podem ser desalojados (sem dimensões / sem saldo)
            fixed = set(grp.get("fixed", {})) if bin_source != "any" else set(grp.get("pinned", set()))
            for b in grp["bins"]:
                if fixed & set(b.orig):
                    b.locked = True  # guarda produto fora do foco: não entra no pool
    if no_level:
        label = _("nível mínimo") if basis == "min" else _("nível máximo")
        notes.append(_("{len} SKU(s) sem {label} cadastrado usaram o saldo atual: ", len=len(no_level), label=label)
                     + ", ".join(sorted(no_level)[:10]) + ("…" if len(no_level) > 10 else "") + ".")
    return groups, locs, notes


def run_pool(db: Session, location_ids: list[int], prm: PoolParams, basis: str = "stock", focus: Optional[set] = None):
    from services.or_engine import cached

    key = (basis, tuple(sorted(focus)) if focus is not None else None)
    return cached(db, "pool", location_ids, prm, key, lambda: _run_pool(db, location_ids, prm, basis, focus))


def _run_pool(db: Session, location_ids: list[int], prm: PoolParams, basis: str = "stock", focus: Optional[set] = None):
    if len(location_ids) > MAX_SCOPE:
        raise ValueError(_("O escopo tem {len} endereços (limite {MAX_SCOPE}). Filtre por armazém ou corredor.", len=f"{len(location_ids):,}", MAX_SCOPE=f"{MAX_SCOPE:,}"))
    types = load_types(db)
    groups, locs, notes = load_pool(db, location_ids, basis, focus, prm.bin_source)
    results: list[PoolResult] = []
    for gk, grp in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        r = optimize_pool(grp["bins"], list(grp["demands"].values()), types, prm, grp["reserved"], grp["no_dims"],
                          grp.get("secondary") if (focus is not None and prm.bin_source == "any") else None)
        r.fixed_pids = dict(grp.get("fixed", {}))
        if focus is not None:
            avail = [b for b in grp["bins"] if b.location_id not in grp["reserved"]]
            r.focus = {"skus": len(grp["demands"]), "locked_bins": sum(1 for b in avail if b.locked),
                       "free_bins": sum(1 for b in avail if not b.locked and not b.orig),
                       "fixed_skus": len(grp.get("fixed", {}))}
        results.append(r)
    return results, locs, types, notes


def pool_closure(results, skus) -> set:
    """SKUs que precisam ir junto ao aplicar só alguns: se um SKU escolhido vai para uma bin de onde outro SKU
    precisa sair, esse outro entra também (e assim por diante)."""
    plans = {p.sku: p for r in results for p in r.plans}
    need = {s for s in skus if s in plans}
    changed = True
    while changed:
        changed = False
        for s in list(need):
            for b, _n in plans[s].allocations:
                for o, q in b.orig.items():
                    if o == s or o in need or o not in plans:
                        continue
                    if plans[o].physical_after.get(b.id, 0) < q - 1e-9:
                        need.add(o)
                        changed = True
    return need


def apply_pool(db: Session, location_ids: list[int], prm: PoolParams, basis: str = "stock",
               only_skus: Optional[set] = None, focus: Optional[set] = None) -> tuple[int, int, set]:
    """Recalcula o plano e executa: move os saldos entre endereços e grava o conteúdo de cada bin física.
    As bins não mudam de lugar. `only_skus` = aplicar só esses SKUs (mais os que dependem deles).
    Retorna (SKUs alterados, endereços tocados, SKUs aplicados)."""
    from services.bin_content_service import refresh_contents, set_contents

    results, locs, _x, _x = run_pool(db, location_ids, prm, basis, focus)
    if prm.unlimited:
        raise ValueError(_("Com bins como recurso infinito o plano é só um dimensionamento e não pode ser aplicado."))
    all_skus = {p.sku for r in results for p in r.plans}
    applied = all_skus if only_skus is None else pool_closure(results, only_skus)
    if not applied:
        raise ValueError(_("Nenhum SKU selecionado."))
    short = sorted(p.sku for r in results for p in r.plans if p.sku in applied and p.shortage)
    if short:
        raise ValueError(_("O plano está incompleto (faltam bins para ") + ", ".join(short[:8])
                         + ("…" if len(short) > 8 else "") + _(") — nada foi alterado. "
                         "Amplie o escopo, mude o objetivo ou cadastre mais bins."))
    pid_of = {p.sku: p.product_id for r in results for p in r.plans}
    orphan_keys = {(sku, b.id) for r in results for sku, _l, b, _q in (r.orphans or []) if b is not None}
    for r in results:  # produtos fora do foco: continuam nas bins de hoje
        for sku, pid in (r.fixed_pids or {}).items():
            pid_of.setdefault(sku, pid)
    changed, touched_locs, touched_prods = 0, set(), set()
    applied_entries: dict[int, list] = {}  # endereço -> [(tipo, nº, produto, qtd)] dos SKUs aplicados
    affected: set[int] = set()
    pool_bins: dict[int, list] = {}
    for r in results:
        for b in r.bins:
            if b.location_id not in r.reserved_locations:
                pool_bins.setdefault(b.location_id, []).append(b)
        for plan in r.plans:
            if plan.sku not in applied:
                continue
            planned = plan.planned
            if not planned:
                continue
            cur = {l: q for l, q in plan.current.items()}
            total = sum(cur.values())
            if total <= 0:
                # sem saldo (min/max level): o produto passa a ficar no endereço planejado
                best = max(planned, key=lambda l: planned[l])
                from services.policy_service import get_policy

                pol = get_policy(db, db.get(StorageLocation, best).branch_id, plan.product_id, create=True)
                if pol.default_location_id != best:
                    touched_locs |= {pol.default_location_id, best}
                    pol.default_location_id = best
                    changed += 1
                continue
            affected |= set(cur) | set(planned)
            # saldo físico por bin depois das movimentações (o resto do planejado é espaço reservado)
            phys = plan.physical_after
            new: dict[int, float] = {}
            for b in {b.id: b for b, _n in plan.allocations}.values():  # cada bin uma vez
                q = phys.get(b.id, 0)
                if q > 1e-9:
                    new[b.location_id] = new.get(b.location_id, 0) + q
                    applied_entries.setdefault(b.location_id, []).append(
                        (b.type_id, b.seq, plan.product_id, Decimal(str(round(q, 4)))))
            rest = total - sum(new.values())
            if rest > 1e-9:  # não deveria ocorrer (o plano cobre o saldo): fica no 1º endereço planejado
                l0 = plan.allocations[0][0].location_id
                new[l0] = new.get(l0, 0) + rest
            if plan.moves:
                changed += 1
                touched_prods.add(plan.product_id)
            if {l: round(q, 4) for l, q in new.items()} == {l: round(q, 4) for l, q in cur.items()}:
                continue
            for l in cur:
                if l not in new:
                    db.execute(delete(StockItem).where(StockItem.product_id == plan.product_id, StockItem.location_id == l))
            for l, q in new.items():
                si = db.scalar(select(StockItem).where(StockItem.product_id == plan.product_id, StockItem.location_id == l))
                if si is None:
                    db.add(StockItem(product_id=plan.product_id, location_id=l, quantity=Decimal(str(round(q, 4)))))
                else:
                    si.quantity = Decimal(str(round(q, 4)))
            touched_locs |= set(cur) | set(new)
    db.flush()
    refresh_occupancy(db, touched_locs)
    sync_default_locations(db, touched_prods)
    # conteúdo de cada bin: SKUs aplicados conforme o plano; os demais continuam como estavam
    # endereços a regravar: todos do pool; com seleção ou foco, só os que o plano toca
    targets = set(pool_bins) if (only_skus is None and focus is None) else (affected & set(pool_bins))
    for l in targets:
        entries = list(applied_entries.get(l, []))
        for b in pool_bins.get(l, []):
            for sku, q in b.orig.items():
                if sku not in applied and sku in pid_of and (sku, b.id) not in orphan_keys:
                    entries.append((b.type_id, b.seq, pid_of[sku], Decimal(str(round(q, 4)))))
        # órfãos (opção "deixar fora de bin"): o saldo continua no endereço, sem bin
        err = set_contents(db, l, entries, allow_loose=bool(orphan_keys))
        if err:
            refresh_contents(db, [l], force=True)
    db.commit()
    return changed, len({l for l in touched_locs | set(applied_entries) if l}), applied
