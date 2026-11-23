"""Plano de ocupação X%: o max level de cada SKU para que as bins que ele ocupa hoje fiquem com X% do volume.

Para que serve: eliminar a ociosidade volumétrica (bins com espaço sobrando), subir o max level para a filial receber
lotes maiores — menos reposições a partir do CD que a abastece, menos separação lá e menos frete.

Como calcula, por SKU, nos endereços do escopo:
- as bins do SKU são as do conteúdo por bin (BinContent: o informado ou o presumido pela distribuição do saldo);
- a capacidade de uma bin para o SKU é a mesma da otimização (medidas, rotação, peso, unitização da filial) com o
  enchimento máximo = X%; numa bin com mais de um SKU, cada um fica com a parte proporcional ao volume que ocupa hoje;
- novo max level = a soma das capacidades (arredondado para embalagens inteiras, se pedido), nunca abaixo do saldo; com
  "só aumentar", nunca abaixo do max level atual;
- com o histórico de saídas da filial: cobertura (dias) e reposições por mês antes e depois — reposição = repor do min
  ao max (lote = max − min; sem min, o max).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.i18n import _


@dataclass
class PlanRow:
    product_id: int
    sku: str
    name: str
    addresses: list = field(default_factory=list)
    bins: int = 0
    capacity: int = 0  # unidades que cabem nas bins do SKU a X%
    stock: float = 0.0
    min_level: Optional[float] = None
    max_level: Optional[float] = None
    new_max: Optional[int] = None
    fill_now: Optional[float] = None  # % do volume das bins ocupado pelo saldo
    fill_max_now: Optional[float] = None  # % com o max level atual
    fill_new: Optional[float] = None  # % com o novo max level
    daily: Optional[float] = None  # unidades por dia (histórico)
    note: str = ""
    pack: int = 0

    @property
    def delta(self) -> Optional[float]:
        if self.new_max is None:
            return None
        return self.new_max - (self.max_level or 0)

    def coverage(self, level) -> Optional[float]:
        return round(level / self.daily, 1) if level and self.daily else None

    def per_month(self, level) -> Optional[float]:
        if not level or not self.daily:
            return None
        lot = level - (self.min_level or 0) if self.min_level and level > self.min_level else level
        return round(self.daily * 30.0 / lot, 2) if lot > 0 else None


def _vol(p) -> float:
    return float(p.height) * float(p.width) * float(p.length) if p.height and p.width and p.length else 0.0


def _bin_vol(bt) -> float:
    from services.branch_insights import _bin_vol as bv

    return bv(bt)


def history_days(data: Optional[dict], default: int = 30) -> int:
    """Dias do período do histórico (pelas datas dos pedidos); sem datas, o informado."""
    from datetime import date

    ds = (data or {}).get("dates")
    if ds and len(ds) == 2:
        try:
            a, b = date.fromisoformat(str(ds[0])[:10]), date.fromisoformat(str(ds[1])[:10])
            return max(1, (b - a).days + 1)
        except ValueError:
            pass
    return max(1, int(default or 30))


def build(db: Session, branch_id: int, location_ids: list[int], target: float, only_increase: bool = True,
          round_packs: bool = True, days: int = 30, unit_run: str = "branch") -> dict:
    """O plano de ocupação `target`% (1–100) nos endereços do escopo da filial."""
    from models import BinContent, BinType, PickHistory, Product, StockItem, StockPolicy, StorageLocation
    from services.bin_content_service import _caps, refresh_contents
    from services.bin_optimizer import Params
    from services.packaging import branch_mode, product_packs, run_mode

    target = max(1.0, min(100.0, float(target)))
    ids = list(location_ids)
    refresh_contents(db, ids)  # o conteúdo por bin (presumido quando não informado) coerente com o saldo
    db.flush()
    types = {t.id: t for t in db.scalars(select(BinType))}
    addr = {}
    contents = []
    for i in range(0, len(ids), 900):
        for c in db.scalars(select(BinContent).where(BinContent.location_id.in_(ids[i:i + 900]), BinContent.quantity > 0)):
            contents.append(c)
        for lid, a in db.execute(select(StorageLocation.id, StorageLocation.address).where(StorageLocation.id.in_(ids[i:i + 900]))):
            addr[lid] = a
    pids = sorted({c.product_id for c in contents})
    prods = {}
    for i in range(0, len(pids), 900):
        for p in db.scalars(select(Product).where(Product.id.in_(pids[i:i + 900]))):
            prods[p.id] = p
    stock = defaultdict(float)
    for i in range(0, len(ids), 900):
        for pid, q in db.execute(select(StockItem.product_id, StockItem.quantity).where(StockItem.location_id.in_(ids[i:i + 900]))):
            stock[pid] += float(q)
    pols = {p.product_id: p for p in db.scalars(select(StockPolicy).where(StockPolicy.branch_id == branch_id))}
    mode = run_mode(branch_mode(db, branch_id), unit_run)
    prm = Params(fill_limit=target / 100.0)

    # bins físicas (endereço, tipo, nº) e o que está em cada uma: a capacidade é dividida pelo volume ocupado hoje
    phys = defaultdict(list)
    for c in contents:
        phys[(c.location_id, c.bin_type_id, c.seq)].append(c)
    cap_cache: dict = {}
    rows: dict[int, PlanRow] = {}
    bin_vol = defaultdict(float)
    for (lid, tid, _seq), items in phys.items():
        bt = types.get(tid)
        vols = {c.product_id: _vol(prods[c.product_id]) * float(c.quantity) for c in items if c.product_id in prods}
        tv = sum(vols.values())
        for c in items:
            p = prods.get(c.product_id)
            if p is None:
                continue
            r = rows.get(p.id)
            if r is None:
                r = rows[p.id] = PlanRow(p.id, p.sku, p.name)
            if lid in addr and addr[lid] not in r.addresses:
                r.addresses.append(addr[lid])
            share = (vols.get(p.id, 0.0) / tv) if tv else 1.0 / len(items)
            r.bins += 1
            if bt is None:
                continue
            key = (p.id, tid)
            if key not in cap_cache:
                cap_cache[key] = _caps(p, bt, prm, (mode, branch_id)) or 0
            r.capacity += int(math.floor(cap_cache[key] * share + 1e-9))
            bin_vol[p.id] += _bin_vol(bt) * share
    # histórico: unidades por dia
    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    hd = (h.data or {}) if h is not None else {}
    ndays = history_days(hd, days)
    daily = {s: float(v[1]) / ndays for s, v in (hd.get("skus") or {}).items() if v and len(v) > 1 and v[1]}

    out_rows = []
    for pid, r in rows.items():
        p = prods[pid]
        pol = pols.get(pid)
        r.stock = round(stock.get(pid, 0.0), 4)
        r.min_level = float(pol.min_level) if pol is not None and pol.min_level is not None else None
        r.max_level = float(pol.max_level) if pol is not None and pol.max_level is not None else None
        r.daily = daily.get(p.sku)
        uv, bv = _vol(p), bin_vol.get(pid, 0.0)
        pct = (lambda q: round(q * uv * 100.0 / bv, 1) if uv and bv and q is not None else None)  # noqa: E731
        r.fill_now, r.fill_max_now = pct(r.stock), pct(r.max_level)
        if not uv:
            r.note = _("SKU sem medidas: fica de fora.")
            out_rows.append(r)
            continue
        if not r.capacity:
            r.note = _("Não cabe nas bins atuais com {p}%: confira medidas ou troque a bin.", p=f"{target:g}")
            out_rows.append(r)
            continue
        new = r.capacity
        # o lote que o CD manda: a 1ª embalagem habilitada na filial (mesmo com a unitização da armazenagem desligada)
        pk, _alts = product_packs(p, "cases", branch_id)
        if round_packs and pk is not None and pk.qty > 1:
            r.pack = pk.qty
            new = (new // pk.qty) * pk.qty or new  # embalagens inteiras (sem zerar)
        new = max(new, int(math.ceil(r.stock - 1e-9)))  # nunca abaixo do saldo
        if only_increase and r.max_level:
            new = max(new, int(math.ceil(r.max_level - 1e-9)))
        r.new_max = new
        r.fill_new = pct(new)
        if r.max_level and new < r.max_level:
            r.note = _("O max level atual não cabe nas bins com {p}%.", p=f"{target:g}")
        out_rows.append(r)
    out_rows.sort(key=lambda r: (-(r.delta or 0), r.sku))

    ok = [r for r in out_rows if r.new_max is not None]

    def tot(f):
        vals = [f(r) for r in ok]
        vals = [v for v in vals if v is not None]
        return round(sum(vals), 2) if vals else None
    hist = [r for r in ok if r.daily]
    vol_tot = sum(bin_vol.get(r.product_id, 0.0) for r in ok)

    def wfill(level_of):
        if not vol_tot:
            return None
        used = sum((level_of(r) or 0) * _vol(prods[r.product_id]) for r in ok)
        return round(used * 100.0 / vol_tot, 1)
    summary = {
        "target": target, "skus": len(out_rows), "planned": len(ok),
        "skipped": len(out_rows) - len(ok), "raised": sum(1 for r in ok if (r.delta or 0) > 0),
        "lowered": sum(1 for r in ok if (r.delta or 0) < 0), "no_max": sum(1 for r in ok if not r.max_level),
        "max_now": tot(lambda r: r.max_level), "max_new": tot(lambda r: r.new_max),
        "fill_stock": wfill(lambda r: r.stock), "fill_max_now": wfill(lambda r: r.max_level),
        "fill_new": wfill(lambda r: r.new_max),
        "history": bool(daily), "days": ndays, "hist_skus": len(hist),
        "repl_now": round(sum(r.per_month(r.max_level) or 0 for r in hist if r.max_level), 1) if hist else None,
        "repl_new": round(sum(r.per_month(r.new_max) or 0 for r in hist), 1) if hist else None,
        "unit_mode": mode,
    }
    return {"rows": out_rows, "summary": summary}


def apply(db: Session, branch_id: int, plan_rows: list[PlanRow], only: Optional[set] = None) -> int:
    """Grava o novo max level na política de estoque da filial (cria a política quando não há)."""
    from models import StockPolicy

    pols = {p.product_id: p for p in db.scalars(select(StockPolicy).where(StockPolicy.branch_id == branch_id))}
    n = 0
    for r in plan_rows:
        if r.new_max is None or (only is not None and r.product_id not in only):
            continue
        pol = pols.get(r.product_id)
        if pol is None:
            pol = StockPolicy(branch_id=branch_id, product_id=r.product_id)
            db.add(pol)
        if pol.max_level is None or float(pol.max_level) != r.new_max:
            pol.max_level = Decimal(r.new_max)
            n += 1
    db.flush()
    return n


def to_xlsx(plan: dict, title: str) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = _("Plano de ocupação")
    head = [_("SKU"), _("Produto"), _("Endereços"), _("Bins"), _("Saldo"), _("Min level"), _("Max level atual"),
            _("Capacidade a {p}%", p=f"{plan['summary']['target']:g}"), _("Max level sugerido"), _("Variação"),
            _("Ocupação com o saldo (%)"), _("Ocupação com o max atual (%)"), _("Ocupação com o novo max (%)"),
            _("Saída por dia"), _("Cobertura atual (dias)"), _("Cobertura nova (dias)"), _("Reposições/mês atuais"),
            _("Reposições/mês novas"), _("Observação")]
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(head)
    for c in ws[2]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F2A2A")
    for r in plan["rows"]:
        ws.append([r.sku, r.name, ", ".join(r.addresses), r.bins, r.stock, r.min_level, r.max_level, r.capacity,
                   r.new_max, r.delta, r.fill_now, r.fill_max_now, r.fill_new,
                   round(r.daily, 3) if r.daily else None, r.coverage(r.max_level), r.coverage(r.new_max),
                   r.per_month(r.max_level), r.per_month(r.new_max), r.note])
    for col, w in zip("ABCDEFGHIJKLMNOPQRS", (14, 36, 28, 7, 10, 10, 12, 14, 14, 10, 12, 12, 12, 10, 12, 12, 12, 12, 40)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B3"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
