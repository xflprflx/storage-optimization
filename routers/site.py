"""Projeto de filial nova: cadastro do imóvel e das estruturas candidatas, otimização do layout e da quantidade de
estruturas/bins, exportação e geração do cadastro da filial."""
from __future__ import annotations

import copy
from typing import Optional
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import BinType, Branch, Product, SiteProject, StructureType, Warehouse
from routers.common import form_data, redirect, render
from services.addressing import normalize_code
from services.bin_optimizer import BASES, OBJECTIVES
from services.optimization_service import load_types
from services.site_planner import (ADDRESS_MODES, CAP_ALIGN, ORIENTATIONS, SECTOR_PLACES, SITE_CRITERIA, SITE_OBJECTIVES, STRUCT_MODES, ZONE_ORIENTATIONS,
                                   floor_svg, struct_colors, dim_mode, dim_pos)
from services.site_service import (BUILDING_FIELDS, DEFAULT_LOCATION_MODES, SLOT_METRICS, SLOT_SOURCES,
                                   config_from_form, create_branch, merged, run_project)
from services.i18n import N_, _, currency, tr

router = APIRouter(prefix="/optimization/site", tags=[N_("Projeto de filial nova")])


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
    # idioma/unidades também: o plano guarda textos prontos (critérios, avisos, rótulos)
    from services import i18n

    return json.dumps([cfg, [tuple(map(str, m)) for m in marks], i18n.get_lang(), i18n.unit_system()], sort_keys=True, default=str)


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
    pr = SiteProject(name=name[:150] or _("Nova filial"), config=cfg)
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
    pr = _new_project(db, name or (Path(upload.filename).stem if has_file else _("Nova filial")))
    if token:
        return _mapping_page(request, pr, token, upload.filename)
    return redirect(f"/optimization/site/{pr.id}", _("Projeto criado. Informe as dimensões do imóvel e escolha as estruturas."))


@router.get("/examples/{slug}.xlsx")
def download_example(slug: str):
    path = _example_file(slug)
    if path is None:
        return redirect("/optimization/site", _("Exemplo não encontrado."), "danger")
    return Response(path.read_bytes(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="exemplo_{slug}.xlsx"'})


@router.get("/seed.xlsx")
def download_seed():
    """Compatibilidade: planilha do exemplo padrão."""
    from services.site_examples import DEFAULT

    return download_example(DEFAULT)


def _save_site_run(db: Session, pr: SiteProject, cfg: dict, plan) -> Optional[int]:
    """Guarda o plano do projeto (indicadores, estruturas, bins e SKUs) para consulta e comparação."""
    from services.run_history import save_run

    try:
        k = plan.slotting or {}
        num = lambda label, v, unit="", places=0, better=None: {"label": label, "value": v, "unit": unit,  # noqa: E731
                                                               "places": places, "better": better}
        kpis = [num(_("Módulos de estrutura"), plan.total_modules, "", 0, "low"),
                num(_("Custo total"), plan.total_cost, " " + currency(), 2, "low"),
                num(_("Custo das estruturas"), plan.structures_cost, " " + currency(), 2, "low"),
                num(_("Custo das bins"), plan.bins_cost, " " + currency(), 2, "low"),
                num(_("Bins alocadas"), plan.bins_placed, "", 0, "high"),
                num(_("Bins sem lugar"), plan.shortage, "", 0, "low"),
                num(_("Ocupação dos níveis"), plan.fill, "%", 1, "high"),
                num(_("Ocupação das bins"), plan.bin_fill, "%", 1, "high"),
                num(_("Uso das fileiras"), plan.row_use, "%", 1, "high"),
                num(_("Área das estruturas"), plan.footprint_m2, " m²", 1, "low"),
                num(_("Caminhada média por saída"), k.get("avg_dist"), " m", 1, "low"),
                num(_("Saídas na zona de ouro da pega"), k.get("golden_picks"), "%", 1, "high"),
                num(_("Saídas na zona de ouro da caminhada"), k.get("walk_picks"), "%", 1, "high"),
                num(_("Distância entre itens que saem juntos"), k.get("pairs_avg"), " m", 1, "low")]
        kpis = [x for x in kpis if x["value"] is not None]
        b = plan.building
        details = {
            "structs": [{N_("Estrutura"): s.struct.code, "Qtd.": s.qty, N_("Níveis"): s.levels, N_("Custo"): s.cost} for s in plan.structs if s.qty],
            "types": [{N_("Tipo de bin"): t.spec.code, N_("Necessárias"): t.needed, N_("Alocadas"): t.placed,
                       N_("Ocupação das bins (%)"): t.bin_fill, N_("Custo"): t.cost} for t in plan.types if t.needed],
            "rows": [{"SKU": d.sku, N_("Produto"): d.name, N_("Quantidade"): d.units, N_("Bins"): d.total_bins, N_("Situação"): d.status_label,
                      "ABC": d.abc, N_("Saída"): d.pop} for d in plan.demands]}
        params = {"projeto": pr.name, N_("imóvel"): f"{b.length_m:g} × {b.width_m:g} × {b.clear_height_m:g} m",
                  "building": cfg.get("building"), "structures": cfg.get("structures"), "demand": cfg.get("demand"),
                  "params": cfg.get("params"), "slotting": cfg.get("slotting")}
        sv = plan.solver or {}
        solver_txt = (sv.get("status") or "") + (_(" · sem limite de tempo") if sv.get("unlimited") else "") \
            + (f" · {sv.get('time', 0):.1f} s" if sv.get("time") is not None else "")
        run = save_run(db, "site", f"Projeto {pr.name} · {b.length_m:g} × {b.width_m:g} m", params,
                       {"kpis": kpis, "notes": list(plan.notes)[:20], "solver": solver_txt}, details,
                       f"/optimization/site/{pr.id}?run=1",
                       fingerprint=_cfg_key(db, cfg), project_id=pr.id)
        return run.id
    except Exception:  # noqa: BLE001
        import logging

        db.rollback()
        logging.getLogger("neokioski").exception(_("Não foi possível guardar o resultado do projeto"))
        return None


def _context(db: Session, pr: SiteProject, cfg: dict) -> dict:
    structs = list(db.scalars(select(StructureType).order_by(StructureType.code)))
    for st in structs:  # estrutura cadastrada depois do projeto: entra desligada
        cfg["structures"].setdefault(str(st.id), {"mode": "off", "qty": "", "min_qty": "", "max_qty": ""})
    return dict(
        project=pr, cfg=cfg, structures=structs, bin_types=list(db.scalars(select(BinType).order_by(BinType.code))),
        branches=list(db.scalars(select(Branch).order_by(Branch.code))),
        warehouses=list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
        BUILDING_FIELDS=BUILDING_FIELDS, ORIENTATIONS=ORIENTATIONS, SITE_OBJECTIVES=SITE_OBJECTIVES, SITE_CRITERIA=SITE_CRITERIA,
        ZONE_ORIENTATIONS=ZONE_ORIENTATIONS, CAP_ALIGN=CAP_ALIGN, ADDRESS_MODES=ADDRESS_MODES, SECTOR_PLACES=SECTOR_PLACES,
        DEFAULT_LOCATION_MODES=DEFAULT_LOCATION_MODES,
        STRUCT_MODES=STRUCT_MODES, BASES=BASES, OBJECTIVES=OBJECTIVES,
        SLOT_SOURCES=SLOT_SOURCES, SLOT_METRICS=SLOT_METRICS,
        sl=cfg.get("slotting") or {}, orders=cfg.get("orders") or {},
        out_count=sum(1 for it in cfg.get("items") or [] if it.get("out")),
    )


@router.get("/{pid}")
def show_project(pid: int, request: Request, run: str = "", db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    plan, errors, _x = _plan_for(db, pr, fresh=True) if run else (None, [], None)
    cfg = merged(copy.deepcopy(pr.config))
    run_id = _save_site_run(db, pr, cfg, plan) if plan is not None and not errors and plan.modules else None
    ctx = _context(db, pr, cfg)
    types = {t.id: t for t in ctx["bin_types"]}
    return render(request, "site/project.html", **ctx, plan=plan, errors=errors, types=types, run_id=run_id,
                  svg=floor_svg(plan) if plan else "", colors=struct_colors(plan) if plan else {})


@router.post("/{pid}")
def save_project(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    pr.name = (form.get("name") or "").strip()[:150] or pr.name
    cfg = config_from_form(form)
    cfg["items"] = list((pr.config or {}).get("items") or [])  # SKUs do arquivo não vêm no formulário
    cfg["import_mapping"] = dict((pr.config or {}).get("import_mapping") or {})
    if form.get("clear_orders") != "1":  # o histórico de pedidos vem da planilha, não do formulário
        cfg["orders"] = dict((pr.config or {}).get("orders") or {})
    old_caps = ((pr.config or {}).get("demand") or {}).get("caps") or {}
    if old_caps and form.get("clear_caps") != "1":  # limites por SKU não vêm no formulário
        cfg["demand"]["caps"] = dict(old_caps)
    pr.config = cfg
    db.commit()
    if form.get("action") == "run":
        return redirect(f"/optimization/site/{pid}?run=1#resultado")
    return redirect(f"/optimization/site/{pid}", _("Projeto salvo."))


@router.post("/{pid}/layout.json")
def layout_json(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Planta (zonas, corredores e fileiras disponíveis) calculada a partir do formulário, sem salvar: usada pelo
    desenho da planta nas etapas do projeto."""
    from fastapi.responses import JSONResponse

    from services.site_planner import build_layout
    from services.site_service import parse_config, structure_specs

    if not _get(db, pid):
        return JSONResponse({"error": _("Projeto não encontrado.")}, status_code=404)
    cfg = config_from_form(form)
    b, modes, _prm, errors = parse_config(cfg)
    if not b.length_m or not b.width_m:
        return JSONResponse({"error": _("Informe comprimento e largura do imóvel."), "shapes": []})
    active = [s for s in structure_specs(db, modes) if s.mode != "off" and s.height <= b.clear_height_m * 100 + 1e-6]
    lay = build_layout(b, active)
    rnd = lambda v: round(v, 1)  # noqa: E731
    return JSONResponse({"errors": errors, "notes": lay.notes, "rows": len(lay.rows), "aisles": lay.real_aisles,
                         "row_m": round(lay.total_row_m, 1), "shapes": [[k, rnd(x), rnd(y), rnd(w), rnd(d), lb]
                                                                         for k, x, y, w, d, lb in lay.shapes()]})


# --------------------------------------------------------------------------- #
# Buscas de fator (redução e crescimento): rodam em segundo plano, com progresso e cancelamento
# --------------------------------------------------------------------------- #
_JOBS: dict[str, dict] = {}  # token -> {pid, kind, step, total, label, done, error, res, cancel, t0}
_JOB_KINDS = {"reduction": (N_("Sugestão de redução da demanda"), "bi-funnel", "site/suggest.html"),
              "growth": (N_("Quanto posso crescer?"), "bi-graph-up-arrow", "site/growth.html")}


def _start_job(pid: int, kind: str, cfg: dict) -> str:
    import threading
    import time
    import uuid

    from database import SessionLocal
    from services.site_service import Cancelled, suggest_growth, suggest_reduction

    for k in [k for k, j in _JOBS.items() if time.time() - j["t0"] > 3600]:  # limpa o que ficou parado há mais de 1 h
        _JOBS.pop(k, None)
    token = uuid.uuid4().hex
    job = {"pid": pid, "kind": kind, "step": 0, "total": 0, "label": _("Começando"), "done": False, "error": "", "res": None,
           "cancel": False, "t0": time.time(), "t1": None}
    _JOBS[token] = job

    def progress(step, total, label):
        job.update(step=step, total=total, label=label)

    def work():
        try:
            with SessionLocal() as db:
                fn = suggest_growth if kind == "growth" else suggest_reduction
                job["res"] = fn(db, cfg, progress=progress, cancelled=lambda: job["cancel"])
        except Cancelled:
            job["error"] = _("Busca cancelada.")
        except Exception as exc:  # noqa: BLE001  (o erro volta para a tela de espera)
            job["error"] = _("Não foi possível concluir a busca: {exc}", exc=exc)
        finally:
            job["done"], job["t1"] = True, time.time()

    import contextvars

    # a busca roda noutra thread: leva junto o idioma e as unidades da requisição (mensagens de progresso e de erro)
    threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()
    return token


def _job(pid: int, token: str):
    j = _JOBS.get(token)
    return j if j and j["pid"] == pid else None


def _wait_page(request: Request, pid: int, kind: str, token: str, pr):
    title, icon, _tpl = _JOB_KINDS[kind]
    return render(request, "site/running.html", project=pr, token=token, kind=kind, title=title, icon=icon)


@router.post("/{pid}/demand-suggestion")
def demand_suggestion(pid: int, request: Request, db: Session = Depends(get_db)):
    """Sugestão de redução da demanda: fator que cabe, limites por SKU e maiores consumidores (em segundo plano)."""
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    token = _start_job(pid, "reduction", merged(copy.deepcopy(pr.config)))
    return _wait_page(request, pid, "reduction", token, pr)


@router.post("/{pid}/growth-suggestion")
def growth_suggestion(pid: int, request: Request, db: Session = Depends(get_db)):
    """Folga de crescimento: até quanto a demanda cresce com as estruturas e o imóvel atuais (em segundo plano)."""
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    token = _start_job(pid, "growth", merged(copy.deepcopy(pr.config)))
    return _wait_page(request, pid, "growth", token, pr)


@router.get("/{pid}/job/{token}/status")
def job_status(pid: int, token: str):
    import time
    from fastapi.responses import JSONResponse

    j = _job(pid, token)
    if j is None:
        return JSONResponse({"error": _("A busca expirou ou o servidor foi reiniciado: rode de novo.")}, status_code=404)
    end = j["t1"] or time.time()
    return {"step": j["step"], "total": j["total"], "label": j["label"], "done": j["done"], "error": j["error"],
            "elapsed": round(end - j["t0"], 1), "cancelling": j["cancel"] and not j["done"]}


@router.post("/{pid}/job/{token}/cancel")
def job_cancel(pid: int, token: str):
    j = _job(pid, token)
    if j is not None and not j["done"]:
        j["cancel"] = True
    return {"ok": j is not None}


@router.get("/{pid}/job/{token}/result")
def job_result(pid: int, token: str, request: Request, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    j = _job(pid, token)
    if not pr or j is None or not j["done"]:
        return redirect(f"/optimization/site/{pid}", _("A busca expirou ou ainda não terminou: rode de novo."), "warning")
    res = j["res"]
    if j["error"] or not res or res.get("error"):
        return redirect(f"/optimization/site/{pid}", j["error"] or (res or {}).get("error") or _("Busca sem resultado."), "danger")
    _title, _icon, tpl = _JOB_KINDS[j["kind"]]
    return render(request, tpl, project=pr, res=res)


@router.post("/{pid}/demand-apply")
def demand_apply(pid: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    cfg = merged(copy.deepcopy(pr.config))
    action = form.get("action")
    if action == "factor":
        val = str(form.get("factor") or "").strip()
        cfg["demand"]["factor"] = val
        msg = _("Fator da demanda ajustado para {val}%.", val=val)
    elif action == "caps":
        skus = form.getlist("cap_sku") if hasattr(form, "getlist") else []
        units = form.getlist("cap_units") if hasattr(form, "getlist") else []
        caps = dict(cfg["demand"].get("caps") or {})
        for s_, u in zip(skus, units):
            if str(u).strip().isdigit():
                caps[s_] = int(u)
        cfg["demand"]["caps"] = caps
        msg = _("Limite de quantidade aplicado a {len} SKU(s) (total com limite: {len2}).", len=len(skus), len2=len(caps))
    elif action == "clear":
        cfg["demand"].pop("caps", None)
        msg = _("Limites por SKU removidos.")
    else:
        return redirect(f"/optimization/site/{pid}", _("Ação inválida."), "danger")
    pr.config = cfg
    db.commit()
    return redirect(f"/optimization/site/{pid}?run=1", msg)


@router.post("/{pid}/duplicate")
def duplicate_project(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    cp = SiteProject(name=_("{name} (cópia)", name=pr.name)[:150], config=copy.deepcopy(pr.config))
    db.add(cp)
    db.commit()
    return redirect(f"/optimization/site/{cp.id}", _("Projeto duplicado: compare cenários alterando a cópia."))


@router.post("/{pid}/delete")
def delete_project(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if pr:
        db.delete(pr)
        db.commit()
    return redirect("/optimization/site", _("Projeto excluído."))


def _report(db: Session, pr: SiteProject):
    """Plano detalhado (o mesmo plano aprovado na tela). Retorna (relatório, erro)."""
    from services.site_report import build_report

    plan, errors, cfg = _plan_for(db, pr)
    if errors or plan is None:
        return None, " ".join(errors) or _("Não foi possível gerar o plano.")
    names = {bt.id: bt.name for bt in db.scalars(select(BinType))}
    return build_report(plan, pr.name, cfg, load_types(db), names), None


def _report_response(request: Request, pid: int, fmt: str, db: Session):
    from services.site_report import to_pdf as report_pdf
    from services.site_report import to_xlsx as report_xlsx

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    rep, err = _report(db, pr)
    if err:
        return redirect(f"/optimization/site/{pid}", err, "danger")
    name = f"plano_filial_{pr.name[:60]}_{rep.created:%Y%m%d_%H%M}"
    if fmt == "xlsx":
        return Response(report_xlsx(rep), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": _attachment(f"{name}.xlsx")})
    if fmt == "pdf":
        return Response(report_pdf(rep, dim_mode(request.query_params.get("dims")), dim_pos(request.query_params.get("pos"))), media_type="application/pdf",
                        headers={"Content-Disposition": _attachment(f"{name}.pdf")})
    return render(request, "site/plan.html", project=pr, rep=rep, plan=rep.plan, svg=floor_svg(rep.plan),
                  colors=struct_colors(rep.plan))


@router.get("/{pid}/3d")
def view_3d(pid: int, request: Request, db: Session = Depends(get_db)):
    """Arranjo 3D da filial (o plano exibido na tela; os dados vêm de plan3d.json)."""
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    v3 = {"name": pr.name, "back_url": f"/optimization/site/{pid}?run=1", "back_label": _("Voltar ao projeto"),
          "links": [(f"/optimization/site/{pid}/plan", "list-check", _("Plano detalhado")),
                    (f"/optimization/site/{pid}/routes", "signpost-2", _("Simulador de trajeto"))],
          "data_url": f"/optimization/site/{pid}/plan3d.json", "routes_url": f"/optimization/site/{pid}/routes3d.json",
          "fix_url": f"/optimization/site/{pid}", "fix_label": _("Abrir o projeto")}
    return render(request, "site/view3d.html", v3=v3)


@router.get("/{pid}/plan3d.json")
def plan_3d_json(pid: int, db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse

    from services.site_3d import plan_3d

    pr = _get(db, pid)
    if not pr:
        return JSONResponse({"error": _("Projeto não encontrado.")}, status_code=404)
    plan, errors, _cfg = _plan_for(db, pr)
    if errors or plan is None or not plan.modules:
        return JSONResponse({"error": " ".join(errors) or _("O plano não tem estruturas para mostrar. Rode a otimização.")})
    data = plan_3d(plan)
    data["building"]["name"] = pr.name
    return JSONResponse(data)


def _sku_report(db: Session, pr: SiteProject):
    """Relatório de SKUs do projeto: popularidade e afinidade (histórico de pedidos ou coluna Saída) e onde cada SKU
    ficou no plano exibido (endereço, caminhada e zonas de ouro)."""
    from services.slotting import sku_report

    cfg = merged(copy.deepcopy(pr.config))
    data = cfg.get("orders") or {}
    items = cfg.get("items") or []
    if not data.get("skus"):
        col = {it["sku"]: [it["out"], it["out"]] for it in items if it.get("out")}
        data = {"kind": "column", "skus": col, "orders": 0, "pairs": []} if col else {}
    names = {it["sku"]: it.get("name") or "" for it in items}
    if data.get("skus"):
        missing = [s_ for s_ in data["skus"] if not names.get(s_)]
        for i in range(0, len(missing), 900):
            for sku, name in db.execute(select(Product.sku, Product.name).where(Product.sku.in_(missing[i:i + 900]))):
                names[sku] = name
    where: dict[str, list] = {}
    hit = _PLAN_CACHE.get(pr.id)
    if hit and hit[0] == _cfg_key(db, cfg):  # só o plano já exibido (não roda a otimização de novo)
        for p in hit[1].positions:
            where.setdefault(p.sku, []).append({"address": p.address, "dist": p.dist_m, "golden": p.golden,
                                                "walk": p.walk_golden})
    metric = (cfg.get("slotting") or {}).get("metric") or "lines"
    return sku_report(data, names, where, metric)


@router.get("/{pid}/skus")
def sku_report_page(pid: int, request: Request, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    return render(request, "slotting/skus.html", rep=_sku_report(db, pr), title=_("Projeto {name}", name=pr.name),
                  back=f"/optimization/site/{pid}?run=1#resultado", xlsx=f"/optimization/site/{pid}/skus.xlsx")


def _route_setup(db: Session, pr: SiteProject):
    """Amostra de pedidos + plano exibido → (sample, plan, nodes, start, ref, erro)."""
    from services.route_sim import plan_nodes, ref_node

    cfg = merged(copy.deepcopy(pr.config))
    sample = (cfg.get("orders") or {}).get("sample")
    if not sample:
        return None, None, None, None, None, (
            _("O simulador usa os pedidos da aba Pedidos (uma amostra é guardada na importação). Importe a planilha com a "
            "aba Pedidos — se ela já foi importada antes desta versão, importe de novo."))
    plan, errors, _cfg = _plan_for(db, pr)
    if errors or plan is None or not plan.modules:
        return None, None, None, None, None, " ".join(errors) or _("Rode a otimização primeiro.")
    from services.walk_net import plan_net

    nodes, ref = plan_nodes(plan)
    start = ref_node(ref)
    plan_net(plan, nodes, start)  # trajeto pelo piso livre: contorna estruturas e obstáculos
    return sample, plan, nodes, start, ref, ""


@router.get("/{pid}/routes")
def route_simulator(pid: int, request: Request, order: str = "", db: Session = Depends(get_db)):
    """Simulador de trajeto: a amostra de pedidos percorrida no plano (lista de separação × rota otimizada)."""
    from services.route_sim import path_points, simulate

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    ctx = dict(title=_("Projeto {name}", name=pr.name), back=f"/optimization/site/{pid}?run=1#resultado", svg="", rows=[], sel=None,
               link=f"/optimization/site/{pid}/routes?", ref_label="", how="", walk3d=f"/optimization/site/{pid}/3d?walk=1&",
               dist_note=_("Distância pelo menor caminho no piso livre: pelos corredores, saindo pela ponta ou por um transversal "
                         "para trocar de corredor, contornando estruturas e obstáculos."))
    sample, plan, nodes, start, ref, error = _route_setup(db, pr)
    if error:
        return render(request, "slotting/routes.html", **ctx, sim={}, error=error)
    from services.route_sim import add_times, day_rows, time_params

    rows_first = add_times(simulate(sample, nodes, start), time_params(request.query_params))
    day = request.query_params.get("day", "")
    rows = day_rows(sorted(rows_first["rows"], key=lambda r: -r["list_m"]), day)
    chosen = order or (rows[0]["order"] if rows else "")
    sim = simulate([o for o in sample if o[0] == chosen], nodes, start, keep_routes={chosen}) if chosen else {}
    svg, sel = "", next((r for r in rows if r["order"] == chosen), None)
    if sel and sim.get("routes", {}).get(chosen):
        base = floor_svg(plan)
        sc = 1000 / plan.floor.length_cm
        overlay = []
        for kind, color, dash in (("list", "#d62728", ""), ("s", "#2f6db5", "2 3"), ("opt", "#1a9850", "6 4")):
            pts = path_points(sim["routes"][chosen][kind], start)
            overlay.append(f'<polyline points="{" ".join(f"{x * sc:.1f},{y * sc:.1f}" for x, y in pts)}" fill="none" '
                           f'stroke="{color}" stroke-width="{3 if kind == "list" else 2.5}" stroke-linejoin="round" '
                           f'opacity=".85"' + (f' stroke-dasharray="{dash}"' if dash else "") + '/>')
        for i, n in enumerate(sim["routes"][chosen]["list"], start=1):
            overlay.append(f'<circle cx="{n.gx * sc:.1f}" cy="{n.gy * sc:.1f}" r="7" fill="#d62728" stroke="#fff"/>'
                           f'<text x="{n.gx * sc:.1f}" y="{n.gy * sc + 3.5:.1f}" font-size="9" fill="#fff" '
                           f'text-anchor="middle" font-weight="bold">{i}<title>{n.key}</title></text>')
        overlay.append(f'<circle cx="{start.gx * sc:.1f}" cy="{start.gy * sc:.1f}" r="8" fill="#111" stroke="#fff" stroke-width="2">'
                       f'<title>{_("Saída e volta: {label}", label=tr(ref["label"]))}</title></circle>')
        svg = base.replace("</svg>", "".join(overlay) + "</svg>")
    how = (_("ruas em serpentina") if plan.building.serpentine else _("ruas na mesma direção")) \
        if plan.building.address_mode == "row" else _("uma letra por corredor (ímpares/pares)")
    keep = [(k, v) for k, v in request.query_params.multi_items() if not k.startswith("t_")]
    tq = "&".join(f"{k}={v}" for k, v in rows_first.get("times", {}).get("params", {}).items())
    ctx["link"] = f"/optimization/site/{pid}/routes?{tq}&" + (f"day={day}&" if day else "")
    ctx["day_link"] = f"/optimization/site/{pid}/routes?{tq}&"
    return render(request, "slotting/routes.html", **{**ctx, "svg": svg, "rows": rows[:500], "sel": sel,
                                                      "ref_label": ref["label"], "how": how, "keep": keep},
                  sim=rows_first, error="")


@router.get("/{pid}/routes3d.json")
def routes_3d_json(pid: int, order: str = "", day: str = "", db: Session = Depends(get_db)):
    """Dados da simulação de caminhada em 3D. Sem `order`: a lista de pedidos da amostra (com as três distâncias) e
    os totais por dia. Com `day`: o dia inteiro (pedidos em sequência). Com `order`: as três rotas daquele pedido (pontos em cm na planta, índice de cada parada, endereços e SKUs)."""
    from fastapi.responses import JSONResponse

    from services.route_sim import day_payload, order_payload, orders_payload

    pr = _get(db, pid)
    if not pr:
        return JSONResponse({"error": _("Projeto não encontrado.")}, status_code=404)
    sample, _plan, nodes, start, ref, error = _route_setup(db, pr)
    if error:
        return JSONResponse({"error": error})
    if day:  # o dia inteiro: os pedidos do dia um depois do outro
        return JSONResponse(day_payload(sample, nodes, start, ref, day))
    if not order:
        return JSONResponse(orders_payload(sample, nodes, start, ref))
    return JSONResponse(order_payload(sample, nodes, start, ref, order))


@router.get("/{pid}/skus.xlsx")
def sku_report_xlsx(pid: int, db: Session = Depends(get_db)):
    from services.slotting import sku_report_xlsx as to_xlsx

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    return Response(to_xlsx(_sku_report(db, pr), _("Projeto {name}", name=pr.name)),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": _attachment(f"skus_popularidade_{pr.name[:60]}.xlsx")})


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
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    back = f"/optimization/site/{pid}?run=1"
    code, name = normalize_code(form.get("branch_code")), (form.get("branch_name") or "").strip()[:150]
    wh_code, wh_name = normalize_code(form.get("wh_code")), (form.get("wh_name") or "").strip()[:150]
    if not code or len(code) > 30 or not name:
        return redirect(back, _("Informe código (até 30 caracteres) e nome da filial nova."), "danger")
    if db.scalar(select(Branch.id).where(Branch.code == code)):
        return redirect(back, _("Já existe uma filial com o código {code}.", code=code), "danger")
    plan, errors, cfg = _plan_for(db, pr)
    if errors or plan is None or plan.errors or not plan.modules:
        return redirect(back, _("O plano não é válido para gerar o cadastro: ") + " ".join(errors + (plan.errors if plan else [])),
                        "danger")
    on = lambda k: str(form.get(k) or "").lower() in {"1", "on", "true"}  # noqa: E731
    from services.site_service import DEFAULT_LOCATION_MODES

    mode = form.get("default_mode") if form.get("default_mode") in DEFAULT_LOCATION_MODES else "empty"
    items = cfg.get("items") if cfg["demand"].get("source") == "upload" else None
    br, stats = create_branch(db, plan, code, name, wh_code, wh_name, link_skus=on("link_skus"), items=items,
                              create_products=on("create_products"), update_products=on("update_products"),
                              default_mode=mode, launch_stock=on("launch_stock"))
    orders = cfg.get("orders") or {}
    if orders.get("skus"):  # histórico de pedidos do projeto vira o da filial (posição pela saída e simulador)
        from models import PickHistory

        db.add(PickHistory(branch_id=br.id, filename=f"projeto {pr.name}"[:200], data=orders))
        stats["history"] = True
    db.commit()
    parts = [_("Filial {code} criada: {bays} estantes com estrutura, {levels} níveis, {locations} endereços e {bins} bins.", code=br.code, bays=stats['bays'], levels=stats['levels'], locations=stats['locations'], bins=stats['bins'])]
    if stats["products_created"] or stats["products_updated"]:
        parts.append(_("Produtos: {products_created} cadastrado(s)", products_created=stats['products_created'])
                     + (_(" ({unplaced} fora do plano, sem endereço)", unplaced=stats['unplaced']) if stats["unplaced"] else "")
                     + f", {stats['products_updated']} atualizado(s).")
    if stats["assigned"]:
        parts.append(_("{assigned} atribuição(ões) produto → endereço", assigned=stats['assigned'])
                     + (_(", {defaults} endereço(s) padrão definido(s)", defaults=stats['defaults']) if stats["defaults"] else "")
                     + (_(", {stock_units} unidade(s) de saldo lançadas", stock_units=f"{stats['stock_units']:g}") if stats["stock_units"] else "") + ".")
    if stats.get("layout_rows"):
        parts.append(_("Planta da filial gerada a partir do plano ({layout_rows} fileiras)", layout_rows=stats['layout_rows'])
                     + (_(" e histórico de pedidos copiado do projeto") if stats.get("history") else "") + ".")
    if stats["skus_without_product"]:
        parts.append(_("{skus_without_product} SKU(s) sem cadastro de produto ficaram sem atribuição.", skus_without_product=stats['skus_without_product']))
    return redirect(f"/layout?branch_id={br.id}", " ".join(parts), "warning" if stats["skus_without_product"] else "success")


# --------------------------------------------------------------------------- #
# Importação dos dados da filial nova (planilha modelo)
# --------------------------------------------------------------------------- #
@router.get("/{pid}/template.xlsx")
def download_template(pid: int, db: Session = Depends(get_db)):
    from services.site_import import template_xlsx

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    data = template_xlsx(db, merged(copy.deepcopy(pr.config)))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="modelo_filial_nova_{pid}.xlsx"'})


@router.post("/{pid}/upload")
async def upload_data(pid: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """1ª etapa: guarda o arquivo e mostra o mapeamento de abas e colunas (pré-preenchido)."""
    from services.file_reader import save_upload

    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
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
        return redirect(f"/optimization/site/{pr.id}", _("Não foi possível ler o arquivo: {exc}", exc=exc), "danger")
    if not preview or not any(preview.values()):
        remove_upload(token)
        return redirect(f"/optimization/site/{pr.id}", _("O arquivo não tem linhas."), "danger")
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
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    try:
        path = token_path(token)
    except ValueError as exc:
        return redirect(f"/optimization/site/{pid}", str(exc), "danger")
    if form.get("action") == "cancel":
        remove_upload(token)
        return redirect(f"/optimization/site/{pid}", _("Importação cancelada."), "secondary")
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
        if kind == "orders":
            mapping[kind]["abs"] = form.get("orders_abs") == "1"
    try:
        cfg, msgs, errs = import_file(db, path, merged(copy.deepcopy(pr.config)), mapping)
    except Exception as exc:
        db.rollback()
        remove_upload(token)
        return redirect(f"/optimization/site/{pid}", _("Não foi possível importar: {exc}", exc=exc), "danger")
    remove_upload(token)
    pr.config = cfg
    db.commit()
    text = " ".join(msgs)
    if errs:
        text = (text + " Avisos: " if text else _("Nada importado. ")) + " ".join(errs[:12]) + (" …" if len(errs) > 12 else "")
    return redirect(f"/optimization/site/{pid}", text or _("Nada importado."), ("warning" if msgs else "danger") if errs else "success")


@router.post("/{pid}/clear-items")
def clear_items(pid: int, db: Session = Depends(get_db)):
    pr = _get(db, pid)
    if not pr:
        return redirect("/optimization/site", _("Projeto não encontrado."), "danger")
    cfg = merged(copy.deepcopy(pr.config))
    cfg["items"] = []
    cfg["demand"]["source"] = "catalog"
    pr.config = cfg
    db.commit()
    return redirect(f"/optimization/site/{pid}", _("SKUs do arquivo removidos; a demanda volta a vir do cadastro."))
