"""Plano de obra e movimentação das propostas de rearranjo, recomposição e combinada.

Junta, numa lista de etapas na ordem de execução, o que precisa ser feito fisicamente:
  1. estantes que mudam de lugar (rearranjo): esvaziar, mover a estante, recarregar — as bins e os SKUs vão com ela;
  2. estantes a recompor: esvaziar, reespaçar/trocar a estrutura, recarregar no MESMO nível (o endereço não muda) —
     quando a estante também muda de lugar, é uma etapa só (esvazia uma vez);
  3. ocupar os níveis ganhos: a recomposição é simulada (sem gravar) e o "Por endereço" roda nas estantes recompostas
     com outros níveis liberados e o max level como base: as bins que vão para os níveis novos — as que saem de outro
     nível do endereço ou bins novas;
  4. movimentações de SKUs (proposta combinada sobre a proposta do otimizador): de qual endereço para qual.
Cada etapa traz as bins, as unidades e os SKUs envolvidos e o tempo estimado (parâmetros da tela).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.i18n import _

TIMES = {"bin_s": 20.0, "move_min": 60.0, "respace_min": 30.0, "piece_s": 2.0}  # padrões da tela


def times_from(src) -> dict:
    """Tempos da tela (segundos por bin tirada e recolocada, minutos por estante movida e por estante recomposta,
    segundos por unidade movida entre endereços)."""
    out = {}
    for k, v in TIMES.items():
        try:
            x = float(str(src.get(f"t_{k}") if src.get(f"t_{k}") not in (None, "") else v).replace(",", "."))
            out[k] = max(0.0, x)
        except ValueError:
            out[k] = v
    return out


def _canon(v) -> str:
    from services.app_settings import canon

    return canon(v or "")


def bay_contents(db: Session, branch_id: int, wid, bays: set) -> dict:
    """{(corredor, estante): {"addresses", "bins", "types": {código: qtd}, "units", "skus"}} das estantes."""
    from models import BinType, LocationBin, StockItem, StorageLocation

    want = {(_canon(a), _canon(b)) for a, b in bays}
    q = select(StorageLocation).where(StorageLocation.branch_id == branch_id)
    q = q.where(StorageLocation.warehouse_id.is_(None) if wid is None else StorageLocation.warehouse_id == wid)
    locs = {l.id: (_canon(l.aisle), _canon(l.bay)) for l in db.scalars(q) if (_canon(l.aisle), _canon(l.bay)) in want}
    out = {k: {"addresses": 0, "bins": 0, "types": defaultdict(int), "units": 0.0, "skus": set()} for k in want}
    for k in locs.values():
        out[k]["addresses"] += 1
    ids = list(locs)
    codes = {t.id: t.code for t in db.scalars(select(BinType))}
    for i in range(0, len(ids), 900):
        for lb in db.scalars(select(LocationBin).where(LocationBin.location_id.in_(ids[i:i + 900]))):
            k = locs[lb.location_id]
            out[k]["bins"] += lb.quantity
            out[k]["types"][codes.get(lb.bin_type_id, "?")] += lb.quantity
        for lid, pid, qv in db.execute(select(StockItem.location_id, StockItem.product_id, StockItem.quantity)
                                       .where(StockItem.location_id.in_(ids[i:i + 900]), StockItem.quantity > 0)):
            k = locs[lid]
            out[k]["units"] += float(qv)
            out[k]["skus"].add(pid)
    return out


def fill_after_recompose(db: Session, branch_id: int, wid, changes: list[dict]) -> dict:
    """Simula a recomposição (sem gravar) e roda o Por endereço nas estantes recompostas, com outros níveis liberados e
    o max level como base: as bins que vão para os níveis novos. Retorna {"rows": [...], "created": níveis novos,
    "free": bins de capacidade que sobram neles}. Desfaz tudo no fim (rollback da sessão)."""
    from models import LevelCapacity, ShelfLevel, StorageLocation
    from services.bin_optimizer import Params
    from services.optimization_service import run
    from services.recompose import apply_changes

    if not changes:
        return {"rows": [], "created": 0}
    nested = db.begin_nested()
    try:
        _stats, created = apply_changes(db, branch_id, wid, changes)
        new_levels = {(_canon(k.split("|")[0]), _canon(k.split("|")[1]), _canon(c)) for k, cs in created.items() for c in cs}
        bays = {(_canon(c["aisle"]), _canon(c["bay"])) for c in changes}
        q = select(StorageLocation).where(StorageLocation.branch_id == branch_id)
        q = q.where(StorageLocation.warehouse_id.is_(None) if wid is None else StorageLocation.warehouse_id == wid)
        locs = [l for l in db.scalars(q) if (_canon(l.aisle), _canon(l.bay)) in bays]
        ids = [l.id for l in locs]
        addr = {l.id: l.address for l in locs}
        rows = []
        if ids and new_levels:
            # só os tipos de bin que as estantes já usam (a recomposição foi feita para eles)
            from models import LocationBin

            used = set(db.scalars(select(LocationBin.bin_type_id).where(LocationBin.location_id.in_(ids)).distinct()))
            prm = Params(allow_other_levels=True, basis="max", limit_inventory=False, objective="min_bins",
                         respect_levels=True, engine="heuristic", time_limit=10.0, allowed_types=used or None)
            results, _summary, types = run(db, ids, prm)
            for r in results:
                if not getattr(r, "applicable", False) or not r.placement:
                    continue
                cur_total = defaultdict(int)
                for _k, cfg in (r.current_placement or {}).items():
                    for t, n in cfg.items():
                        cur_total[t] += n
                for key, cfg in r.placement.items():
                    if not isinstance(key, tuple) or len(key) < 5:
                        continue
                    if (_canon(key[2]), _canon(key[3]), _canon(key[4])) not in new_levels:
                        continue
                    for t, n in cfg.items():
                        if n <= 0:
                            continue
                        sug_total = sum(c.get(t, 0) for c in r.placement.values())
                        src = _("bins do próprio endereço (de outro nível)") if sug_total <= cur_total.get(t, 0) else _("bins novas")
                        rows.append({"address": addr.get(r.id, r.address), "level": key[4],
                                     "bay": f"{key[2]}-{key[3]}", "type": types[t].code if t in types else str(t),
                                     "qty": n, "source": src})
        # capacidade que sobra nos níveis novos (bins do tipo de referência)
        free = 0
        for a, b, lv in new_levels:
            sl = db.scalar(select(ShelfLevel).where(ShelfLevel.branch_id == branch_id, ShelfLevel.aisle == a,
                                                    ShelfLevel.bay == b, ShelfLevel.level == lv))
            if sl is None:
                continue
            caps = [c.max_bins for c in db.scalars(select(LevelCapacity).where(LevelCapacity.shelf_level_id == sl.id))]
            used = sum(n for r in rows if (r["bay"], r["level"]) == (f"{a}-{b}", lv) for n in [r["qty"]])
            free += max(0, (max(caps) if caps else 0) - used)
        return {"rows": sorted(rows, key=lambda r: (r["bay"], r["level"], r["address"])), "created": len(new_levels),
                "free": free}
    finally:
        # desfaz a simulação e fecha a transação (no SQLite ela segura a trava de escrita): a sessão não pode ter
        # mudanças pendentes do chamador — as telas do plano de obra só leem
        nested.rollback()
        db.rollback()


def opt_moves(db: Session, assign_before: dict, assign_after: dict) -> list[dict]:
    """Movimentações de SKUs entre a distribuição de hoje e a da proposta do otimizador: de qual endereço para qual."""
    from models import StorageLocation

    rows = []
    ids = {l for d in (assign_before, assign_after) for lst in d.values() for l, _q in lst}
    addr = dict(db.execute(select(StorageLocation.id, StorageLocation.address).where(StorageLocation.id.in_(list(ids))))
                .all()) if ids else {}
    for sku, after in assign_after.items():
        b = {l: q for l, q in assign_before.get(sku, [])}
        a = {l: q for l, q in after}
        out = [(l, q) for l, q in b.items() if l not in a]
        into = [(l, q) for l, q in a.items() if l not in b]
        if not out and not into:
            continue
        rows.append({"sku": sku, "from": ", ".join(addr.get(l, str(l)) for l, _q in out) or "—",
                     "to": ", ".join(addr.get(l, str(l)) for l, _q in into) or "—",
                     "units": round(sum(q for _l, q in out) or sum(q for _l, q in into), 3)})
    return sorted(rows, key=lambda r: r["sku"])


def build_plan(db: Session, branch_id: int, wid, moved: set, changes: list[dict], times: dict,
               fill: Optional[dict] = None, moves: Optional[list] = None, title: str = "") -> dict:
    """O plano de obra e movimentação: etapas na ordem, com bins, unidades, SKUs e tempo."""
    ACT = {"respace": _("reespaçar os níveis"), "swap": _("trocar a estrutura")}  # no meio da frase
    by_bay = {(_canon(c["aisle"]), _canon(c["bay"])): c for c in changes}
    moved = {(_canon(a), _canon(b)) for a, b in moved}
    cont = bay_contents(db, branch_id, wid, set(by_bay) | moved)
    steps = []

    def add(stage, bay, action, c=None, minutes=0.0, detail=""):
        steps.append({"n": len(steps) + 1, "stage": stage, "bay": bay, "action": action,
                      "addresses": c["addresses"] if c else None, "bins": c["bins"] if c else None,
                      "types": ", ".join(f"{q}× {t}" for t, q in sorted(c["types"].items())) if c else "",
                      "units": round(c["units"], 3) if c else None, "skus": len(c["skus"]) if c else None,
                      "minutes": round(minutes, 1), "detail": detail})
    handle = lambda c: c["bins"] * 2 * times["bin_s"] / 60.0  # noqa: E731 — tirar e recolocar cada bin
    for k in sorted(moved | set(by_bay), key=lambda k: (k[0], k[1])):
        c, ch = cont.get(k), by_bay.get(k)
        bay = f"{k[0]}-{k[1]}"
        if k in moved and ch:
            add(_("Planta + recomposição"), bay, _("Esvaziar, mudar a estante de lugar, {act} e recarregar cada bin no mesmo nível",
                                                  act=ACT.get(ch["kind"], ch["kind"])), c,
                handle(c) + times["move_min"] + times["respace_min"],
                _("{a} → {b} níveis · {f} → {t}", a=ch["levels_from"], b=ch["levels_to"], f=ch["from_code"], t=ch["to_code"]))
        elif k in moved:
            add(_("Planta"), bay, _("Esvaziar, mudar a estante de lugar e recarregar (as bins e os SKUs vão com ela)"), c,
                handle(c) + times["move_min"])
        else:
            add(_("Recomposição"), bay, _("Esvaziar, {act} e recarregar cada bin no mesmo nível", act=ACT.get(ch["kind"], ch["kind"])), c,
                handle(c) + times["respace_min"],
                _("{a} → {b} níveis · {f} → {t} · +{g} bin(s) de capacidade", a=ch["levels_from"], b=ch["levels_to"],
                  f=ch["from_code"], t=ch["to_code"], g=ch["gain"]))
    for r in (fill or {}).get("rows") or []:
        steps.append({"n": len(steps) + 1, "stage": _("Ocupar níveis ganhos"), "bay": r["bay"],
                      "action": _("Pôr {q}× {t} do endereço {a} no nível novo {lv}", q=r["qty"], t=r["type"], a=r["address"], lv=r["level"]),
                      "addresses": 1, "bins": r["qty"], "types": f"{r['qty']}× {r['type']}", "units": None, "skus": None,
                      "minutes": round(r["qty"] * times["bin_s"] / 60.0, 1), "detail": r["source"]})
    for m in moves or []:
        steps.append({"n": len(steps) + 1, "stage": _("Movimentar SKUs"), "bay": "",
                      "action": _("{sku}: de {a} para {b}", sku=m["sku"], a=m["from"], b=m["to"]),
                      "addresses": None, "bins": None, "types": "", "units": m["units"], "skus": 1,
                      "minutes": round((m["units"] or 0) * times["piece_s"] / 60.0, 1), "detail": ""})
    tot = lambda f: round(sum(s[f] or 0 for s in steps), 1)  # noqa: E731
    by_stage = defaultdict(lambda: {"steps": 0, "minutes": 0.0})
    for s in steps:
        by_stage[s["stage"]]["steps"] += 1
        by_stage[s["stage"]]["minutes"] += s["minutes"]
    return {"title": title, "steps": steps, "times": times,
            "summary": {"steps": len(steps), "bins": tot("bins"), "units": tot("units"), "hours": round(tot("minutes") / 60.0, 1),
                        "moved": len(moved), "recomp": len(by_bay),
                        "fill": len((fill or {}).get("rows") or []), "fill_free": (fill or {}).get("free"),
                        "fill_levels": (fill or {}).get("created"), "moves": len(moves or []),
                        "stages": [{"stage": k, "steps": v["steps"], "hours": round(v["minutes"] / 60.0, 1)} for k, v in by_stage.items()]}}


def to_xlsx(plan: dict) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = _("Plano de obra")
    ws.append([plan["title"]])
    ws["A1"].font = Font(bold=True, size=13)
    S = plan["summary"]
    ws.append([_("Etapas: {n} · bins: {b} · unidades: {u} · horas estimadas: {h}", n=S["steps"], b=S["bins"], u=S["units"],
                 h=S["hours"])])
    head = ["#", _("Etapa"), _("Estante"), _("Ação"), _("Endereços"), _("Bins"), _("Bins por tipo"), _("Unidades"), "SKUs",
            _("Minutos"), _("Detalhe")]
    ws.append(head)
    for c in ws[3]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F2A2A")
    for s in plan["steps"]:
        ws.append([s["n"], s["stage"], s["bay"], s["action"], s["addresses"], s["bins"], s["types"], s["units"], s["skus"],
                   s["minutes"], s["detail"]])
    for col, w in zip("ABCDEFGHIJK", (5, 22, 10, 70, 10, 8, 26, 10, 7, 9, 50)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
