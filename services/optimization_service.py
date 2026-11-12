"""Ponte entre o banco e o otimizador de bins: carrega o escopo, executa e aplica sugestões."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BinType, LocationBin, Product, StockItem, StockPolicy, StorageLocation
from services.bin_optimizer import AddressIn, BinSpec, LevelLedger, LevelSlot, Params, ProductUnit, optimize
from services.bin_service import set_location_bins
from services.layout_service import natural_key
from services.level_service import bay_levels, ensure_level, fit_structure, usage_for
from services.i18n import _

MAX_SCOPE = 20_000


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def load_types(db: Session) -> dict[int, BinSpec]:
    from services.bin_type_service import footprint_width

    return {
        bt.id: BinSpec(bt.id, bt.code, _f(bt.height), _f(bt.width), _f(bt.length), _f(bt.volume), _f(bt.max_weight),
                       front_w=footprint_width(bt), cost=bin_cost(bt))
        for bt in db.scalars(select(BinType))
    }


def bin_cost(bt: BinType) -> Optional[float]:
    """Custo de uma bin do tipo; compartimento sem custo próprio = custo da bin pai ÷ compartimentos por pai."""
    if bt.cost is not None:
        return float(bt.cost)
    if bt.is_fraction and bt.parent is not None and bt.parent.cost is not None:
        return float(bt.parent.cost) / bt.per_parent
    return None


def derive_caps(caps: dict[int, int], types: dict[int, BinSpec]) -> tuple[dict[int, int], set]:
    """Tipos sem capacidade cadastrada no nível: estima pela largura útil do nível
    (a maior largura entre os tipos cadastrados: cabem 8× CX-P de 20 cm = 160 cm -> cabem 4× de 40 cm)."""
    widths = [n * types[t].fw for t, n in caps.items() if t in types and types[t].fw]
    if not widths:
        return dict(caps), set()
    usable = max(widths)
    out, derived = dict(caps), set()
    for t, spec in types.items():
        if t not in out and spec.fw:
            n = int(usable // spec.fw + 1e-9)
            if n > 0:
                out[t] = n
                derived.add(t)
    return out, derived


def load_addresses(db: Session, location_ids: list[int], basis: str = "stock",
                   types: Optional[dict[int, BinSpec]] = None):
    """Retorna (endereços, ledger de níveis). Chave de nível = (filial, armazém, corredor, estante, nível).

    `basis` define a quantidade de cada SKU no endereço:
      - "stock": saldo atual do endereço;
      - "min"/"max": nível mínimo/máximo do produto, no ENDEREÇO PADRÃO dele (o endereço onde ele fica),
        inclusive se ainda não houver saldo lançado. Nunca menos que o saldo físico do endereço. Sem o nível cadastrado (vazio ou 0), usa o saldo atual (com aviso).
        Em endereços que não são o padrão do produto, vale o saldo atual."""
    from models import ShelfLevel

    out: dict[int, AddressIn] = {}
    bays: set[tuple] = set()
    sl_key: dict[int, tuple] = {}
    for i in range(0, len(location_ids), 900):
        chunk = location_ids[i : i + 900]
        for r in db.execute(select(StorageLocation.id, StorageLocation.address, StorageLocation.branch_id,
                                   StorageLocation.warehouse_id, StorageLocation.aisle, StorageLocation.bay,
                                   StorageLocation.level).where(StorageLocation.id.in_(chunk))):
            a = AddressIn(r.id, r.address, {}, [])
            if r.bay and r.level:
                a.own_level = (r.branch_id, r.warehouse_id, r.aisle, r.bay, r.level)
                bays.add(a.own_level[:4])
            out[r.id] = a
        rows = db.execute(select(LocationBin.location_id, LocationBin.bin_type_id, LocationBin.quantity,
                                 LocationBin.shelf_level_id).where(LocationBin.location_id.in_(chunk))).all()
        need = {r[3] for r in rows if r[3] and r[3] not in sl_key}
        if need:
            for sl in db.scalars(select(ShelfLevel).where(ShelfLevel.id.in_(need))):
                sl_key[sl.id] = (sl.branch_id, sl.warehouse_id, sl.aisle, sl.bay, sl.level)
        for lid, tid, qty, sl_id in rows:
            a = out[lid]
            a.current[tid] = a.current.get(tid, 0) + qty
            where = sl_key.get(sl_id) if sl_id else a.own_level
            if where is not None:
                pl = a.current_placement.setdefault(where, {})
                pl[tid] = pl.get(tid, 0) + qty
        seen: set[tuple[int, int]] = set()
        missing: dict[int, list[str]] = {}
        # endereço padrão e min/max level: política de estoque do produto na filial do endereço
        level_col = StockPolicy.min_level if basis == "min" else StockPolicy.max_level
        for lid, pid, sku, qty, h, w, ln, wt, dflt, lvl in db.execute(
            select(StockItem.location_id, Product.id, Product.sku, StockItem.quantity, Product.height, Product.width,
                   Product.length, Product.weight, StockPolicy.default_location_id, level_col)
            .join(Product, Product.id == StockItem.product_id)
            .join(StorageLocation, StorageLocation.id == StockItem.location_id)
            .outerjoin(StockPolicy, (StockPolicy.product_id == Product.id)
                       & (StockPolicy.branch_id == StorageLocation.branch_id))
            .where(StockItem.location_id.in_(chunk))
        ):
            seen.add((lid, pid))
            q = float(qty)
            if basis != "stock" and dflt == lid:
                if lvl is not None and float(lvl) > 0:  # 0 = não cadastrado
                    q = max(float(lvl), q)  # nunca menos espaço que o saldo físico do endereço
                else:
                    missing.setdefault(lid, []).append(sku)
            out[lid].items.append(ProductUnit(sku, q, _f(h), _f(w), _f(ln), _f(wt)))
        for lid, pid, sku, h, w, ln, wt, lvl in db.execute(
            select(StockPolicy.default_location_id, Product.id, Product.sku, Product.height, Product.width,
                   Product.length, Product.weight, level_col)
            .join(Product, Product.id == StockPolicy.product_id).where(StockPolicy.default_location_id.in_(chunk))
        ):
            out[lid].linked_without_stock = True
            if basis != "stock" and (lid, pid) not in seen and lvl is not None and float(lvl) > 0:
                # produto que fica aqui mas ainda sem saldo: dimensiona pelo min/max level
                out[lid].items.append(ProductUnit(sku, float(lvl), _f(h), _f(w), _f(ln), _f(wt)))
        label = _("nível mínimo") if basis == "min" else _("nível máximo")
        for lid, skus in missing.items():
            out[lid].notes.append(_("Sem {label} cadastrado (usou o saldo atual): ", label=label) + ", ".join(skus[:5])
                                  + ("…" if len(skus) > 5 else "") + ".")

    # ---- níveis das estantes envolvidas: capacidade e uso atual (de TODOS os endereços) ----
    bay_keys: dict[tuple, list[tuple]] = {}
    all_keys: list[tuple] = []
    for b, w, a_, bay in bays:
        keys = [lv["key"] for lv in bay_levels(db, b, w, a_, bay)]
        keys.sort(key=lambda k: natural_key(k[4]))
        bay_keys[(b, w, a_, bay)] = keys
        all_keys += keys
    usage = usage_for(db, all_keys) if all_keys else {}
    slots = {}
    for k, u in usage.items():
        caps, derived = derive_caps(dict(u.capacity), types or {}) if u.capacity else ({}, set())
        slots[k] = LevelSlot(k, "-".join(k[2:]), caps or None, u.height, derived)
    ledger = LevelLedger(slots, {k: dict(u.used) for k, u in usage.items()})
    addresses = [out[i] for i in location_ids if i in out]
    for a in addresses:
        if a.own_level is not None:
            a.bay_levels = bay_keys.get(a.own_level[:4], [])
    return addresses, ledger


def run(db: Session, location_ids: list[int], prm: Params, donors: Optional[list[int]] = None):
    """`donors` (foco nos SKUs filtrados, bins existentes): outros endereços do escopo que podem ceder bins."""
    from services.or_engine import cached

    donors = donors if (donors and prm.limit_inventory) else None
    return cached(db, "address", location_ids, prm, tuple(donors or ()),
                  lambda: _run(db, location_ids, prm, donors))


def _donor_bins(db: Session, ids: list[int], codes: dict) -> dict[int, list]:
    """Bins físicas dos doadores com o conteúdo atual: {endereço: [(tipo, nº, {sku: qtd})]}."""
    from models import BinContent
    from services.bin_content_service import physical_bins, refresh_contents

    refresh_contents(db, ids)
    phys = physical_bins(db, ids)
    cont: dict[tuple, dict] = {}
    for i in range(0, len(ids), 900):
        for lid, t, seq, sku, q in db.execute(
                select(BinContent.location_id, BinContent.bin_type_id, BinContent.seq, Product.sku, BinContent.quantity)
                .join(Product, Product.id == BinContent.product_id).where(BinContent.location_id.in_(ids[i:i + 900]))):
            c = cont.setdefault((lid, t, seq), {})
            c[sku] = c.get(sku, 0.0) + float(q)
    return {lid: [(b.type_id, b.seq, cont.get((lid, b.type_id, b.seq), {})) for b in bl] for lid, bl in phys.items()}


def _run(db: Session, location_ids: list[int], prm: Params, donors: Optional[list[int]] = None):
    import copy
    import dataclasses

    from services.bin_optimizer import Donor, assign_donors, donors_inventory, summarize

    if len(location_ids) + len(donors or []) > MAX_SCOPE:
        raise ValueError(_("O escopo tem {len} endereços (limite {MAX_SCOPE}). Filtre por armazém ou corredor.", len=f"{len(location_ids) + len(donors or []):,}", MAX_SCOPE=f"{MAX_SCOPE:,}"))
    types = load_types(db)
    if prm.allowed_types is not None:
        prm.allowed_types = {t for t in prm.allowed_types if t in types}
    if not donors:
        addresses, ledger = load_addresses(db, location_ids, prm.basis, types)
        results, summary = optimize(addresses, types, prm, ledger)
        return results, summary, types
    # foco: os endereços filtrados podem usar as bins dos doadores (outros endereços do escopo)
    focus = set(location_ids)
    every, ledger0 = load_addresses(db, list(location_ids) + [i for i in donors if i not in focus], prm.basis, types)
    addresses = [a for a in every if a.id in focus]
    dn = [Donor(a) for a in every if a.id not in focus and a.current]
    pbins = _donor_bins(db, [d.a.id for d in dn if d.stocked], {})
    for d in dn:
        d.bins = pbins.get(d.a.id, [])
    extra = donors_inventory(dn, prm)
    for _x in range(4):
        ledger = copy.deepcopy(ledger0)
        p2 = dataclasses.replace(prm, extra_inventory=extra)
        results, summary = optimize(addresses, types, p2, ledger)
        dres, uncovered, orphans = assign_donors(results, dn, types, prm, ledger)
        if not uncovered:
            break
        # doadores com saldo que não conseguiram ceder: tira do estoque disponível e refaz
        extra = {t: max(0, n - uncovered.get(t, 0)) for t, n in extra.items()}
    all_res = results + dres
    s = summarize(all_res)
    s.solver = summary.solver
    s.orphans = orphans
    s.donors = {"free": sum(1 for d in dn if not d.stocked), "stocked": sum(1 for d in dn if d.stocked),
                "free_bins": sum(sum(d.a.current.values()) for d in dn if not d.stocked),
                "used": len(dres), "uncovered": uncovered, "focus": len(addresses)}
    return all_res, s, types


def inventory_closure(results, only: set) -> set:
    """Bins existentes: aplicando só alguns endereços, o total de cada tipo não pode passar do que existe no escopo.
    Endereços que ganham bins de um tipo puxam junto os que liberam bins desse tipo (doadores), até fechar a conta."""
    from services.bin_optimizer import _after

    sel = {r.id for r in results if r.id in only and r.applicable}
    budget: dict[int, int] = {}
    for r in results:
        for t, n in (r.current or {}).items():
            budget[t] = budget.get(t, 0) + n
    donors = sorted((r for r in results if r.applicable and r.id not in sel), key=lambda r: r.address)
    while True:
        tot: dict[int, int] = {}
        for r in results:
            for t, n in (_after(r) if r.id in sel else dict(r.current or {})).items():
                tot[t] = tot.get(t, 0) + n
        over = [t for t, n in tot.items() if n > budget.get(t, 0)]
        if not over:
            return sel
        d = next((r for r in donors if r.id not in sel
                  and any(_after(r).get(t, 0) < (r.current or {}).get(t, 0) for t in over)), None)
        if d is None:
            return sel
        sel.add(d.id)


def _kept_contents(db: Session, r) -> list:
    """Conteúdo das bins que o doador mantém, renumerado como fica depois (as cedidas saem; o conteúdo delas fica
    fora de bin no endereço)."""
    from models import BinContent

    gone = set(r.removed_bins or [])
    rows = db.execute(select(BinContent.bin_type_id, BinContent.seq, BinContent.product_id, BinContent.quantity)
                      .where(BinContent.location_id == r.id)).all()
    kept: dict[int, list[int]] = {}
    for t, seq, _p, _q in rows:
        if (t, seq) not in gone and seq not in kept.setdefault(t, []):
            kept[t].append(seq)
    new_seq = {(t, s_): i + 1 for t, seqs in kept.items() for i, s_ in enumerate(sorted(seqs))}
    return [(t, new_seq[(t, seq)], pid, q) for t, seq, pid, q in rows if (t, seq) in new_seq]


def apply(db: Session, location_ids: list[int], prm: Params, only: Optional[set] = None,
          donors: Optional[list[int]] = None) -> int:
    """Recalcula (mesmos parâmetros, no escopo todo) e grava as sugestões aplicáveis — todas, ou só as de `only`
    (com bins existentes, mais os endereços que liberam as bins que eles recebem). Retorna nº de endereços alterados."""
    results, _x, _x = run(db, location_ids, prm, donors)
    chosen = None
    if only is not None:
        chosen = inventory_closure(results, only) if prm.limit_inventory else set(only)
    changed = 0
    touched: list[int] = []
    loose: dict[int, list] = {}  # doador "órfão": conteúdo das bins que ficam (o resto fica fora de bin)
    for r in results:
        if not r.applicable or (chosen is not None and r.id not in chosen):
            continue
        if r.status == "orfao":
            loose[r.id] = _kept_contents(db, r)
        if r.placement:
            rows = []
            for key, cfg in r.placement.items():
                lvl_id = None if key == r.own_level else ensure_level(db, key).id
                rows += [(t, n, lvl_id) for t, n in cfg.items() if n > 0]
            set_location_bins(db, r.id, rows)
        else:
            set_location_bins(db, r.id, sorted(r.suggested.items()))
        changed += 1
        touched.append(r.id)
    db.flush()
    from services.bin_content_service import refresh_contents

    refresh_contents(db, [i for i in touched if i not in loose], force=True, prm=prm)  # conteúdo = distribuição do plano
    from services.bin_content_service import set_contents

    for lid, rows in loose.items():
        set_contents(db, lid, rows, allow_loose=True)
    fit_structure(db)  # bin mais alta que o vão: a estrutura (vão) sobe, a bin fica
    db.commit()
    return changed
