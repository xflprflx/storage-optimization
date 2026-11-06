"""
Conteúdo das bins físicas (qual bin guarda cada SKU e quanto).

Bin física = (endereço, tipo, nº). O nº segue a ordem das bins do endereço: para cada tipo, primeiro as que
estão no nível do próprio endereço, depois as de níveis extras (na ordem dos níveis).

Regras de consistência (refresh_contents):
  - Conteúdo informado (presumed=False) é mantido enquanto for coerente: todas as bins existem e a soma por SKU
    é igual ao saldo do SKU no endereço. Se o saldo ou as bins mudarem e deixar de ser coerente, o endereço volta
    para a distribuição presumida (e a função informa quais endereços foram recalculados).
  - Distribuição presumida (presumed=True): o sistema enche as bins em ordem, um SKU por bin, do maior volume para
    o menor, respeitando a capacidade de cada bin (geometria, volume e peso). Se as bins não forem suficientes, o
    excedente fica na última bin do SKU (a bin aparece acima de 100%).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from models import BinContent, BinType, LocationBin, Product, ShelfLevel, StockItem

EPS = Decimal("0.0001")


@dataclass(frozen=True)
class PhysBin:
    location_id: int
    type_id: int
    seq: int
    level: str = ""  # nível onde a bin está (vazio = nível do próprio endereço)


def _chunks(ids, n=900):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def physical_bins(db: Session, location_ids: Iterable[int]) -> dict[int, list[PhysBin]]:
    from services.layout_service import natural_key

    out: dict[int, list[PhysBin]] = {}
    lv_code = {}
    rows = []
    for ch in _chunks(set(location_ids)):
        rows += db.execute(select(LocationBin.location_id, LocationBin.bin_type_id, LocationBin.quantity,
                                  LocationBin.shelf_level_id).where(LocationBin.location_id.in_(ch))).all()
    sl_ids = {r[3] for r in rows if r[3]}
    if sl_ids:
        lv_code = dict(db.execute(select(ShelfLevel.id, ShelfLevel.level).where(ShelfLevel.id.in_(sl_ids))).all())
    rows.sort(key=lambda r: (r[0], r[1], r[3] is not None, natural_key(lv_code.get(r[3], "")) if r[3] else (0,)))
    seq: dict[tuple, int] = {}
    for lid, tid, qty, sl in rows:
        for _ in range(qty or 0):
            k = (lid, tid)
            seq[k] = seq.get(k, 0) + 1
            out.setdefault(lid, []).append(PhysBin(lid, tid, seq[k], lv_code.get(sl, "") if sl else ""))
    return out


def _caps(product: Product, bt: BinType, prm=None) -> Optional[int]:
    """Capacidade de uma bin do tipo para o produto. Sem `prm`: encaixe geométrico, enchimento 100%
    (conteúdo de hoje). Com `prm`: os mesmos parâmetros da otimização (enchimento máximo, rotação...)."""
    from services.bin_optimizer import BinSpec, Params, ProductUnit, capacity

    f = lambda v: float(v) if v is not None else None  # noqa: E731
    p = ProductUnit(product.sku, 0, f(product.height), f(product.width), f(product.length), f(product.weight))
    b = BinSpec(bt.id, bt.code, f(bt.height), f(bt.width), f(bt.length), f(bt.volume), f(bt.max_weight))
    if not p.has_dims or b.volume_cm3 is None:
        return None
    return capacity(p, b, prm if prm is not None else Params(fill_limit=1.0))


def split_units(total: Decimal, weights: list[float]) -> list[Decimal]:
    """Divide `total` em partes proporcionais aos pesos (unidades inteiras quando o total é inteiro;
    maiores restos primeiro). Com total >= nº de partes, nenhuma parte fica zerada."""
    n = len(weights)
    if n == 0:
        return []
    w = [max(float(x), 0.0) for x in weights]
    if sum(w) <= 0:
        w = [1.0] * n
    tw = sum(w)
    if total != total.to_integral_value():
        parts = [total * Decimal(str(x / tw)) for x in w]
        parts[-1] = total - sum(parts[:-1])
        return parts
    t = int(total)
    raw = [t * x / tw for x in w]
    out = [int(r) for r in raw]
    for i in sorted(range(n), key=lambda i: -(raw[i] - out[i]))[: t - sum(out)]:
        out[i] += 1
    if t >= n:  # nenhuma bin vazia: tira 1 das maiores para as zeradas
        for i in range(n):
            if out[i] == 0:
                j = max(range(n), key=lambda k: out[k])
                out[j] -= 1
                out[i] += 1
    return [Decimal(x) for x in out]


def presume_distribution(bins: list[PhysBin], stock: dict[int, Decimal], products: dict[int, Product],
                         types: dict[int, BinType], prm=None) -> list[tuple[PhysBin, int, Decimal]]:
    """Distribuição automática do saldo pelas bins: um SKU por bin, maior volume primeiro. Bins que sobrariam vazias
    vão para os SKUs mais apertados, e cada SKU é dividido entre as suas bins na proporção da capacidade —
    assim nenhuma bin do endereço fica vazia enquanto houver unidades para dividir."""
    if not bins:
        return []
    vol = lambda pid: float(products[pid].height or 0) * float(products[pid].width or 0) * float(products[pid].length or 0)  # noqa: E731
    order = sorted((pid for pid, q in stock.items() if q > 0), key=lambda pid: (-vol(pid) * float(stock[pid]), products[pid].sku))
    cap = lambda pid, b: _caps(products[pid], types[b.type_id], prm) if b.type_id in types else None  # noqa: E731
    free = list(bins)
    mine: dict[int, list[PhysBin]] = {}
    for pid in order:
        rest = stock[pid]
        got = mine.setdefault(pid, [])
        while rest > 0 and free:
            b = free.pop(0)
            c = cap(pid, b)
            take = rest if c is None else min(rest, Decimal(max(c, 0)))
            if take <= 0:
                free.append(b)  # não cabe nesta bin: tenta as próximas
                caps_left = [cap(pid, x) for x in free if x.type_id in types]
                if all(x is not None and x <= 0 for x in caps_left):
                    break
                continue
            got.append(b)
            rest -= take
    # bins que sobraram: para o SKU mais apertado que caiba nela e tenha unidades para dividir
    for b in list(free):
        best, best_key = None, None
        for pid in order:
            got = mine.get(pid, [])
            c = cap(pid, b)
            if not got or (c is not None and c <= 0) or stock[pid] < len(got) + 1:
                continue
            room = sum(float(cap(pid, x) or 0) for x in got) or 1.0
            k = float(stock[pid]) / room
            if best_key is None or k > best_key:
                best, best_key = pid, k
        if best is not None:
            mine[best].append(b)
            free.remove(b)
    out: list[tuple[PhysBin, int, Decimal]] = []
    for pid in order:
        got = mine.get(pid, [])
        if not got:
            got = [bins[-1]]  # nenhuma bin comporta: fica na última (aparece como excedente)
        caps_ = [cap(pid, b) for b in got]
        weights = [float(c) if c else 1.0 for c in caps_]
        for b, q in zip(got, split_units(stock[pid], weights)):
            if q > 0:
                out.append((b, pid, q))
    return out


def refresh_contents(db: Session, location_ids: Optional[Iterable[int]] = None, force: bool = False, prm=None) -> list[int]:
    """Garante conteúdo coerente nos endereços. Retorna os endereços cujo conteúdo informado foi descartado."""
    if location_ids is None:
        location_ids = set(db.scalars(select(LocationBin.location_id).distinct())) | set(
            db.scalars(select(BinContent.location_id).distinct()))
    ids = {i for i in location_ids if i}
    if not ids:
        return []
    phys = physical_bins(db, ids)
    stock: dict[int, dict[int, Decimal]] = {}
    contents: dict[int, list[BinContent]] = {}
    for ch in _chunks(ids):
        for lid, pid, q in db.execute(select(StockItem.location_id, StockItem.product_id, StockItem.quantity)
                                      .where(StockItem.location_id.in_(ch), StockItem.quantity > 0)):
            stock.setdefault(lid, {})[pid] = q
        for bc in db.scalars(select(BinContent).where(BinContent.location_id.in_(ch))):
            contents.setdefault(bc.location_id, []).append(bc)
    pids = {p for s in stock.values() for p in s}
    products = {}
    for ch in _chunks(pids):
        products.update({p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ch)))})
    types = {t.id: t for t in db.scalars(select(BinType))}
    dropped: list[int] = []
    to_insert = []
    to_delete = []
    for lid in ids:
        bins = phys.get(lid, [])
        cur = contents.get(lid, [])
        st = stock.get(lid, {})
        manual = [c for c in cur if not c.presumed]
        if cur and not manual and not force:
            # presumido igual à distribuição presumida atual: mantém (sem regravar)
            fresh = sorted((b.type_id, b.seq, pid, round(float(q), 4)) for b, pid, q in presume_distribution(bins, st, products, types, prm))
            if fresh == sorted((c.bin_type_id, c.seq, c.product_id, round(float(c.quantity), 4)) for c in cur):
                continue
        if manual and not force:
            valid = {(b.type_id, b.seq) for b in bins}
            sums: dict[int, Decimal] = {}
            ok = True
            for c in manual:
                if (c.bin_type_id, c.seq) not in valid:
                    ok = False
                sums[c.product_id] = sums.get(c.product_id, Decimal("0")) + c.quantity
            # informado e coerente: mantém (a soma pode ser menor que o saldo: o resto está fora de bin, ex. órfãos)
            if ok and set(sums) <= set(st) and all(sums[p] <= st[p] + EPS for p in sums) and len(manual) == len(cur):
                continue
            dropped.append(lid)
        # recalcula a distribuição presumida
        to_delete.append(lid)
        for b, pid, q in presume_distribution(bins, st, products, types, prm):
            to_insert.append({"location_id": lid, "bin_type_id": b.type_id, "seq": b.seq, "product_id": pid,
                              "quantity": q, "presumed": prm is None})  # com prm: distribuição do plano aplicado (fica)
    for ch in _chunks(to_delete):
        db.execute(delete(BinContent).where(BinContent.location_id.in_(ch)))
    if to_insert:
        db.execute(insert(BinContent), to_insert)
    db.flush()
    return dropped


def set_contents(db: Session, location_id: int, rows: list[tuple[int, int, int, Decimal]],
                 allow_loose: bool = False) -> Optional[str]:
    """Grava o conteúdo informado de um endereço: [(tipo, nº da bin, produto, qtd)].
    Valida que as bins existem e que a soma por SKU bate com o saldo (`allow_loose`: pode ser menor — o resto fica
    fora de bin no endereço, ex.: itens órfãos da otimização). Retorna erro ou None."""
    bins = {(b.type_id, b.seq) for b in physical_bins(db, [location_id]).get(location_id, [])}
    stock = dict(db.execute(select(StockItem.product_id, StockItem.quantity)
                            .where(StockItem.location_id == location_id, StockItem.quantity > 0)).all())
    merged: dict[tuple, Decimal] = {}
    for t, seq, pid, q in rows:
        if q is None or q <= 0:
            continue
        if (t, seq) not in bins:
            return f"A bin nº {seq} do tipo informado não existe neste endereço."
        if pid not in stock:
            return "Só é possível colocar nas bins produtos que têm saldo neste endereço."
        merged[(t, seq, pid)] = merged.get((t, seq, pid), Decimal("0")) + Decimal(str(q))
    sums: dict[int, Decimal] = {}
    for (t, seq, pid), q in merged.items():
        sums[pid] = sums.get(pid, Decimal("0")) + q
    for pid, q in stock.items():
        if allow_loose and sums.get(pid, Decimal("0")) <= q + EPS:
            continue
        if abs(sums.get(pid, Decimal("0")) - q) > EPS:
            sku = db.get(Product, pid).sku
            return f"{sku}: a soma nas bins ({sums.get(pid, 0):g}) precisa ser igual ao saldo do endereço ({q:g})."
    db.execute(delete(BinContent).where(BinContent.location_id == location_id))
    if merged:
        db.execute(insert(BinContent), [{"location_id": location_id, "bin_type_id": t, "seq": seq, "product_id": pid,
                                         "quantity": q, "presumed": False} for (t, seq, pid), q in merged.items()])
    db.flush()
    return None


def contents_view(db: Session, location_id: int) -> list[dict]:
    """Para a tela do endereço: cada bin física com o que tem dentro e a ocupação estimada."""
    bins = physical_bins(db, [location_id]).get(location_id, [])
    types = {t.id: t for t in db.scalars(select(BinType))}
    rows = db.scalars(select(BinContent).where(BinContent.location_id == location_id)).all()
    by_bin: dict[tuple, list[BinContent]] = {}
    for c in rows:
        by_bin.setdefault((c.bin_type_id, c.seq), []).append(c)
    out = []
    for b in bins:
        items = by_bin.get((b.type_id, b.seq), [])
        fill = 0.0
        known = True
        for c in items:
            cap = _caps(c.product, types[b.type_id]) if b.type_id in types else None
            if cap:
                fill += float(c.quantity) / cap
            else:
                known = False
        out.append({"bin": b, "type": types.get(b.type_id), "contents": items, "fill": fill * 100 if known and items else (None if items else 0),
                    "presumed": any(c.presumed for c in items)})
    return out


def planned_bins(location_id: int, cfg_by_level: dict, own_level) -> list[PhysBin]:
    """Bins físicas de uma configuração sugerida ({nível: {tipo: qtd}}), na mesma numeração do cadastro:
    por tipo, primeiro as do nível do endereço, depois as de outros níveis (ordem natural)."""
    from services.layout_service import natural_key

    levels = sorted(cfg_by_level, key=lambda k: (k != own_level, natural_key(k[4]) if isinstance(k, tuple) else (0,)))
    seq: dict[int, int] = {}
    out = []
    for t in sorted({t for cfg in cfg_by_level.values() for t in cfg}):
        for lv in levels:
            for _ in range(cfg_by_level[lv].get(t, 0)):
                seq[t] = seq.get(t, 0) + 1
                out.append(PhysBin(location_id, t, seq[t], "" if lv == own_level else (lv[4] if isinstance(lv, tuple) else "")))
    return out


def content_changes(db: Session, changes: dict[int, tuple], prm=None) -> dict[int, dict]:
    """Para o plano por endereço: o que tirar das bins e como guardar nas novas.
    `changes` = {endereço: (config sugerida {nível: {tipo: qtd}}, nível do endereço)}.
    Retorna {endereço: {"empty": [(bin, sku, qtd)], "store": [(bin, [(sku, qtd)])], "after": {(tipo, nº): [...]}}}."""
    if not changes:
        return {}
    types = {t.id: t for t in db.scalars(select(BinType))}
    before_bins = physical_bins(db, changes)
    cur: dict[int, dict[tuple, list]] = {}
    stock: dict[int, dict[int, Decimal]] = {}
    pids: set[int] = set()
    for ch in _chunks(changes):
        for c in db.scalars(select(BinContent).where(BinContent.location_id.in_(ch))):
            cur.setdefault(c.location_id, {}).setdefault((c.bin_type_id, c.seq), []).append((c.product_id, c.quantity))
            pids.add(c.product_id)
        for lid, pid, q in db.execute(select(StockItem.location_id, StockItem.product_id, StockItem.quantity)
                                      .where(StockItem.location_id.in_(ch), StockItem.quantity > 0)):
            stock.setdefault(lid, {})[pid] = q
            pids.add(pid)
    products = {}
    for ch in _chunks(pids):
        products.update({p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ch)))})
    tag = lambda t, seq, lv="": f"{types[t].code if t in types else t} #{seq}" + (f" ↳{lv}" if lv else "")  # noqa: E731
    out = {}
    for lid, (cfg, own) in changes.items():
        after = planned_bins(lid, cfg, own)
        after_c: dict[tuple, list] = {}
        for b, pid, q in presume_distribution(after, stock.get(lid, {}), products, types, prm):
            after_c.setdefault((b.type_id, b.seq), []).append((pid, q))
        before_c = cur.get(lid, {})
        norm = lambda items: sorted((p, round(float(q), 4)) for p, q in items)  # noqa: E731
        empty, store = [], []
        lv_before = {(b.type_id, b.seq): b.level for b in before_bins.get(lid, [])}
        for (t, seq), items in sorted(before_c.items()):
            if norm(items) != norm(after_c.get((t, seq), [])):
                for pid, q in items:
                    empty.append((tag(t, seq, lv_before.get((t, seq), "")), products[pid].sku if pid in products else pid, q))
        lv_after = {(b.type_id, b.seq): b.level for b in after}
        for (t, seq), items in sorted(after_c.items()):
            if norm(items) != norm(before_c.get((t, seq), [])):
                store.append((tag(t, seq, lv_after.get((t, seq), "")),
                              [(products[p].sku if p in products else p, q) for p, q in items]))
        out[lid] = {"empty": empty, "store": store}
    return out


def arrangement_rows(db: Session, results, prm=None) -> dict[int, dict]:
    """Para a tabela da otimização por endereço: o conteúdo de cada bin HOJE e DEPOIS da sugestão.
    Retorna {endereço: {"before": [(PhysBin, [(sku, qtd)])], "after": [...] | None}} (after None = não muda)."""
    ids = [r.id for r in results]
    if not ids:
        return {}
    types = {t.id: t for t in db.scalars(select(BinType))}
    phys = physical_bins(db, ids)
    cur: dict[int, dict[tuple, list]] = {}
    stock: dict[int, dict[int, Decimal]] = {}
    pids: set[int] = set()
    for ch in _chunks(ids):
        for c in db.scalars(select(BinContent).where(BinContent.location_id.in_(ch))):
            cur.setdefault(c.location_id, {}).setdefault((c.bin_type_id, c.seq), []).append((c.product_id, c.quantity))
            pids.add(c.product_id)
        for lid, pid, q in db.execute(select(StockItem.location_id, StockItem.product_id, StockItem.quantity)
                                      .where(StockItem.location_id.in_(ch), StockItem.quantity > 0)):
            stock.setdefault(lid, {})[pid] = q
            pids.add(pid)
    products = {}
    for ch in _chunks(pids):
        products.update({p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ch)))})
    sku = lambda pid: products[pid].sku if pid in products else str(pid)  # noqa: E731
    out = {}
    for r in results:
        before = [(b, [(sku(p), q) for p, q in cur.get(r.id, {}).get((b.type_id, b.seq), [])]) for b in phys.get(r.id, [])]
        after = None
        if r.applicable:
            own = r.own_level if r.own_level is not None else "own"
            cfg = r.placement if r.placement else {own: dict(r.suggested or {})}
            nb = planned_bins(r.id, cfg, own)
            dist: dict[tuple, list] = {}
            for b, pid, q in presume_distribution(nb, stock.get(r.id, {}), products, types, prm):
                dist.setdefault((b.type_id, b.seq), []).append((sku(pid), q))
            after = [(b, dist.get((b.type_id, b.seq), [])) for b in nb]
        out[r.id] = {"before": before, "after": after}
    return out


def address_scenario(db: Session, r, planned: dict[str, float], prm=None):
    """Conteúdo DEPOIS de uma sugestão por endereço, separando unidades físicas e espaço reservado.
    `planned` = {SKU: quantidade planejada} (saldo, min level ou max level). Retorna None (não muda) ou
    [(PhysBin, [(SKU, físicas, reserva)])]: o planejado é distribuído pelas bins sugeridas e o saldo físico
    ocupa essas mesmas posições; o que sobra do planejado é reserva."""
    if not r.applicable:
        return None
    types = {t.id: t for t in db.scalars(select(BinType))}
    stock = {pid: q for pid, q in db.execute(select(StockItem.product_id, StockItem.quantity)
                                               .where(StockItem.location_id == r.id, StockItem.quantity > 0))}
    skus = set(planned)
    products = {p.id: p for p in db.scalars(select(Product).where(
        (Product.id.in_(list(stock))) | (Product.sku.in_(list(skus)))))}
    pid_of = {p.sku: p.id for p in products.values()}
    want: dict[int, Decimal] = dict(stock)
    for sku, q in planned.items():
        pid = pid_of.get(sku)
        if pid is not None:
            want[pid] = max(Decimal(str(q)), stock.get(pid, Decimal(0)))
    own = r.own_level if r.own_level is not None else "own"
    cfg = r.placement if r.placement else {own: dict(r.suggested or {})}
    nb = planned_bins(r.id, cfg, own)
    dist = presume_distribution(nb, want, products, types, prm)
    # saldo físico dividido entre as bins planejadas do SKU na proporção do planejado (nenhuma fica só com reserva
    # enquanto houver unidades para dividir); o que falta para o planejado é reserva
    per_pid: dict[int, list] = {}
    for b, pid, n in dist:
        per_pid.setdefault(pid, []).append((b, n))
    by_bin: dict[tuple, list] = {}
    for pid, lst in per_pid.items():
        phys = split_units(stock.get(pid, Decimal(0)), [float(n) for _b, n in lst])
        for (b, n), f in zip(lst, phys):
            by_bin.setdefault((b.type_id, b.seq), []).append((products[pid].sku, float(f), max(float(n) - float(f), 0.0)))
    return [(b, by_bin.get((b.type_id, b.seq), [])) for b in nb]
