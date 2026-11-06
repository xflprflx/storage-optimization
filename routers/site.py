"""Projeto de filial nova: cadastro do imóvel e das estruturas candidatas, otimização do layout e da quantidade de
estruturas/bins, exportação e geração do cadastro da filial."""
from __future__ import annotations

import copy

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import BinType, Branch, SiteProject, StructureType, Warehouse
from routers.common import form_data, redirect, render
from services.addressing import normalize_code
from services.bin_optimizer import BASES, OBJECTIVES
from services.optimization_service import load_types
from services.site_planner import ORIENTATIONS, SITE_OBJECTIVES, STRUCT_MODES, floor_svg, struct_colors
from services.site_service import BUILDING_FIELDS, config_from_form, create_branch, merged, run_project, to_xlsx

router = APIRouter(prefix="/optimization/site", tags=["Projeto de filial nova"])


def _get(db: Session, pid: int) -> SiteProject | None:
    return db.get(SiteProject, pid)


@router.get("")
def list_projects(request: Request, db: Session = Depends(get_db)):
    projects = list(db.scalars(select(SiteProject).order_by(SiteProject.updated_at.desc(), SiteProject.id.desc())))
    n_structs = len(list(db.scalars(select(StructureType.id))))
    return render(request, "site/index.html", projects=projects, n_structs=n_structs)


@router.post("/new")
def create_project(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    name = (form.get("name") or "").strip()[:150] or "Nova filial"
    cfg = merged(None)
    for st in db.scalars(select(StructureType)):
        cfg["structures"][str(st.id)] = {"mode": "auto", "qty": "", "min_qty": "", "max_qty": ""}
    pr = SiteProject(name=name, config=cfg)
    db.add(pr)
    db.commit()
    return redirect(f"/optimization/site/{pr.id}", "Projeto criado. Informe as dimensões do imóvel e escolha as estruturas.")


def _context(db: Session, pr: SiteProject, cfg: dict) -> dict:
    structs = list(db.scalars(select(StructureType).order_by(StructureType.code)))
    for st in structs:  # estrutura cadastrada depois do projeto: entra desligada
        cfg["structures"].setdefault(str(st.id), {"mode": "off", "qty": "", "min_qty": "", "max_qty": ""})
    return dict(
        project=pr, cfg=cfg, structures=structs, bin_types=list(db.scalars(select(BinType).order_by(BinType.code))),
        branches=list(db.scalars(select(Branch).order_by(Branch.code))),
        warehouses=list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
        BUILDING_FIELDS=BUILDING_FIELDS, ORIENTATIONS=ORIENTATIONS, SITE_OBJECTIVES=SITE_OBJECTIVES,
        STRUCT_MODES=STRUCT_MODES, BASES=BASES, OBJECTIVES=OBJECTIVES,
    )


@router.get("/{pid}")
def show_project(pid: int, request: Request, run: str = "", db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    cfg = merged(copy.deepcopy(pr.config))
    ctx = _context(db, pr, cfg)
    plan, errors = (run_project(db, cfg) if run else (None, []))
    types = {t.id: t for t in ctx["bin_types"]}
    return render(request, "site/project.html", **ctx, plan=plan, errors=errors, types=types,
                  svg=floor_svg(plan) if plan else "", colors=struct_colors(plan) if plan else {})


@router.post("/{pid}")
def save_project(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    pr.name = (form.get("name") or "").strip()[:150] or pr.name
    pr.config = config_from_form(form)
    db.commit()
    if form.get("action") == "run":
        return redirect(f"/optimization/site/{pid}?run=1#resultado")
    return redirect(f"/optimization/site/{pid}", "Projeto salvo.")


@router.post("/{pid}/duplicate")
def duplicate_project(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    cp = SiteProject(name=f"{pr.name} (cópia)"[:150], config=copy.deepcopy(pr.config))
    db.add(cp)
    db.commit()
    return redirect(f"/optimization/site/{cp.id}", "Projeto duplicado: compare cenários alterando a cópia.")


@router.post("/{pid}/delete")
def delete_project(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if pr:
        db.delete(pr)
        db.commit()
    return redirect("/optimization/site", "Projeto excluído.")


@router.get("/{pid}/plan.xlsx")
def export_xlsx(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    plan, errors = run_project(db, merged(copy.deepcopy(pr.config)))
    if errors or plan is None:
        return redirect(f"/optimization/site/{pid}", " ".join(errors) or "Não foi possível gerar o plano.", "danger")
    data = to_xlsx(plan, pr, load_types(db))
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pr.name)[:60] or "projeto"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="filial_{safe}.xlsx"'})


@router.post("/{pid}/create-branch")
def create_branch_from_plan(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    back = f"/optimization/site/{pid}?run=1"
    code, name = normalize_code(form.get("branch_code")), (form.get("branch_name") or "").strip()[:150]
    wh_code, wh_name = normalize_code(form.get("wh_code")), (form.get("wh_name") or "").strip()[:150]
    if not code or len(code) > 30 or not name:
        return redirect(back, "Informe código (até 30 caracteres) e nome da filial nova.", "danger")
    if db.scalar(select(Branch.id).where(Branch.code == code)):
        return redirect(back, f"Já existe uma filial com o código {code}.", "danger")
    plan, errors = run_project(db, merged(copy.deepcopy(pr.config)))
    if errors or plan is None or plan.errors or not plan.modules:
        return redirect(back, "O plano não é válido para gerar o cadastro: " + " ".join(errors + (plan.errors if plan else [])),
                        "danger")
    link = str(form.get("link_skus") or "").lower() in {"1", "on", "true"}
    br, stats = create_branch(db, plan, code, name, wh_code, wh_name, link_skus=link)
    db.commit()
    return redirect(f"/layout?branch_id={br.id}",
                    f"Filial {br.code} criada: {stats['levels']} níveis, {stats['locations']} endereços e "
                    f"{stats['bins']} bins.")
