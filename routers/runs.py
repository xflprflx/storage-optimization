"""Resultados de otimização guardados: lista, detalhe, comparação lado a lado e Excel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import FormData

from database import get_db
from models import Branch, OptimizationRun
from routers.common import form_data, paginate, redirect, render, to_int
from services.run_history import KINDS, kpi_rows, param_diff
from services.i18n import N_, _, translate_workbook

router = APIRouter(prefix="/optimization/runs", tags=[N_("Resultados de otimização")])


@router.get("")
def list_runs(request: Request, kind: str = "", branch_id: str = "", q: str = "", page: int = 1,
              db: Session = Depends(get_db)):
    stmt = select(OptimizationRun).options(joinedload(OptimizationRun.branch))
    if kind in KINDS:
        stmt = stmt.where(OptimizationRun.kind == kind)
    if to_int(branch_id):
        stmt = stmt.where(OptimizationRun.branch_id == to_int(branch_id))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(OptimizationRun.title.ilike(like) | OptimizationRun.note.ilike(like))
    pg = paginate(db, stmt.order_by(OptimizationRun.updated_at.desc(), OptimizationRun.id.desc()), page)
    counts = dict(db.execute(select(OptimizationRun.kind, func.count()).group_by(OptimizationRun.kind)).all())
    return render(request, "runs/list.html", page=pg, kinds=KINDS, kind=kind, branch_id=branch_id, q=q, counts=counts,
                  branches=list(db.scalars(select(Branch).order_by(Branch.code))))


@router.post("/bulk-delete")
def runs_bulk_delete(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Exclui os resultados marcados ou, com `all`, todos os do filtro (modo, filial, busca)."""
    from sqlalchemy import delete

    back = str(form.get("back") or "/optimization/runs")
    back = back if back.startswith("/optimization/runs") else "/optimization/runs"
    if form.get("all") == "1":
        stmt = delete(OptimizationRun)
        kind, bid, q = str(form.get("kind") or ""), to_int(form.get("branch_id")), str(form.get("q") or "").strip()
        if kind in KINDS:
            stmt = stmt.where(OptimizationRun.kind == kind)
        if bid:
            stmt = stmt.where(OptimizationRun.branch_id == bid)
        if q:
            stmt = stmt.where(OptimizationRun.title.ilike(f"%{q}%") | OptimizationRun.note.ilike(f"%{q}%"))
    else:
        ids = [i for i in (to_int(v) for v in form.getlist("ids")) if i]
        if not ids:
            return redirect(back, _("Marque os resultados a excluir."), "warning")
        stmt = delete(OptimizationRun).where(OptimizationRun.id.in_(ids))
    n = db.execute(stmt).rowcount or 0
    db.commit()
    return redirect(back if form.get("all") != "1" else "/optimization/runs", _("{n} resultado(s) excluído(s).", n=n), "secondary")


@router.get("/compare")
def compare_runs(request: Request, db: Session = Depends(get_db)):
    ids = [i for i in (to_int(v) for v in request.query_params.getlist("ids")) if i]
    runs = [r for r in (db.get(OptimizationRun, i) for i in ids) if r is not None]
    if len(runs) < 2:
        return redirect("/optimization/runs", _("Escolha pelo menos dois resultados para comparar."), "warning")
    return render(request, "runs/compare.html", runs=runs, rows=kpi_rows(runs), diff=param_diff(runs), kinds=KINDS)


@router.get("/{run_id}")
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(OptimizationRun, run_id)
    if run is None:
        return redirect("/optimization/runs", _("Resultado não encontrado."), "danger")
    return render(request, "runs/detail.html", run=run, kinds=KINDS)


@router.get("/{run_id}/export.xlsx")
def run_xlsx(run_id: int, db: Session = Depends(get_db)):
    from io import BytesIO

    from openpyxl import Workbook

    from routers.site import _attachment

    run = db.get(OptimizationRun, run_id)
    if run is None:
        return redirect("/optimization/runs", _("Resultado não encontrado."), "danger")
    wb = Workbook()
    ws = wb.active
    ws.title = _("Indicadores")
    ws.append([_("Indicador"), _("Antes"), _("Depois / valor"), _("Unidade")])
    for k in (run.summary or {}).get("kpis", []):
        ws.append([k["label"], k.get("before"), k.get("after") if k.get("after") is not None else k.get("value"),
                   (k.get("unit") or "").strip()])
    for name, key in ((_("Detalhes"), "rows"), (_("Estruturas"), "structs"), (N_("Bins"), "types")):
        rows = (run.details or {}).get(key) or []
        if rows:
            sh = wb.create_sheet(name)
            cols = list(rows[0].keys())
            sh.append(cols)
            for r in rows:
                sh.append([r.get(c) for c in cols])
    ps = wb.create_sheet(_("Parâmetros"))
    for k, v in sorted((run.params or {}).items()):
        ps.append([k, v if isinstance(v, (int, float, str)) or v is None else str(v)])
    buf = BytesIO()
    translate_workbook(wb)
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": _attachment(f"resultado_{run.id}_{run.kind}.xlsx")})


@router.post("/{run_id}/note")
def run_note(run_id: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    run = db.get(OptimizationRun, run_id)
    if run is None:
        return redirect("/optimization/runs", _("Resultado não encontrado."), "danger")
    run.note = str(form.get("note") or "").strip()[:300]
    db.commit()
    return redirect(f"/optimization/runs/{run_id}", _("Nome/observação salvo."))


@router.post("/{run_id}/delete")
def run_delete(run_id: int, db: Session = Depends(get_db)):
    run = db.get(OptimizationRun, run_id)
    if run is not None:
        db.delete(run)
        db.commit()
    return redirect("/optimization/runs", _("Resultado excluído."), "secondary")
