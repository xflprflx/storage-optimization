"""Projeto de filial nova: cadastro do imóvel e das estruturas candidatas, otimização do layout e da quantidade de
estruturas/bins, exportação e geração do cadastro da filial."""
from __future__ import annotations

import copy
import shutil
import uuid
from pathlib import Path

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
from services.site_planner import (CAP_ALIGN, ORIENTATIONS, SITE_CRITERIA, SITE_OBJECTIVES, STRUCT_MODES, ZONE_ORIENTATIONS,
                                   floor_svg, struct_colors, dim_mode)
from services.site_service import (BUILDING_FIELDS, DEFAULT_LOCATION_MODES, config_from_form, create_branch, merged,
                                   run_project)

router = APIRouter(prefix="/optimization/site", tags=["Projeto de filial nova"])


def _attachment(filename: str) -> str:
    """Content-Disposition seguro: nome ASCII (sem acentos) + o nome original codificado (RFC 5987).
    O cabeçalho HTTP só aceita latin-1; nomes de projeto com acento ou travessão quebravam o download."""
    import unicodedata
    from urllib.parse import quote

    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    ascii_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in ascii_name).strip("_") or "arquivo"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _get(db: Session, pid: int) -> SiteProject | None:
    return db.get(SiteProject, pid)


# Último plano exibido de cada projeto: o cadastro da filial e o Excel usam exatamente o plano aprovado na tela
# (o solver tem tempo limite e pode achar outra solução equivalente numa nova execução). Vale enquanto a
# configuração do projeto não mudar; numa reinicialização do sistema o plano é recalculado.
_PLAN_CACHE: dict[int, tuple[str, object]] = {}


def _cfg_key(db: Session, cfg: dict) -> str:
    """Configuração do projeto + "versão" dos cadastros que entram no plano (estruturas, bins, produtos, saldos)."""
    import json

    from sqlalchemy import func

    from models import BinType, Product, StockItem

    marks = [db.execute(select(func.count(), func.max(M.updated_at))).one()
             for M in (StructureType, BinType, Product, StockItem)]
    return json.dumps([cfg, [tuple(map(str, m)) for m in marks]], sort_keys=True, default=str)


def _plan_for(db: Session, pr: SiteProject, fresh: bool = False):
    """(plano, erros, configuração) do projeto: o último exibido, se a configuração for a mesma."""
    cfg = merged(copy.deepcopy(pr.config))
    key = _cfg_key(db, cfg)
    hit = _PLAN_CACHE.get(pr.id)
    if not fresh and hit and hit[0] == key:
        return hit[1], [], cfg
    plan, errors = run_project(db, cfg)
    if plan is not None and not errors:
        _PLAN_CACHE[pr.id] = (key, plan)
    return plan, errors, cfg


@router.get("")
def list_projects(request: Request, db: Session = Depends(get_db)):
    projects = list(db.scalars(select(SiteProject).order_by(SiteProject.updated_at.desc(), SiteProject.id.desc())))
    n_structs = len(list(db.scalars(select(StructureType.id))))
    from services.site_examples import EXAMPLES, preview_svg

    examples = [{**{k: e[k] for k in ("slug", "title", "description", "tags")}, "svg": preview_svg(e["slug"])}
                for e in EXAMPLES]
    return render(request, "site/index.html", projects=projects, n_structs=n_structs, examples=examples)


def _example_file(slug: str) -> Path | None:
    """Planilha do exemplo (gerada na hora se ainda não existir). None = exemplo desconhecido."""
    from services.site_examples import BY_SLUG, path_of, write_workbook

    if slug not in BY_SLUG:
        return None
    path = path_of(slug)
    if not path.exists():
        write_workbook(BY_SLUG[slug], path)
    return path


def _new_project(db: Session, name: str) -> SiteProject:
    cfg = merged(None)
    for st in db.scalars(select(StructureType)):
        cfg["structures"][str(st.id)] = {"mode": "auto", "qty": "", "min_qty": "", "max_qty": ""}
    pr = SiteProject(name=name[:150] or "Nova filial", config=cfg)
    db.add(pr)
    db.commit()
    return pr


@router.post("/new")
async def create_project(request: Request, db: Session = Depends(get_db)):
    """Cria o projeto. Com arquivo (ou "exemplo"), segue direto para o mapeamento das colunas da planilha."""
    from services.file_reader import UPLOAD_DIR, save_upload

    form = await request.form()
    name = (form.get("name") or "").strip()
    upload = form.get("file")
    has_file = upload is not None and getattr(upload, "filename", "")
    if form.get("sample"):
        from services.site_examples import BY_SLUG, DEFAULT

        slug = form.get("sample") if form.get("sample") in BY_SLUG else DEFAULT
        src = _example_file(slug)
        pr = _new_project(db, name or f"Exemplo — {BY_SLUG[slug]['title']}")
        token = f"{uuid.uuid4().hex}.xlsx"
        shutil.copyfile(src, UPLOAD_DIR / token)
        return _mapping_page(request, pr, token, src.name)
    token = None
    if has_file:
        try:
            token = await save_upload(upload)
        except ValueError as exc:
            return redirect("/optimization/site", str(exc), "danger")
    pr = _new_project(db, name or (Path(upload.filename).stem if has_file else "Nova filial"))
    if token:
        return _mapping_page(request, pr, token, upload.filename)
    return redirect(f"/optimization/site/{pr.id}", "Projeto criado. Informe as dimensões do imóvel e escolha as estruturas.")


@router.get("/examples/{slug}.xlsx")
def download_example(slug: str):
    path = _example_file(slug)
    if path is None:
        return redirect("/optimization/site", "Exemplo não encontrado.", "danger")
    return Response(path.read_bytes(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="exemplo_{slug}.xlsx"'})


@router.get("/seed.xlsx")
def download_seed():
    """Compatibilidade: planilha do exemplo padrão."""
    from services.site_examples import DEFAULT

    return download_example(DEFAULT)


def _context(db: Session, pr: SiteProject, cfg: dict) -> dict:
    structs = list(db.scalars(select(StructureType).order_by(StructureType.code)))
    for st in structs:  # estrutura cadastrada depois do projeto: entra desligada
        cfg["structures"].setdefault(str(st.id), {"mode": "off", "qty": "", "min_qty": "", "max_qty": ""})
    return dict(
        project=pr, cfg=cfg, structures=structs, bin_types=list(db.scalars(select(BinType).order_by(BinType.code))),
        branches=list(db.scalars(select(Branch).order_by(Branch.code))),
        warehouses=list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
        BUILDING_FIELDS=BUILDING_FIELDS, ORIENTATIONS=ORIENTATIONS, SITE_OBJECTIVES=SITE_OBJECTIVES, SITE_CRITERIA=SITE_CRITERIA,
        ZONE_ORIENTATIONS=ZONE_ORIENTATIONS, CAP_ALIGN=CAP_ALIGN,
        DEFAULT_LOCATION_MODES=DEFAULT_LOCATION_MODES,
        STRUCT_MODES=STRUCT_MODES, BASES=BASES, OBJECTIVES=OBJECTIVES,
    )


@router.get("/{pid}")
def show_project(pid: int, request: Request, run: str = "", db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    plan, errors, _ = _plan_for(db, pr, fresh=True) if run else (None, [], None)
    cfg = merged(copy.deepcopy(pr.config))
    ctx = _context(db, pr, cfg)
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
    old_caps = ((pr.config or {}).get("demand") or {}).get("caps") or {}
    if old_caps and form.get("clear_caps") != "1":  # limites por SKU não vêm no formulário
        cfg["demand"]["caps"] = dict(old_caps)
    pr.config = cfg
    db.commit()
    if form.get("action") == "run":
        return redirect(f"/optimization/site/{pid}?run=1#resultado")
    return redirect(f"/optimization/site/{pid}", "Projeto salvo.")


@router.post("/{pid}/layout.json")
def layout_json(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Planta (zonas, corredores e fileiras disponíveis) calculada a partir do formulário, sem salvar: usada pelo
    desenho da planta nas etapas do projeto."""
    from fastapi.responses import JSONResponse

    from services.site_planner import build_layout
    from services.site_service import parse_config, structure_specs

    if not _get(db, pid):
        return JSONResponse({"error": "Projeto não encontrado."}, status_code=404)
    cfg = config_from_form(form)
    b, modes, _prm, errors = parse_config(cfg)
    if not b.length_m or not b.width_m:
        return JSONResponse({"error": "Informe comprimento e largura do imóvel.", "shapes": []})
    active = [s for s in structure_specs(db, modes) if s.mode != "off" and s.height <= b.clear_height_m * 100 + 1e-6]
    lay = build_layout(b, active)
    rnd = lambda v: round(v, 1)  # noqa: E731
    return JSONResponse({"errors": errors, "notes": lay.notes, "rows": len(lay.rows), "aisles": lay.real_aisles,
                         "row_m": round(lay.total_row_m, 1), "shapes": [[k, rnd(x), rnd(y), rnd(w), rnd(d), lb]
                                                                         for k, x, y, w, d, lb in lay.shapes()]})


@router.post("/{pid}/demand-suggestion")
def demand_suggestion(pid: int, request: Request, db: Session = Depends(get_db)):
    """Sugestão de redução da demanda: fator que cabe, limites por SKU e maiores consumidores."""
    from services.site_service import suggest_reduction

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    res = suggest_reduction(db, merged(copy.deepcopy(pr.config)))
    if res.get("error"):
        return redirect(f"/optimization/site/{pid}", res["error"], "danger")
    return render(request, "site/suggest.html", project=pr, res=res)


@router.post("/{pid}/demand-apply")
def demand_apply(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    cfg = merged(copy.deepcopy(pr.config))
    action = form.get("action")
    if action == "factor":
        val = str(form.get("factor") or "").strip()
        cfg["demand"]["factor"] = val
        msg = f"Fator da demanda ajustado para {val}%."
    elif action == "caps":
        skus = form.getlist("cap_sku") if hasattr(form, "getlist") else []
        units = form.getlist("cap_units") if hasattr(form, "getlist") else []
        caps = dict(cfg["demand"].get("caps") or {})
        for s_, u in zip(skus, units):
            if str(u).strip().isdigit():
                caps[s_] = int(u)
        cfg["demand"]["caps"] = caps
        msg = f"Limite de quantidade aplicado a {len(skus)} SKU(s) (total com limite: {len(caps)})."
    elif action == "clear":
        cfg["demand"].pop("caps", None)
        msg = "Limites por SKU removidos."
    else:
        return redirect(f"/optimization/site/{pid}", "Ação inválida.", "danger")
    pr.config = cfg
    db.commit()
    return redirect(f"/optimization/site/{pid}?run=1", msg)


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


def _report(db: Session, pr: SiteProject):
    """Plano detalhado (o mesmo plano aprovado na tela). Retorna (relatório, erro)."""
    from services.site_report import build_report

    plan, errors, cfg = _plan_for(db, pr)
    if errors or plan is None:
        return None, " ".join(errors) or "Não foi possível gerar o plano."
    names = {bt.id: bt.name for bt in db.scalars(select(BinType))}
    return build_report(plan, pr.name, cfg, load_types(db), names), None


def _report_response(request: Request, pid: int, fmt: str, db: Session):
    from services.site_report import to_pdf as report_pdf
    from services.site_report import to_xlsx as report_xlsx

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    rep, err = _report(db, pr)
    if err:
        return redirect(f"/optimization/site/{pid}", err, "danger")
    name = f"plano_filial_{pr.name[:60]}_{rep.created:%Y%m%d_%H%M}"
    if fmt == "xlsx":
        return Response(report_xlsx(rep), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": _attachment(f"{name}.xlsx")})
    if fmt == "pdf":
        return Response(report_pdf(rep, dim_mode(request.query_params.get("dims"))), media_type="application/pdf",
                        headers={"Content-Disposition": _attachment(f"{name}.pdf")})
    return render(request, "site/plan.html", project=pr, rep=rep, plan=rep.plan, svg=floor_svg(rep.plan),
                  colors=struct_colors(rep.plan))


@router.get("/{pid}/plan")
def plan_page(pid: int, request: Request, db: Session = Depends(get_db)):
    return _report_response(request, pid, "html", db)


@router.get("/{pid}/plan.xlsx")
def export_xlsx(pid: int, request: Request, db: Session = Depends(get_db)):
    return _report_response(request, pid, "xlsx", db)


@router.get("/{pid}/plan.pdf")
def export_pdf(pid: int, request: Request, db: Session = Depends(get_db)):
    return _report_response(request, pid, "pdf", db)


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
    plan, errors, cfg = _plan_for(db, pr)
    if errors or plan is None or plan.errors or not plan.modules:
        return redirect(back, "O plano não é válido para gerar o cadastro: " + " ".join(errors + (plan.errors if plan else [])),
                        "danger")
    on = lambda k: str(form.get(k) or "").lower() in {"1", "on", "true"}  # noqa: E731
    from services.site_service import DEFAULT_LOCATION_MODES

    mode = form.get("default_mode") if form.get("default_mode") in DEFAULT_LOCATION_MODES else "empty"
    items = cfg.get("items") if cfg["demand"].get("source") == "upload" else None
    br, stats = create_branch(db, plan, code, name, wh_code, wh_name, link_skus=on("link_skus"), items=items,
                              create_products=on("create_products"), update_products=on("update_products"),
                              default_mode=mode, launch_stock=on("launch_stock"))
    db.commit()
    parts = [f"Filial {br.code} criada: {stats['bays']} estantes com estrutura, {stats['levels']} níveis, "
             f"{stats['locations']} endereços e {stats['bins']} bins."]
    if stats["products_created"] or stats["products_updated"]:
        parts.append(f"Produtos: {stats['products_created']} cadastrado(s)"
                     + (f" ({stats['unplaced']} fora do plano, sem endereço)" if stats["unplaced"] else "")
                     + f", {stats['products_updated']} atualizado(s).")
    if stats["assigned"]:
        parts.append(f"{stats['assigned']} atribuição(ões) produto → endereço"
                     + (f", {stats['defaults']} endereço(s) padrão definido(s)" if stats["defaults"] else "")
                     + (f", {stats['stock_units']:g} unidade(s) de saldo lançadas" if stats["stock_units"] else "") + ".")
    if stats["skus_without_product"]:
        parts.append(f"{stats['skus_without_product']} SKU(s) sem cadastro de produto ficaram sem atribuição.")
    return redirect(f"/layout?branch_id={br.id}", " ".join(parts), "warning" if stats["skus_without_product"] else "success")


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
    from services.file_reader import save_upload

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", "Projeto não encontrado.", "danger")
    try:
        token = await save_upload(file)
    except ValueError as exc:
        return redirect(f"/optimization/site/{pid}", str(exc), "danger")
    return _mapping_page(request, pr, token, file.filename or "")


def _mapping_page(request: Request, pr: SiteProject, token: str, filename: str):
    """Tela de mapeamento de abas/colunas de um arquivo já salvo em uploads (token)."""
    from services.file_reader import UPLOAD_DIR, remove_upload
    from services.site_import import FIELD_LABELS, KIND_LABELS, REQUIRED, auto_mapping, guess_columns, read_sheets

    try:
        preview = read_sheets(UPLOAD_DIR / token, limit=6)
    except Exception as exc:  # arquivo corrompido / formato inesperado
        remove_upload(token)
        return redirect(f"/optimization/site/{pr.id}", f"Não foi possível ler o arquivo: {exc}", "danger")
    if not preview or not any(preview.values()):
        remove_upload(token)
        return redirect(f"/optimization/site/{pr.id}", "O arquivo não tem linhas.", "danger")
    remembered = ((pr.config or {}).get("import_mapping") or {})
    sheets = {name: {"headers": rows[0] if rows else [], "rows": rows[1:6]} for name, rows in preview.items()}
    # mapeamento automático de cada tipo para CADA aba (a tela reaplica ao trocar a aba)
    guesses = {kind: {name: guess_columns(kind, sh["headers"], remembered.get(kind)) for name, sh in sheets.items()}
               for kind in KIND_LABELS}
    return render(request, "site/mapping.html", project=pr, token=token, filename=filename,
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
