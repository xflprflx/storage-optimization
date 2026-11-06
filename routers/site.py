"""Projeto de filial nova: cadastro do imóvel e das estruturas candidatas, otimização do layout e da quantidade de
estruturas/bins, exportação e geração do cadastro da filial."""
from __future__ import annotations

import copy

from fastapi import APIRouter, Depends, File, Request, UploadFile
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
    cfg = config_from_form(form)
    cfg["items"] = list((pr.config or {}).get("items") or [])  # SKUs do arquivo não vêm no formulário
    cfg["import_mapping"] = dict((pr.config or {}).get("import_mapping") or {})
    pr.config = cfg
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


# --------------------------------------------------------------------------- #
# Importação dos dados da filial nova (planilha modelo)
# --------------------------------------------------------------------------- #
@router.get("/{pid}/template.xlsx")
def download_template(pid: int, db: Session = Depends(get_db)):
    from services.site_import import template_xlsx

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    data = template_xlsx(db, merged(copy.deepcopy(pr.config)))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="modelo_filial_nova_{pid}.xlsx"'})


@router.post("/{pid}/upload")
async def upload_data(pid: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """1ª etapa: guarda o arquivo e mostra o mapeamento de abas e colunas (pré-preenchido)."""
    from services.file_reader import UPLOAD_DIR, remove_upload, save_upload
    from services.site_import import FIELD_LABELS, KIND_LABELS, REQUIRED, auto_mapping, guess_columns, read_sheets

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    try:
        token = await save_upload(file)
    except ValueError as exc:
        return redirect(f"/optimization/site/{pid}", str(exc), "danger")
    try:
        preview = read_sheets(UPLOAD_DIR / token, limit=6)
    except Exception as exc:  # arquivo corrompido / formato inesperado
        remove_upload(token)
        return redirect(f"/optimization/site/{pid}", f"Não foi possível ler o arquivo: {exc}", "danger")
    if not preview or not any(preview.values()):
        remove_upload(token)
        return redirect(f"/optimization/site/{pid}", "O arquivo não tem linhas.", "danger")
    remembered = ((pr.config or {}).get("import_mapping") or {})
    sheets = {name: {"headers": rows[0] if rows else [], "rows": rows[1:6]} for name, rows in preview.items()}
    # mapeamento automático de cada tipo para CADA aba (a tela reaplica ao trocar a aba)
    guesses = {kind: {name: guess_columns(kind, sh["headers"], remembered.get(kind)) for name, sh in sheets.items()}
               for kind in KIND_LABELS}
    return render(request, "site/mapping.html", project=pr, token=token, filename=file.filename or "",
                  sheets=sheets, mapping=auto_mapping(preview, remembered), guesses=guesses,
                  KIND_LABELS=KIND_LABELS, FIELD_LABELS=FIELD_LABELS, REQUIRED=REQUIRED)


@router.post("/{pid}/upload/confirm")
def confirm_upload(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """2ª etapa: importa com as abas/colunas escolhidas."""
    from services.file_reader import remove_upload, token_path
    from services.site_import import FIELD_LABELS, KIND_LABELS, import_file

    pr = _get(db, pid)
    token = str(form.get("token") or "")
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    try:
        path = token_path(token)
    except ValueError as exc:
        return redirect(f"/optimization/site/{pid}", str(exc), "danger")
    if form.get("action") == "cancel":
        remove_upload(token)
        return redirect(f"/optimization/site/{pid}", "Importação cancelada.", "secondary")
    mapping = {}
    for kind in KIND_LABELS:
        sheet = form.get(f"sheet_{kind}") or ""
        if not sheet:
            continue
        cols = {}
        for field in FIELD_LABELS[kind]:
            raw = str(form.get(f"col_{kind}_{field}") or "")
            cols[field] = int(raw) if raw.isdigit() else None
        mapping[kind] = {"sheet": sheet, "cols": cols}
    try:
        cfg, msgs, errs = import_file(db, path, merged(copy.deepcopy(pr.config)), mapping)
    except Exception as exc:
        db.rollback()
        remove_upload(token)
        return redirect(f"/optimization/site/{pid}", f"Não foi possível importar: {exc}", "danger")
    remove_upload(token)
    pr.config = cfg
    db.commit()
    text = " ".join(msgs)
    if errs:
        text = (text + " Avisos: " if text else "Nada importado. ") + " ".join(errs[:12]) + (" …" if len(errs) > 12 else "")
    return redirect(f"/optimization/site/{pid}", text or "Nada importado.", ("warning" if msgs else "danger") if errs else "success")


@router.post("/{pid}/clear-items")
def clear_items(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    cfg = merged(copy.deepcopy(pr.config))
    cfg["items"] = []
    cfg["demand"]["source"] = "catalog"
    pr.config = cfg
    db.commit()
    return redirect(f"/optimization/site/{pid}", "SKUs do arquivo removidos; a demanda volta a vir do cadastro.")
