"""Otimização de bins: escolha do objetivo e das restrições, análise e aplicação das sugestões."""
from __future__ import annotations

import math
from typing import Mapping, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import BinType, Branch, StockItem, StorageLocation, Warehouse, branch_warehouse
from routers.common import form_data, redirect, render, to_int
from routers.locations import FILTER_KEYS, _apply_filters
from services.bin_optimizer import BASES, CRITERIA, OBJECTIVES, STATUS_LABELS, Params
from services.action_plan import build_address_plan, build_pool_plan
from services.file_reader import parse_decimal
from services.optimization_service import apply as apply_suggestions
from services.optimization_service import run
from services.pool_optimizer import POOL_OBJECTIVES, PoolParams
from routers.packing import Arranger, bin_card, section
from services.plan_export import to_pdf, to_xlsx
from services.pool_service import apply_pool, run_pool

router = APIRouter(prefix="/optimization", tags=["Otimização de bins"])

PER_PAGE = 200
ENGINE_KEYS = ("engine", "time_limit", "prio2", "prio3", "prio4", "bin_source", "orphans")
PARAM_KEYS = ENGINE_KEYS + ("focus", "staging", "bins_mode", "basis", "objective", "fill_limit", "growth", "geometry", "rotation", "weight", "mix", "single_type",
              "max_bins", "keep_space", "max_width", "max_height", "clear_empty", "levels", "other_levels", "uniform_levels", "types")
ENGINE_DEFAULTS = {"engine": "or", "time_limit": "15", "prio2": "", "prio3": "", "prio4": "", "bin_source": "free",
                   "orphans": "relocate"}
BIN_SOURCES = {"free": "Só bins livres (sem saldo)", "any": "Qualquer bin (inclusive com saldo de outros itens)"}
ORPHAN_MODES = {"relocate": "Realocar os itens desalojados", "leave": "Deixar fora de bin (órfãos)"}
DEFAULTS = {**ENGINE_DEFAULTS, "focus": "1", "staging": "ÁREA DE ESPERA", "bins_mode": "existing", "basis": "stock", "objective": "min_volume", "fill_limit": "85", "growth": "0", "geometry": "1", "rotation": "upright",
            "weight": "1", "mix": "", "single_type": "", "max_bins": "", "keep_space": "1", "max_width": "",
            "max_height": "", "clear_empty": "", "levels": "1", "other_levels": "", "uniform_levels": ""}
SORTS = {
    "address": lambda r: r.address,
    "status": lambda r: (list(STATUS_LABELS).index(r.status), r.address),
    "bins_delta": lambda r: (r.bins_delta, r.address),
    "volume_delta": lambda r: (r.volume_delta, r.address),
    "width_delta": lambda r: (r.width_delta, r.address),
    "fill_now": lambda r: (r.fill_now if r.fill_now is not None else -1, r.address),
    "fill_after": lambda r: (r.fill_after if r.fill_after is not None else -1, r.address),
}


def _getlist(src, key: str) -> list[str]:
    return src.getlist(key) if hasattr(src, "getlist") else ([src[key]] if key in src else [])


def parse_engine(raw: dict, errors: list) -> tuple[str, float, Optional[list]]:
    """Motor (heurística ou pesquisa operacional), tempo limite (s) e ordem de prioridade dos critérios."""
    engine = "or" if raw.get("engine") == "or" else "heuristic"
    try:
        tl = float(str(raw.get("time_limit") or "15").replace(",", "."))
    except ValueError:
        errors.append("Tempo limite: número inválido.")
        tl = 15.0
    if not (1 <= tl <= 600):
        errors.append("Tempo limite deve estar entre 1 e 600 segundos.")
        tl = min(max(tl, 1.0), 600.0)
    prio = [raw.get(k) for k in ("prio2", "prio3", "prio4") if raw.get(k) in CRITERIA]
    return engine, tl, (prio or None)


def parse_params(src: Mapping) -> tuple[Params, dict, list[str]]:
    """Lê objetivo e restrições de query string ou formulário. Retorna (Params, valores p/ tela, erros)."""
    submitted = "objective" in src  # formulário enviado: checkbox ausente = desligado
    raw = {k: (src.get(k, "") if submitted else DEFAULTS.get(k, "")) for k in PARAM_KEYS if k != "types"}
    raw["types"] = [t for t in _getlist(src, "types") if t]
    errors: list[str] = []

    def num(key: str, label: str):
        try:
            v = parse_decimal(raw[key])
            return float(v) if v is not None else None
        except ValueError:
            errors.append(f"{label}: número inválido.")
            return None

    fill = num("fill_limit", "Taxa máxima de enchimento")
    growth = num("growth", "Margem de crescimento")
    max_w = num("max_width", "Largura máxima")
    max_h = num("max_height", "Altura máxima")
    max_bins = to_int(raw["max_bins"])
    if raw["max_bins"] and max_bins is None:
        errors.append("Máximo de bins: número inteiro inválido.")
    on = lambda k: str(raw.get(k) or "").lower() in {"1", "on", "true"}  # noqa: E731
    prm = Params(
        objective=raw["objective"] if raw["objective"] in OBJECTIVES else "min_volume",
        allowed_types={int(t) for t in raw["types"] if str(t).isdigit()} or None,
        fill_limit=(fill if fill is not None else 85.0) / 100.0,
        growth=(growth or 0.0) / 100.0,
        use_geometry=on("geometry"),
        rotation="any" if raw["rotation"] == "any" else "upright",
        enforce_weight=on("weight"),
        mix_skus=on("mix"),
        single_type=on("single_type"),
        max_bins=max_bins,
        keep_space=on("keep_space"),
        max_width_cm=max_w or None,
        max_height_cm=max_h or None,
        clear_empty=on("clear_empty"),
        respect_levels=True,  # sempre: nunca sugerir mais bins do que o nível suporta
        allow_other_levels=on("other_levels"),
        mixed_level_types=not on("uniform_levels"),
        basis=raw["basis"] if raw["basis"] in BASES else "stock",
    )
    # bins existentes: o plano só usa as bins que já existem no escopo (trocam de endereço);
    # recurso infinito: pode sugerir bins novas (a capacidade dos níveis continua valendo)
    prm.limit_inventory = raw.get("bins_mode") != "unlimited"
    prm.engine, prm.time_limit, prm.priority = parse_engine(raw, errors)
    prm.bin_source = "any" if raw.get("bin_source") == "any" else "free"
    prm.orphans = "leave" if raw.get("orphans") == "leave" else "relocate"
    try:
        prm.validate()
    except ValueError as exc:
        errors.append(str(exc))
    return prm, raw, errors


def address_scope(db: Session, filters: dict, raw: dict) -> tuple[list[int], Optional[list[int]]]:
    """Endereços a otimizar e, com "foco nos SKUs filtrados" + bins existentes + filtro de SKU/material, os outros
    endereços do escopo (sem o filtro de SKU/material) que podem ceder bins (doadores)."""
    ids = _scope_ids(db, filters)
    on = str(raw.get("focus") or "").lower() in {"1", "on", "true"}
    sku, material = (filters.get("sku") or "").strip(), (filters.get("material") or "").strip()
    if not on or not (sku or material) or raw.get("bins_mode") == "unlimited":
        return ids, None
    mine = set(ids)
    return ids, [i for i in _scope_ids(db, {**filters, "sku": "", "material": ""}) if i not in mine]


def _lookups(db: Session) -> dict:
    wh_branches: dict[int, list[int]] = {}
    for b, w in db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)).all():
        wh_branches.setdefault(w, []).append(b)
    return {
        "branches": list(db.scalars(select(Branch).order_by(Branch.code))),
        "warehouses": list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
        "wh_branches": wh_branches,
        "bin_types": list(db.scalars(select(BinType).order_by(BinType.code))),
    }


def _scope_ids(db: Session, filters: dict) -> list[int]:
    stmt = _apply_filters(select(StorageLocation.id), filters).order_by(StorageLocation.id)
    return list(db.scalars(stmt))


STATUS_CLS = {"melhoria": "primary", "corrigir": "danger", "liberar": "info", "ok": "success",
              "inviavel": "warning", "sem_dados": "secondary", "sem_saldo": "light", "realocar": "warning",
              "orfao": "dark"}
POOL_STATUS = {"mover": ("Mover", "primary"), "sem_bin": ("Sem bin", "danger"), "dimensionado": ("Dimensionado", "info"),
               "realocado": ("Desalojado e realocado", "warning"), "manter": ("Fica onde está", "success")}


def kpi(label: str, before, after, unit: str = "", places: int = 0, good_negative: bool = True, pct: bool = False,
        hint: str = "") -> dict:
    """Cartão de indicador: antes → depois (com a diferença)."""
    d = None if before is None or after is None else after - before
    return {"label": label, "before": before, "after": after, "unit": unit, "places": places, "delta": d,
            "good_negative": good_negative, "pct": pct, "hint": hint}


CRIT_LABELS = {"displaced": "Unidades de outros itens desalojadas", "short": "Violações / falta de bins", "bins": "Bins", "volume": "Volume de bins (L)",
               "width": "Largura de frente (cm)", "changes": "Mudanças / unidades movimentadas", "levels": "Bins fora do próprio nível",
               "cost": "Custo de bins (R$)"}


def _solver_view(info: Optional[dict], objective: str, priority, drop_changes: bool = False) -> Optional[dict]:
    if not info:
        return None
    from services.bin_optimizer import priority_list

    order = priority_list(objective, priority)
    if drop_changes:
        order = [c for c in order if c != "changes"]
    return {**info, "order": order}


def _common_ctx(db: Session, mode: str, filters: dict, raw: dict, errors: list, objectives: dict, unlimited: bool) -> dict:
    return {"mode": mode, "criteria": CRITERIA, "crit_labels": CRIT_LABELS, "solvers": [], "bin_sources": BIN_SOURCES,
            "orphan_modes": ORPHAN_MODES, "orphans": [], "displaced": 0, "filters": filters, "params": raw, "errors": errors, "objectives": objectives, "bases": BASES,
            "bins_modes": BINS_MODES, "unlimited": unlimited, "results": None,
            "base_url": "/optimization" if mode == "address" else "/optimization/pool", **_lookups(db)}


@router.get("")
def optimization_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    filters = {k: qp.get(k, "") for k in FILTER_KEYS}
    prm, raw, errors = parse_params(qp)
    unlimited = raw.get("bins_mode") == "unlimited"
    ctx = _common_ctx(db, "address", filters, raw, errors, OBJECTIVES, unlimited)
    if qp.get("run") and not errors:
        if not filters.get("branch_id"):
            ctx["errors"] = ["Selecione ao menos a filial do escopo."]
        else:
            try:
                ids, donors = address_scope(db, filters, raw)
                results, summary, types = run(db, ids, prm, donors)
            except ValueError as exc:
                ctx["errors"] = [str(exc)]
            else:
                status_f = qp.get("status", "")
                shown = [r for r in results if not status_f or r.status == status_f]
                sort = qp.get("sort", "status")
                desc = qp.get("dir", "asc") == "desc"
                shown.sort(key=SORTS.get(sort, SORTS["status"]), reverse=desc)
                page = max(1, to_int(qp.get("page"), 1))
                pages = max(1, math.ceil(len(shown) / PER_PAGE))
                page = min(page, pages)
                page_rows = shown[(page - 1) * PER_PAGE : page * PER_PAGE]
                from services.bin_content_service import arrangement_rows

                arranger = Arranger(db, prm.rotation)
                arr_views: dict = {}
                code = lambda t: types[t].code if t in types else "?"  # noqa: E731
                for lid, ba in arrangement_rows(db, page_rows, prm).items():
                    tag = lambda b: f"{code(b.type_id)} #{b.seq}" + (f" ↳{b.level}" if b.level else "")  # noqa: E731
                    case = case_link(qp, "address", lid)
                    v = {}
                    for side in ("before", "after"):
                        if ba[side] is None:
                            v[side] = None
                            continue
                        v[side] = []
                        for i, (b, items) in enumerate(ba[side]):
                            pk = arranger.view(b.type_id, items, tag(b), seq=b.seq) if items else None
                            if pk:
                                pk["url"] = f"{case}#b-{side if side == 'before' else 'after-' + prm.basis}-{i}"
                            label = tag(b) + " · " + ("+".join(f"{float(q):g}" for _s, q in items) if items else "vazia")
                            v[side].append((label, items, pk))
                    v["case"] = case
                    arr_views[lid] = v
                # indicadores (mesmos cartões do pool)
                changed = [r for r in results if r.applicable and r.status != "ok"]
                changed_ids = {r.id for r in changed}
                addr_before = sum(1 for r in results if sum(r.current.values()) > 0)
                addr_after = sum(1 for r in results if sum(((r.suggested if r.id in changed_ids else r.current) or {}).values()) > 0)
                kpis = [kpi("Bins ocupadas", summary.bins_before, summary.bins_after, hint="bins dos endereços do escopo"),
                        kpi("Volume de bins", summary.volume_before, summary.volume_after, " L"),
                        kpi("Largura de frente", summary.width_before, summary.width_after, " cm"),
                        kpi("Enchimento médio", summary.fill_before, summary.fill_after, "%", good_negative=False, pct=True),
                        kpi("Endereços com bins", addr_before, addr_after),
                        {"label": "Endereços a alterar", "value": len(changed), "hint": "trocas de bins"}]
                by_type: dict[int, dict] = {}
                for r in results:
                    for t, n in (r.current or {}).items():
                        by_type.setdefault(t, {"existing": 0, "needed": 0})["existing"] += n
                    for t, n in ((r.suggested if r.id in changed_ids else r.current) or {}).items():
                        by_type.setdefault(t, {"existing": 0, "needed": 0})["needed"] += n
                chips = [(k, label, STATUS_CLS[k], summary.by_status.get(k, 0)) for k, label in STATUS_LABELS.items()
                         if summary.by_status.get(k)]
                if unlimited:
                    extra = {t: v["needed"] - v["existing"] for t, v in by_type.items() if v["needed"] > v["existing"]}
                    if extra:
                        notes = ["Recurso infinito: o plano usa bins que não existem no escopo — "
                                 + ", ".join(f"{v} × {types[t].code if t in types else t}" for t, v in sorted(extra.items()))
                                 + " a mais (veja Bins por tipo)."]
                    else:
                        notes = ["Recurso infinito: mesmo sem limite de quantidade, o plano cabe nas bins que já existem no escopo."]
                else:
                    notes = []
                dinfo = summary.donors
                if dinfo:
                    unc = dinfo.get("uncovered") or {}
                    if unc:
                        notes.append("Faltaram bins nos outros endereços do escopo para o plano dos itens filtrados: "
                                     + ", ".join(f"{n} × {types[t].code if t in types else t}" for t, n in sorted(unc.items()))
                                     + ". O plano foi refeito com o que dá para liberar.")
                ctx.update(orphans=list(summary.orphans or []), addr_focus=dinfo)
                ctx.update(results=results, rows=page_rows, arr_views=arr_views, n_shown=len(shown), n_total=len(results),
                           page_no=page, pages=pages, types=types, sort=sort, sort_dir="desc" if desc else "asc",
                           status_filter=status_f, chips=chips, kpis=kpis, by_type=by_type, notes=notes,
                           chip_cls=STATUS_CLS, chip_lbl=STATUS_LABELS,
                           solvers=[_solver_view(summary.solver, prm.objective, prm.priority)],
                           n_applicable=sum(1 for r in results if r.applicable),
                           can_apply=True, filter_query=urlencode({k: v for k, v in filters.items() if v}))
    return render(request, "optimization/page.html", **ctx)


@router.post("/apply")
def apply_page(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    prm, raw, errors = parse_params(form)
    filters = {k: form.get(f"f_{k}", "") for k in FILTER_KEYS}
    query = {**{k: v for k, v in filters.items() if v},
             **{k: v for k, v in raw.items() if k != "types" and v not in ("", None)}, "run": "1"}
    back_qs = urlencode(query) + "".join(f"&types={t}" for t in raw["types"])
    back = f"/optimization?{back_qs}"
    if errors:
        return redirect(back, " ".join(errors), "danger")
    scope_ids, donors = address_scope(db, filters, raw) if filters.get("branch_id") else ([], None)
    only = None
    if form.get("scope") != "all":
        only = {i for i in (to_int(v) for v in form.getlist("ids")) if i}
        if not only:
            return redirect(back, "Nenhum endereço selecionado.", "warning")
        if not scope_ids:
            scope_ids = sorted(only)
    if not scope_ids:
        return redirect(back, "Nenhum endereço no escopo.", "warning")
    try:
        changed = apply_suggestions(db, scope_ids, prm, only, donors)
    except ValueError as exc:
        return redirect(back, str(exc), "danger")
    extra = (f" {changed - len(only)} endereço(s) foram junto por liberarem as bins recebidas (bins existentes)."
             if only is not None and changed > len(only) else "")
    return redirect(back, f"Sugestões aplicadas em {changed} endereço(s).{extra} A análise abaixo já reflete as novas bins.")


# --------------------------------------------------------------------------- #
# Otimização cega aos endereços (pool de bins)
# --------------------------------------------------------------------------- #
POOL_KEYS = ENGINE_KEYS + ("focus", "staging", "split", "bins_mode", "basis", "objective", "fill_limit", "growth", "geometry", "rotation", "weight", "mix",
             "single_type", "max_bins")
BINS_MODES = {"existing": "Bins existentes no escopo", "unlimited": "Bins como recurso infinito"}
POOL_DEFAULTS = {**ENGINE_DEFAULTS, "focus": "1", "staging": "ÁREA DE ESPERA", "split": "", "bins_mode": "existing", "basis": "stock", "objective": "min_bins", "fill_limit": "85", "growth": "0", "geometry": "1",
                 "rotation": "upright", "weight": "1", "mix": "", "single_type": "", "max_bins": ""}
POOL_PER_PAGE = 200


CASE_DROP = {"run", "page", "sort", "dir", "status", "show", "mode", "key", "sc", "scf"}
SCENARIO_LABELS = [("stock", "Saldo atual"), ("min", "Min level"), ("max", "Max level")]


def pool_scope(db: Session, filters: dict, raw: dict) -> tuple[list[int], Optional[set]]:
    """Endereços do pool e, com "foco nos SKUs filtrados", os produtos a otimizar.
    Com foco, o filtro de SKU/material escolhe só os PRODUTOS; as bins vêm do restante do escopo (filial, armazém,
    endereço, bins atuais). Sem foco (ou sem filtro de SKU/material), o filtro escolhe os endereços, como antes."""
    on = str(raw.get("focus") or "").lower() in {"1", "on", "true"}
    sku, material = (filters.get("sku") or "").strip(), (filters.get("material") or "").strip()
    if not on or not (sku or material):
        return _scope_ids(db, filters), None
    from models import Product
    from routers.common import sku_name_fields, text_search
    from services.material_service import material_condition

    stmt = text_search(select(Product.id), sku, Product.sku, Product.name, fields=sku_name_fields(Product))
    cond = material_condition(material)
    if cond is not None:
        stmt = stmt.where(cond)
    pids = set(db.scalars(stmt))
    if not pids:
        raise ValueError("Nenhum produto corresponde ao filtro de SKU / tipo de material.")
    return _scope_ids(db, {**filters, "sku": "", "material": ""}), pids


def case_link(qp, mode: str, key) -> str:
    """Link da tela do caso (endereço ou SKU) com os mesmos parâmetros da otimização."""
    q = [(k, v) for k, v in qp.multi_items() if k not in CASE_DROP] + [("mode", mode), ("key", str(key))]
    return "/optimization/case?" + urlencode(q)


def physical_map(results) -> dict:
    """{(bin id, SKU): unidades físicas depois do plano do pool}."""
    phys: dict = {}
    for r in results:
        for p in r.plans:
            for bid, q in p.physical_after.items():
                phys[(bid, p.sku)] = q
    return phys


def after_items(b, phys: dict) -> list:
    """Conteúdo planejado de uma bin do pool como [(SKU, físicas, reserva)]."""
    out = []
    for s_, n in b.contents.items():
        f = phys.get((b.id, s_), n)
        out.append((s_, f, max(n - f, 0)))
    return out


def _level_note(db: Session, skus, basis: str) -> str:
    """Aviso de SKUs sem o nível do cenário (vazio ou 0 = usa o saldo)."""
    from models import Product

    if basis == "stock" or not skus:
        return ""
    col = Product.min_level if basis == "min" else Product.max_level
    miss = [sku for sku, v in db.execute(select(Product.sku, col).where(Product.sku.in_(list(skus)))) if not v or v <= 0]
    if not miss:
        return ""
    label = "min level" if basis == "min" else "max level"
    return f"Sem {label} cadastrado (usou o saldo): " + ", ".join(sorted(miss)[:8]) + ("…" if len(miss) > 8 else "") + "."


@router.get("/case")
def case_page(request: Request, db: Session = Depends(get_db)):
    """Tela do caso (um endereço ou um SKU): ANTES e o DEPOIS em cada cenário escolhido (saldo, min, max level),
    todas as bins em 2D e 3D. Cada cenário recalcula a otimização com os mesmos parâmetros."""
    qp = request.query_params
    mode = "pool" if qp.get("mode") == "pool" else "address"
    key = qp.get("key", "")
    filters = {k: qp.get(k, "") for k in FILTER_KEYS}
    run_basis = qp.get("basis") if qp.get("basis") in BASES else "stock"
    codes = [c for c, _ in SCENARIO_LABELS]
    chosen = [c for c in codes if c in qp.getlist("sc")] if qp.get("scf") else codes
    hidden = [(k, v) for k, v in qp.multi_items() if k not in {"sc", "scf"}]
    scen = {"options": SCENARIO_LABELS, "chosen": chosen, "run_basis": run_basis, "hidden": hidden}
    icons = {"stock": "bi-box-seam", "min": "bi-arrow-down-circle", "max": "bi-arrow-up-circle"}
    sections, title, errors, rotation = [], "", [], "upright"
    try:
        if not filters.get("branch_id"):
            raise ValueError("Selecione ao menos a filial do escopo.")
        ids = _scope_ids(db, filters)
        focus = None
        if mode == "pool":
            prm, _b, raw, errs = parse_pool_params(qp)
            if errs:
                raise ValueError(" ".join(errs))
            ids, focus = pool_scope(db, filters, raw)
            rotation = prm.rotation
            arranger = Arranger(db, rotation)
            title = f"SKU {key}"
            before_done = False
            for c in (chosen or ["stock"]):
                results, locs, types, notes = run_pool(db, ids, prm, c, focus)
                where = lambda b: f"{locs[b.location_id][0]} · {b.tag}" if b.location_id in locs else f"bin nova · {b.tag}"  # noqa: E731
                plan = next((p for r in results for p in r.plans if p.sku == key), None)
                if not before_done:
                    before = list({b.id: b for b, _n in plan.orig_bins}.values()) if plan else []
                    sections.append(section("before", "Antes (hoje)", "bi-clock-history",
                                            [bin_card(arranger, b.type_id, where(b), list(b.orig.items()), b.seq) for b in before]))
                    before_done = True
                if c not in chosen:
                    break
                if plan is None:
                    sections.append(section(f"after-{c}", f"Depois — {dict(SCENARIO_LABELS)[c]}", icons[c], [],
                                            _level_note(db, [key], c), "SKU fora do plano neste cenário."))
                    continue
                phys = physical_map(results)
                after = list({b.id: b for b, _n in plan.allocations}.values())
                bins = [bin_card(arranger, b.type_id, where(b), after_items(b, phys), b.seq) for b in after]
                cur_total = sum(plan.current.values())
                note = f"planejado {plan.needed:g} un. · saldo hoje {cur_total:g} un."
                if plan.needed > cur_total + 1e-9:
                    note += (" · o planejado é maior que o saldo: o saldo é dividido entre as bins planejadas e o "
                             "restante fica reservado (tracejado)")
                if plan.shortage:
                    note += f" · sem bin: {plan.shortage} un."
                lv = _level_note(db, [key], c)
                sections.append(section(f"after-{c}", f"Depois — {dict(SCENARIO_LABELS)[c]}", icons[c], bins,
                                        note + (" · " + lv if lv else "")))
        else:
            from services.bin_content_service import address_scenario, arrangement_rows
            from services.optimization_service import load_addresses, load_types

            lid = to_int(key)
            prm, raw, errs = parse_params(qp)
            if errs:
                raise ValueError(" ".join(errs))
            rotation = prm.rotation
            arranger = Arranger(db, rotation)
            loc = db.get(StorageLocation, lid) if lid else None
            if loc is None:
                raise ValueError("Endereço não encontrado.")
            title = f"endereço {loc.address}"
            code = None
            ids, donors = address_scope(db, filters, raw)
            for c in (chosen or ["stock"]):
                prm.basis = c
                results, summary, types = run(db, ids, prm, donors)
                code = lambda t: types[t].code if t in types else "?"  # noqa: E731
                tag = lambda b: f"{code(b.type_id)} #{b.seq}" + (f" ↳{b.level}" if b.level else "")  # noqa: E731
                r = next((x for x in results if x.id == lid), None)
                if r is None:
                    raise ValueError("Endereço fora do escopo da otimização.")
                if not sections:
                    ba = arrangement_rows(db, [r], prm)[lid]
                    sections.append(section("before", "Antes (hoje)", "bi-clock-history",
                                            [bin_card(arranger, b.type_id, tag(b), items, b.seq) for b, items in ba["before"]]))
                if c not in chosen:
                    break
                addr, _ = load_addresses(db, [lid], c, load_types(db))
                planned = {i.sku: i.qty for a in addr for i in a.items}
                after = address_scenario(db, r, planned, prm)
                bins = None if after is None else [bin_card(arranger, b.type_id, tag(b), items, b.seq) for b, items in after]
                stock = sum(float(q) for q in db.scalars(select(StockItem.quantity).where(StockItem.location_id == lid)))
                note = f"{r.status_label} · planejado {sum(planned.values()):g} un. · saldo hoje {stock:g} un."
                if sum(planned.values()) > stock + 1e-9:
                    note += (" · o planejado é maior que o saldo: o saldo é dividido entre as bins sugeridas e o "
                             "restante fica reservado (tracejado)")
                lv = _level_note(db, list(planned), c)
                sections.append(section(f"after-{c}", f"Depois — {dict(SCENARIO_LABELS)[c]}", icons[c], bins,
                                        note + (" · " + lv if lv else "")))
    except ValueError as exc:
        errors.append(str(exc))
    return render(request, "packing/case.html", title=title, rot=rotation, sections=sections, scenarios=scen,
                  errors=errors)


def parse_pool_params(src: Mapping):
    submitted = "objective" in src
    raw = {k: (src.get(k, "") if submitted else POOL_DEFAULTS.get(k, "")) for k in POOL_KEYS}
    raw["types"] = [t for t in _getlist(src, "types") if t]
    errors: list[str] = []

    def num(key, label, default):
        try:
            v = parse_decimal(raw[key])
            return float(v) if v is not None else default
        except ValueError:
            errors.append(f"{label}: número inválido.")
            return default

    fill = num("fill_limit", "Enchimento máximo", 85.0)
    growth = num("growth", "Margem", 0.0)
    if not (5 <= fill <= 100):
        errors.append("Enchimento máximo deve estar entre 5% e 100%.")
    if growth < 0 or growth > 1000:
        errors.append("Margem deve estar entre 0% e 1000%.")
    on = lambda k: str(raw.get(k) or "").lower() in {"1", "on", "true"}  # noqa: E731
    prm = PoolParams(objective=raw["objective"] if raw["objective"] in POOL_OBJECTIVES else "min_bins",
                     fill_limit=fill / 100.0, growth=growth / 100.0, use_geometry=on("geometry"),
                     rotation="any" if raw["rotation"] == "any" else "upright", enforce_weight=on("weight"),
                     mix_skus=on("mix"), allow_split=on("split"), single_type=on("single_type"),
                     max_bins=to_int(raw.get("max_bins")) or None,
                     bins_mode=raw["bins_mode"] if raw["bins_mode"] in BINS_MODES else "existing",
                     allowed_types={int(t) for t in raw["types"] if str(t).isdigit()} or None)
    if raw.get("max_bins") and not to_int(raw.get("max_bins")):
        errors.append("Máximo de bins: número inteiro inválido.")
    prm.engine, prm.time_limit, prm.priority = parse_engine(raw, errors)
    prm.bin_source = "any" if raw.get("bin_source") == "any" else "free"
    prm.orphans = "leave" if raw.get("orphans") == "leave" else "relocate"
    if prm.unlimited and prm.objective == "min_moves":
        prm.objective = "min_bins"  # sem bins físicas não há movimentação a minimizar
    basis = raw["basis"] if raw["basis"] in BASES else "stock"
    return prm, basis, raw, errors


POOL_SORTS = {
    "sku": lambda m: m["sku"],
    "status": lambda m: (list(POOL_STATUS).index(m["status"]), m["sku"]),
    "qty": lambda m: (m["qty"], m["sku"]),
    "bins_delta": lambda m: (m["bins_delta"], m["sku"]),
    "volume_delta": lambda m: (m["volume_delta"], m["sku"]),
    "width_delta": lambda m: (m["width_delta"], m["sku"]),
    "fill_now": lambda m: (m["fill_now"] if m["fill_now"] is not None else -1, m["sku"]),
    "fill_after": lambda m: (m["fill_after"] if m["fill_after"] is not None else -1, m["sku"]),
    "moves": lambda m: (m["moves"], m["sku"]),
}


def pool_row(p, types: dict, unlimited: bool) -> dict:
    """Situação e indicadores de um SKU do pool (mesmas colunas do modo por endereço)."""
    before = {b.id: b for b, _n in p.orig_bins}
    after = {b.id: b for b, _n in p.allocations}
    vol = lambda bs: sum((types[b.type_id].volume_cm3 or 0) for b in bs if b.type_id in types) / 1000.0  # noqa: E731
    wid = lambda bs: sum((types[b.type_id].fw or 0) for b in bs if b.type_id in types)  # noqa: E731
    units = p.unit_vol * sum(p.current.values()) / 1000.0
    vb, va = vol(before.values()), vol(after.values())
    status = ("sem_bin" if p.shortage else "dimensionado" if unlimited else
              ("realocado" if p.secondary and p.moves else "mover" if p.moves else "manter"))
    return {"sku": p.sku, "plan": p, "status": status, "qty": p.needed,
            "bins_before": len(before), "bins_after": len(after), "bins_delta": len(after) - len(before),
            "volume_delta": va - vb, "width_delta": wid(after.values()) - wid(before.values()),
            "fill_now": units / vb * 100 if vb else None, "fill_after": units / va * 100 if va else None,
            "moves": 0 if unlimited else len(p.moves)}


@router.get("/pool")
def pool_page(request: Request, db: Session = Depends(get_db)):
    qp = request.query_params
    filters = {k: qp.get(k, "") for k in FILTER_KEYS}
    prm, basis, raw, errors = parse_pool_params(qp)
    ctx = _common_ctx(db, "pool", filters, raw, errors, POOL_OBJECTIVES, prm.unlimited)
    if qp.get("run") and not errors:
        if not filters.get("branch_id"):
            ctx["errors"] = ["Selecione ao menos a filial do escopo."]
        else:
            try:
                ids, focus = pool_scope(db, filters, raw)
                results, locs, types, notes = run_pool(db, ids, prm, basis, focus)
            except ValueError as exc:
                ctx["errors"] = [str(exc)]
            else:
                rows_all = [pool_row(p, types, prm.unlimited) for r in results for p in r.plans]
                status_f = qp.get("status", "")
                shown = [m for m in rows_all if not status_f or m["status"] == status_f]
                sort = qp.get("sort", "status")
                desc = qp.get("dir", "asc") == "desc"
                shown.sort(key=POOL_SORTS.get(sort, POOL_SORTS["status"]), reverse=desc)
                page = max(1, to_int(qp.get("page"), 1))
                pages = max(1, math.ceil(len(shown) / POOL_PER_PAGE))
                page = min(page, pages)
                freed_addr = []
                for r in results:
                    used = {b.location_id for b in r.bins if b.contents or b.locked} | r.reserved_locations
                    before = {l for p in r.plans for l, q in p.current.items() if q > 0}
                    freed_addr += sorted((locs[l][0] for l in before - used), key=str)
                by_type: dict[int, dict] = {}
                for r in results:
                    for t, v in r.by_type.items():
                        bt = by_type.setdefault(t, {"existing": 0, "needed": 0})
                        bt["existing"] += v["existing"]
                        bt["needed"] += v["needed"]
                page_rows = shown[(page - 1) * POOL_PER_PAGE: page * POOL_PER_PAGE]
                arranger = Arranger(db, prm.rotation)
                packs: dict = {}
                where = lambda b: f"{locs[b.location_id][0]} · {b.tag}" if b.location_id in locs else f"bin nova · {b.tag}"  # noqa: E731
                phys = physical_map(results)
                for m in page_rows:
                    p = m["plan"]
                    before = list({b.id: b for b, _n in p.orig_bins}.values())
                    after = list({b.id: b for b, _n in p.allocations}.values())
                    case = case_link(qp, "pool", p.sku)
                    for i, b in enumerate(after):
                        pk = arranger.view(b.type_id, after_items(b, phys), where(b), seq=b.seq)
                        pk["url"] = f"{case}#b-after-{basis}-{i}"
                        packs[(p.sku, b.id)] = pk
                    for i, b in enumerate(before):
                        pk = arranger.view(b.type_id, list(b.orig.items()), where(b), seq=b.seq)
                        pk["url"] = f"{case}#b-before-{i}"
                        packs[(p.sku, "o", b.id)] = pk
                tot = {"before": sum(r.bins_used_before for r in results), "after": sum(r.bins_used_after for r in results),
                       "bins": sum(len([b for b in r.bins if b.location_id not in r.reserved_locations and not b.locked]) for r in results),
                       "moves": sum(r.moves for r in results), "units": sum(r.moved_units for r in results),
                       "addr_before": sum(r.addresses_before for r in results),
                       "addr_after": sum(r.addresses_after for r in results),
                       "no_dims": [s for r in results for s in r.no_dims]}
                vb = sum(r.volume_before for r in results)
                va = sum(r.volume_after for r in results)
                uv = sum(r.units_volume for r in results)
                kpis = [kpi("Bins ocupadas", tot["before"], tot["after"],
                            hint=("bins novas necessárias" if prm.unlimited else f"de {tot['bins']} bins no pool")),
                        kpi("Volume de bins", vb, va, " L"),
                        kpi("Largura de frente", sum(r.width_before for r in results), sum(r.width_after for r in results), " cm"),
                        kpi("Enchimento médio", uv / vb * 100 if vb else None, uv / va * 100 if va else None, "%",
                            good_negative=False, pct=True),
                        kpi("Endereços com bins", tot["addr_before"], None if prm.unlimited else tot["addr_after"]),
                        {"label": "Dimensionados" if prm.unlimited else "Movimentações",
                         "value": len(rows_all) if prm.unlimited else tot["moves"],
                         "hint": "recurso infinito: só simulação" if prm.unlimited else f"{tot['units']:g} unidades"}]
                counts: dict[str, int] = {}
                for m in rows_all:
                    counts[m["status"]] = counts.get(m["status"], 0) + 1
                chips = [(k, lbl, cls, counts[k]) for k, (lbl, cls) in POOL_STATUS.items() if counts.get(k)]
                shortage = any(r.shortage for r in results)
                ctx.update(results=results, rows=page_rows, by_type=by_type, packs=packs, phys=phys,
                           n_shown=len(shown), n_total=len(rows_all), page_no=page, pages=pages, sort=sort,
                           sort_dir="desc" if desc else "asc", status_filter=status_f, chips=chips, kpis=kpis,
                           chip_cls={k: c for k, (_l, c) in POOL_STATUS.items()},
                           chip_lbl={k: lbl for k, (lbl, _c) in POOL_STATUS.items()}, locs=locs,
                           solvers=[_solver_view(r.solver, prm.objective, prm.priority, prm.unlimited) for r in results],
                           orphans=[(sku, locs[l][0] if l in locs else "—", b.tag if b is not None else "—", q)
                                    for r in results for sku, l, b, q in (r.orphans or [])],
                           displaced=sum(len(r.displaced or {}) for r in results),
                           focus_info=({k: sum((r.focus or {}).get(k, 0) for r in results)
                                        for k in ("skus", "locked_bins", "free_bins", "fixed_skus")}
                                       if any(r.focus for r in results) else None), types=types, notes=notes, freed_addr=freed_addr, shortage=shortage, tot=tot,
                           n_applicable=0 if prm.unlimited else sum(1 for m in rows_all if m["status"] == "mover"),
                           can_apply=not prm.unlimited)
    return render(request, "optimization/page.html", **ctx)


@router.post("/pool/apply")
def pool_apply(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    prm, basis, raw, errors = parse_pool_params(form)
    filters = {k: form.get(f"f_{k}", "") for k in FILTER_KEYS}
    back = "/optimization/pool?" + urlencode({**{k: v for k, v in filters.items() if v},
                                              **{k: v for k, v in raw.items() if k != "types" and v not in ("", None)},
                                              "run": "1"}) + "".join(f"&types={t}" for t in raw["types"])
    if errors:
        return redirect(back, " ".join(errors), "danger")
    if prm.unlimited:
        return redirect(back, "Com bins como recurso infinito o resultado é um dimensionamento (quantas bins de cada tipo "
                              "seriam necessárias) e não pode ser aplicado. Use \"Bins existentes no escopo\" para gerar "
                              "um plano de movimentação.", "warning")
    try:
        ids, focus = pool_scope(db, filters, raw)
    except ValueError as exc:
        return redirect(back, str(exc), "warning")
    if not ids:
        return redirect(back, "Nenhum endereço no escopo.", "warning")
    only = None
    if form.get("scope") == "selected":
        only = {s_ for s_ in form.getlist("ids") if s_}
        if not only:
            return redirect(back, "Nenhum SKU selecionado.", "warning")
    try:
        n, nl, applied = apply_pool(db, ids, prm, basis, only, focus)
    except ValueError as exc:
        return redirect(back, str(exc), "danger")
    extra = ""
    if only is not None and len(applied) > len(only):
        dep = sorted(applied - only)
        extra = (f" Foram junto {len(dep)} SKU(s) que precisavam sair das bins de destino: "
                 + ", ".join(dep[:10]) + ("…" if len(dep) > 10 else "") + ".")
    return redirect(back, f"Plano aplicado: {n} SKU(s) movimentado(s) em {nl} endereço(s).{extra} "
                          "A análise abaixo já reflete os novos saldos.")


# --------------------------------------------------------------------------- #
# Plano de ação (tela, Excel e PDF) — os dois modos
# --------------------------------------------------------------------------- #
def _scope_desc(db: Session, filters: dict) -> list[tuple[str, str]]:
    out = []
    b = db.get(Branch, to_int(filters.get("branch_id"))) if filters.get("branch_id") else None
    out.append(("Filial", f"{b.code} - {b.name}" if b else "—"))
    wh = filters.get("warehouse_id") or ""
    w = db.get(Warehouse, to_int(wh)) if to_int(wh) else None
    out.append(("Armazém", f"{w.code} - {w.name}" if w else ("Sem armazém" if wh == "none" else "Todos")))
    for k, label in (("q", "Endereço contém"), ("sku", "SKU ou produto"), ("material", "Tipo de material")):
        if filters.get(k):
            v = filters[k]
            out.append((label, "Sem tipo de material" if v == "__none__" else v))
    bt = filters.get("bin_type_id") or ""
    if bt:
        t = db.get(BinType, to_int(bt)) if to_int(bt) else None
        out.append(("Bins atuais", t.code if t else {"any": "Com alguma bin", "none": "Sem bin"}.get(bt, bt)))
    return out


def _types_desc(db: Session, ids) -> str:
    if not ids:
        return "Todos"
    return ", ".join(t.code for t in db.scalars(select(BinType).where(BinType.id.in_([int(i) for i in ids if str(i).isdigit()]))))


def _yes(v) -> str:
    return "Sim" if str(v or "").lower() in {"1", "on", "true"} else "Não"


def _staging(raw) -> str:
    return (str(raw.get("staging") or "").strip() or "ÁREA DE ESPERA")[:60]


def _address_params_desc(db, raw) -> list[tuple[str, str]]:
    return [("Bins", BINS_MODES.get(raw.get("bins_mode") or "existing", "")),
            ("Quantidade considerada", BASES.get(raw.get("basis") or "stock", "")),
            ("Objetivo", OBJECTIVES.get(raw.get("objective"), raw.get("objective"))),
            ("Tipos de bin permitidos", _types_desc(db, raw.get("types"))),
            ("Enchimento máximo", f"{raw.get('fill_limit')}%"), ("Margem sobre a quantidade", f"{raw.get('growth') or 0}%"),
            ("Encaixe geométrico", _yes(raw.get("geometry"))),
            ("Rotação", "Qualquer orientação" if raw.get("rotation") == "any" else "Só girar no plano"),
            ("Peso máximo da bin", _yes(raw.get("weight"))), ("SKUs na mesma bin", _yes(raw.get("mix"))),
            ("Um tipo por endereço", _yes(raw.get("single_type"))), ("Capacidade dos níveis", "Sempre respeitada"),
            ("Um único tipo de bin por nível", _yes(raw.get("uniform_levels"))),
            ("Outros níveis da estante", _yes(raw.get("other_levels"))), ("Não exceder espaço atual", _yes(raw.get("keep_space"))),
            ("Máx. bins / larg. / alt.", f"{raw.get('max_bins') or '—'} / {raw.get('max_width') or '—'} / {raw.get('max_height') or '—'}"),
            ("Liberar bins de vazios", _yes(raw.get("clear_empty"))), ("Área de espera", _staging(raw))]


def _pool_params_desc(db, raw) -> list[tuple[str, str]]:
    return [("Bins", BINS_MODES.get(raw.get("bins_mode") or "existing", "")),
            ("Quantidade considerada", BASES.get(raw.get("basis") or "stock", "")),
            ("Meta", POOL_OBJECTIVES.get(raw.get("objective"), raw.get("objective"))),
            ("Tipos de bin permitidos", _types_desc(db, raw.get("types"))),
            ("Enchimento máximo", f"{raw.get('fill_limit')}%"), ("Margem sobre a quantidade", f"{raw.get('growth') or 0}%"),
            ("Encaixe geométrico", _yes(raw.get("geometry"))),
            ("Rotação", "Qualquer orientação" if raw.get("rotation") == "any" else "Só girar no plano"),
            ("Peso máximo da bin", _yes(raw.get("weight"))), ("SKUs na mesma bin", _yes(raw.get("mix"))),
            ("Foco nos SKUs filtrados", _yes(raw.get("focus"))),
            ("Bins que podem ser usadas", BIN_SOURCES.get(raw.get("bin_source") or "free", "")),
            ("Itens desalojados", ORPHAN_MODES.get(raw.get("orphans") or "relocate", "") if raw.get("bin_source") == "any" else "—"),
            ("Mesmo SKU em mais de um endereço", _yes(raw.get("split"))), ("Um tipo por SKU", _yes(raw.get("single_type"))),
            ("Máx. bins por SKU", raw.get("max_bins") or "—"), ("Área de espera", _staging(raw))]


def _build_plan(db: Session, qp, mode: str):
    filters = {k: qp.get(k, "") for k in FILTER_KEYS}
    if not filters.get("branch_id"):
        raise ValueError("Selecione ao menos a filial do escopo.")
    ids = _scope_ids(db, filters)
    if mode == "pool":
        prm, basis, raw, errors = parse_pool_params(qp)
        if errors:
            raise ValueError(" ".join(errors))
        ids, focus = pool_scope(db, filters, raw)
        results, locs, types, notes = run_pool(db, ids, prm, basis, focus)
        plan = build_pool_plan(results, locs, types, _scope_desc(db, filters), _pool_params_desc(db, raw), prm.unlimited,
                               _staging(raw))
        plan.warnings = notes + plan.warnings
    else:
        prm, raw, errors = parse_params(qp)
        if errors:
            raise ValueError(" ".join(errors))
        ids, donors = address_scope(db, filters, raw)
        results, summary, types = run(db, ids, prm, donors)
        from services.bin_content_service import content_changes

        changes = {}
        for r in results:
            if r.applicable and r.status not in ("liberar", "orfao"):
                own = r.own_level if r.own_level is not None else "own"
                cfg = r.placement if r.placement else {own: dict(r.suggested or {})}
                changes[r.id] = (cfg, own)
        plan = build_address_plan(results, summary, types, _scope_desc(db, filters), _address_params_desc(db, raw),
                                  content_changes(db, changes, prm), _staging(raw))
        if raw.get("bins_mode") == "unlimited":
            plan.warnings.insert(0, "Recurso infinito: o plano pode usar bins que não existem hoje no escopo "
                                    "(providencie as bins a mais antes de executar; veja Bins por tipo).")
    return plan, filters, raw


def _plan_response(request: Request, db: Session, mode: str, fmt: str):
    qp = request.query_params
    back = ("/optimization/pool?" if mode == "pool" else "/optimization?") + str(qp)
    try:
        plan, filters, raw = _build_plan(db, qp, mode)
    except ValueError as exc:
        return redirect(back, str(exc), "danger")
    stamp = plan.created.strftime("%Y%m%d_%H%M")
    name = f"plano_{'pool' if mode == 'pool' else 'enderecos'}_{stamp}"
    if fmt == "xlsx":
        return Response(to_xlsx(plan), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    if fmt == "pdf":
        return Response(to_pdf(plan), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
    qs = str(qp)
    return render(request, "optimization/plan.html", plan=plan, mode=mode, qs=qs, back=back, filters=filters,
                  params=raw)


@router.get("/plan")
def address_plan(request: Request, db: Session = Depends(get_db)):
    return _plan_response(request, db, "address", "html")


@router.get("/plan.xlsx")
def address_plan_xlsx(request: Request, db: Session = Depends(get_db)):
    return _plan_response(request, db, "address", "xlsx")


@router.get("/plan.pdf")
def address_plan_pdf(request: Request, db: Session = Depends(get_db)):
    return _plan_response(request, db, "address", "pdf")


@router.get("/pool/plan")
def pool_plan(request: Request, db: Session = Depends(get_db)):
    return _plan_response(request, db, "pool", "html")


@router.get("/pool/plan.xlsx")
def pool_plan_xlsx(request: Request, db: Session = Depends(get_db)):
    return _plan_response(request, db, "pool", "xlsx")


@router.get("/pool/plan.pdf")
def pool_plan_pdf(request: Request, db: Session = Depends(get_db)):
    return _plan_response(request, db, "pool", "pdf")
