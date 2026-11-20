"""Propostas de rearranjo (planta) e de recomposição (estruturas) de uma filial existente:
  - carimbo do momento em que a proposta foi gerada (planta e última recomposição), para avisar quando a proposta
    ficou desatualizada porque a outra foi aplicada (ou desfeita) depois;
  - proposta combinada: para cada variação da planta (rearranjo), a recomposição calculada NESSA planta (as laterais
    compartilhadas seguem as estantes vizinhas dela), com um único "aplicar" que grava as duas.
"""
from __future__ import annotations

from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.i18n import _


def stamp(db: Session, branch_id: int, wid: Optional[int]) -> dict:
    """Estado da planta e das estruturas da filial: muda quando um rearranjo ou uma recomposição é aplicado ou
    desfeito (ou a planta é editada)."""
    from models import RecompositionLog
    from services.branch_layout import get_layout

    lay = get_layout(db, branch_id, wid)
    log = db.scalar(select(RecompositionLog).where(RecompositionLog.branch_id == branch_id,
                                                   RecompositionLog.warehouse_id.is_(None) if wid is None
                                                   else RecompositionLog.warehouse_id == wid)
                    .order_by(RecompositionLog.id.desc()))
    import hashlib
    import json

    body = json.dumps(lay.data or {}, sort_keys=True, default=str) if lay is not None else ""
    return {"layout": hashlib.sha1(body.encode()).hexdigest() if body else "",
            "recomp": f"{log.id}:{int(bool(log.undone))}:{log.updated_at}" if log is not None else ""}


def stale_reason(db: Session, branch_id: int, wid: Optional[int], old: Optional[dict], kind: str) -> str:
    """Por que a proposta (`kind` = "recompose", "rearrange" ou "combined") ficou desatualizada; "" = em dia."""
    if not old:
        return ""
    now = stamp(db, branch_id, wid)
    why = []
    if kind in ("recompose", "combined") and now["layout"] != old.get("layout"):
        why.append(_("a planta da filial mudou (rearranjo aplicado ou desfeito, ou planta editada)"))
    if kind in ("rearrange", "combined") and now["recomp"] != old.get("recomp"):
        why.append(_("uma recomposição das estruturas foi aplicada ou desfeita"))
    return "; ".join(why)


def propose_combined(db: Session, branch_id: int, wid: Optional[int], assign: Optional[dict] = None,
                     pieces: str = "existing", min_gain: int = 1, progress: Optional[Callable] = None,
                     cancelled: Optional[Callable] = None) -> dict:
    """Variações da planta (rearranjo) e, em cada uma, a recomposição das estruturas calculada nela."""
    from services.branch_layout import build, geometry, scope_bays
    from services.rearrange import propose_branch
    from services.recompose import propose

    prog = progress or (lambda *_a: None)
    stop = cancelled or (lambda: False)
    st0 = stamp(db, branch_id, wid)
    rr = propose_branch(db, branch_id, wid, assign=assign,
                        progress=lambda i, n, msg="": prog(i, n * 2, msg), cancelled=stop)
    if rr.get("error"):
        return rr
    try:
        g_now = geometry(db, branch_id, wid)
    except Exception:  # noqa: BLE001
        g_now = None
    bays = scope_bays(db, branch_id, wid)
    good = [v for v in rr["variants"] if v.get("data") and not v.get("error")]
    options = []
    for k, v in enumerate(good):
        if stop():
            break
        prog(len(good) + k, len(good) * 2, _("Recomposição na variação: {label}", label=v["label"]))
        g_new = g_now if v.get("current") else build(v["data"], bays)
        rc = propose(db, branch_id, wid, pieces=pieces, min_gain=min_gain, time_s=8.0, cancelled=stop,
                     geo=g_new, geo_have=g_now)
        bought = [p for p in rc.get("pieces") or [] if p["bought"] > 1e-9]
        options.append({"variant": v, "recomp": rc, "gain": rc.get("gain", 0), "bought": bought,
                        "feasible": rc.get("status") != "infeasible"})
    best_walk = min((o for o in options if o["variant"].get("opt_avg") is not None and o["feasible"]),
                    key=lambda o: o["variant"]["opt_avg"], default=None)
    best_gain = max((o for o in options if o["feasible"]), key=lambda o: o["gain"], default=None)
    for o in options:
        o["best_walk"], o["best_gain"] = o is best_walk, o is best_gain and o["gain"] > 0
    prog(1, 1, _("Pronto"))
    return {"options": options, "orders": rr.get("orders"), "basis": rr.get("basis"), "pieces_mode": pieces,
            "min_gain": min_gain, "branch_id": branch_id, "warehouse_id": wid, "stamp": st0, "partial": bool(stop()),
            "skipped": [v for v in rr["variants"] if v.get("error")]}


def apply_combined(db: Session, res: dict, idx: int) -> tuple[str, str]:
    """Grava a opção `idx`: a planta da variação (a anterior fica guardada para Desfazer rearranjo) e a recomposição
    calculada nela (Desfazer a última recomposição). Retorna (mensagem, nível do aviso)."""
    from services.branch_layout import clean, get_layout
    from services.recompose import apply as apply_recompose

    try:
        opt = res["options"][idx]
    except (IndexError, KeyError, TypeError):
        return _("A proposta expirou: gere de novo."), "warning"
    bid, wid = res["branch_id"], res["warehouse_id"]
    if not opt["feasible"]:
        return _("Esta opção não cabe nas peças existentes: gere a proposta com peças ilimitadas."), "warning"
    parts = []
    v = opt["variant"]
    if not v.get("current"):
        lay = get_layout(db, bid, wid)
        if lay is None:
            return _("A filial não tem planta desenhada."), "warning"
        prev = {k: x for k, x in (lay.data or {}).items() if k != "_prev"}
        lay.data = {**clean(v["data"]), "_prev": prev}
        db.commit()
        parts.append(_("planta: {label}", label=v["label"]))
    rc = opt["recomp"]
    if rc.get("changes"):
        st = apply_recompose(db, rc)
        parts.append(_("recomposição em {bays} estante(s) ({created} nível(is) criado(s), {removed} removido(s))",
                       bays=st["bays"], created=st["levels_created"], removed=st["levels_removed"]))
    if not parts:
        return _("Nada a aplicar nesta opção (a planta atual, sem recomposição)."), "info"
    return (_("Proposta combinada aplicada — {v}. Para voltar: Desfazer rearranjo (na planta) e Desfazer a última "
              "recomposição (em Capacidade dos níveis).", v="; ".join(parts)), "success")
