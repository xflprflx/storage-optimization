"""Mapa do estoque: desenho proporcional das estruturas a partir do cadastro de endereços."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import Branch, StorageLocation, Warehouse, branch_warehouse
from routers.common import render, to_int
from services.layout_service import get_preview, load_layout
from services.i18n import N_, _

router = APIRouter(tags=[N_("Mapa do estoque")])


def _lookups(db: Session) -> dict:
    branches = list(db.scalars(select(Branch).order_by(Branch.code)))
    warehouses = list(db.scalars(select(Warehouse).order_by(Warehouse.code)))
    wh_branches: dict[int, list[int]] = {}
    for b, w in db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)).all():
        wh_branches.setdefault(w, []).append(b)
    # Filiais com endereços "sem armazém"
    no_wh = set(db.scalars(select(StorageLocation.branch_id).where(StorageLocation.warehouse_id.is_(None)).distinct()))
    return {"branches": branches, "warehouses": warehouses, "wh_branches": wh_branches, "branches_no_wh": no_wh}


@router.get("/layout")
def layout_page(request: Request, branch_id: str = "", warehouse_id: str = "", aisle: str = "", preview: str = "",
                embed: str = "", db: Session = Depends(get_db)):
    lk = _lookups(db)
    bid = to_int(branch_id)
    # Sem filial escolhida: sugere o primeiro depósito que tem endereços
    if not bid:
        first = db.execute(select(StorageLocation.branch_id, StorageLocation.warehouse_id)
                           .order_by(StorageLocation.branch_id, StorageLocation.warehouse_id).limit(1)).first()
        if first:
            bid = first[0]
            warehouse_id = str(first[1]) if first[1] else "none"
    return render(request, "layout/index.html", branch_id=bid, warehouse_id=warehouse_id, aisle=aisle,
                  preview=preview, embed=bool(embed), **lk)


@router.get("/layout/data")
def layout_data(branch_id: int, warehouse_id: str = "", aisle: str = "", preview: str = "",
                db: Session = Depends(get_db)):
    if not db.get(Branch, branch_id):
        return JSONResponse({"detail": _("Filial não encontrada.")}, status_code=404)
    wid = None if warehouse_id in ("", "none") else to_int(warehouse_id)
    pv = get_preview(preview) if preview else None
    if preview and pv is None:
        return JSONResponse({"detail": _("A pré-visualização expirou: clique de novo em Pré-visualizar.")}, status_code=404)
    out = load_layout(db, branch_id, wid, aisle.strip().upper(), pv)
    if pv is not None:
        out["preview"] = {"new": len(pv["new"]), "removed": len(pv["removed"]), "conflicts": len(pv["conflicts"])}
    return out


# --------------------------------------------------------------------------- #
# Planta desenhada do depósito existente (fileiras de estantes, obstáculos e referência)
# --------------------------------------------------------------------------- #
def _plan_scope(db: Session, branch_id, warehouse_id) -> tuple[Optional[Branch], Optional[int], str]:
    """(filial, armazém ou None, texto do armazém). Armazém vazio = o único com planta/endereços da filial."""
    br = db.get(Branch, to_int(branch_id)) if to_int(branch_id) else None
    if br is None:
        return None, None, ""
    wh = str(warehouse_id or "").strip()
    if wh in ("", "all"):
        from models import BranchLayout

        lay = db.scalars(select(BranchLayout).where(BranchLayout.branch_id == br.id).order_by(BranchLayout.id)).first()
        if lay is not None:
            wid = lay.warehouse_id
        else:
            first = db.execute(select(StorageLocation.warehouse_id).where(StorageLocation.branch_id == br.id)
                               .order_by(StorageLocation.warehouse_id).limit(1)).first()
            wid = first[0] if first else None
    else:
        wid = None if wh == "none" else to_int(wh)
    w = db.get(Warehouse, wid) if wid else None
    return br, wid, (f"{w.code} - {w.name}" if w else _("sem armazém"))


def _plan_qs(br: Branch, wid: Optional[int]) -> str:
    return f"branch_id={br.id}&warehouse_id={wid if wid else 'none'}"


def _geom_info(g) -> dict:
    rows = g.data.get("rows") or []
    return {"corridors": [{"orient": c.orient, "center": round(c.center, 1), "width": round(c.width, 1), "lo": round(c.lo, 1),
                           "hi": round(c.hi, 1), "ends": [round(e, 1) for e in c.ends], "flow": c.flow,
                           "rows": [rows[ri]["id"] for ri in c.rows]} for c in g.corridors],
            "pairs": [[rows[i]["id"], rows[j]["id"], round(gap, 1), manual] for i, j, gap, manual in g.pairs],
            "warn": g.warn,
            "doors": [{"bay": dr["bay"], "hit": dr["hit"], "pts": [[[round(x, 1), round(y, 1)] for x, y in pts] for pts in dr["pts"]]}
                      for dr in g.doors],
            "unplaced": g.unplaced, "dup": g.dup[:50], "placed": len(g.placed)}


@router.get("/layout/plan")
def plan_editor(request: Request, branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from routers.common import redirect

    lk = _lookups(db)
    br, wid, wlabel = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        if lk["branches"]:
            return redirect(f"/layout/plan?branch_id={lk['branches'][0].id}")
        return redirect("/layout", _("Cadastre uma filial e os endereços antes de desenhar a planta."), "warning")
    from models import SiteProject

    projects = list(db.execute(select(SiteProject.id, SiteProject.name).order_by(SiteProject.updated_at.desc())))
    from services.branch_layout import get_layout

    lay = get_layout(db, br.id, wid)
    return render(request, "layout/plan.html", branch=br, wid=wid, wlabel=wlabel, qs=_plan_qs(br, wid), projects=projects,
                  has_prev=bool(lay is not None and (lay.data or {}).get("_prev")), **lk)


@router.get("/layout/plan/data")
def plan_data(branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from services.branch_layout import auto_layout, bays_json, build, clean, get_layout, scope_bays

    br, wid, _w = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    bays = scope_bays(db, br.id, wid)
    lay = get_layout(db, br.id, wid)
    data = clean(lay.data) if lay is not None else auto_layout(bays)
    return {"saved": lay is not None, "updated": str(lay.updated_at) if lay is not None else "", "data": data,
            "bays": bays_json(bays), **_geom_info(build(data, bays))}


@router.post("/layout/plan/auto")
async def plan_auto(request: Request, db: Session = Depends(get_db)):
    from services.branch_layout import auto_layout, build, scope_bays

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    bays = scope_bays(db, br.id, wid)
    from services.branch_layout import parse_widths

    data = auto_layout(bays, "row" if body.get("mode") == "row" else "aisle", body.get("aisle_w"), body.get("front"),
                       parse_widths(str(body.get("widths") or "")))
    return {"data": data, **_geom_info(build(data, bays))}


@router.post("/layout/plan/from_project")
async def plan_from_project(request: Request, db: Session = Depends(get_db)):
    """Planta a partir do plano de um projeto de filial nova (as estantes precisam ter os mesmos códigos de endereço:
    é o caso da filial criada pelo projeto). Não salva: volta para o editor."""
    from models import SiteProject
    from routers.site import _plan_for
    from services.branch_layout import build, clean, from_plan, scope_bays

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    pr = db.get(SiteProject, to_int(body.get("project_id")) or 0)
    if pr is None:
        return JSONResponse({"error": _("Projeto não encontrado.")}, status_code=404)
    plan, errors, _cfg = _plan_for(db, pr)
    if errors or plan is None or not plan.modules:
        return JSONResponse({"error": _("O projeto não tem plano válido: ") + (" ".join(errors) or _("rode a otimização."))})
    data = clean(from_plan(plan))
    return {"data": data, **_geom_info(build(data, scope_bays(db, br.id, wid)))}


@router.get("/layout/plan/ai/prompt")
def plan_ai_prompt(branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    """Prompt para o chat de IA (com os corredores e as estantes da filial), a mandar junto com a planta."""
    from services.branch_layout import scope_bays
    from services.plan_ai import prompt

    br, wid, _w = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    return {"prompt": prompt(scope_bays(db, br.id, wid))}


@router.post("/layout/plan/ai")
async def plan_ai(request: Request, db: Session = Depends(get_db)):
    """Planta a partir da resposta da IA (JSON). Não salva: volta para o editor, com os avisos dos consertos."""
    from services.branch_layout import build, scope_bays
    from services.plan_ai import parse

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    bays = scope_bays(db, br.id, wid)
    data, error, notes = parse(str(body.get("code") or "")[:500000], bays)
    if error:
        return {"error": error, "notes": notes}
    return {"data": data, "notes": notes, **_geom_info(build(data, bays))}


@router.post("/layout/plan/check")
async def plan_check(request: Request, db: Session = Depends(get_db)):
    """Corredores e pendências da planta em edição (sem salvar)."""
    from services.branch_layout import build, clean, scope_bays

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    data = clean(body.get("data") or {})
    return {"data": data, **_geom_info(build(data, scope_bays(db, br.id, wid)))}


@router.post("/layout/plan/save")
async def plan_save(request: Request, db: Session = Depends(get_db)):
    from models import BranchLayout
    from services.branch_layout import build, clean, get_layout, scope_bays

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    data = clean(body.get("data") or {})  # sem fileiras também vale (só o imóvel): as estantes são desenhadas depois
    lay = get_layout(db, br.id, wid)
    if lay is None:
        lay = BranchLayout(branch_id=br.id, warehouse_id=wid, data=data)
        db.add(lay)
    else:
        lay.data = data
    db.commit()
    return {"ok": True, "data": data, "updated": str(lay.updated_at), **_geom_info(build(data, scope_bays(db, br.id, wid)))}


@router.post("/layout/plan/delete")
async def plan_delete(request: Request, db: Session = Depends(get_db)):
    from routers.common import redirect
    from services.branch_layout import get_layout

    form = await request.form()
    br, wid, _w = _plan_scope(db, form.get("branch_id"), form.get("warehouse_id"))
    if br is None:
        return redirect("/layout", _("Filial não encontrada."), "danger")
    lay = get_layout(db, br.id, wid)
    if lay is not None:
        db.delete(lay)
        db.commit()
    return redirect(f"/layout/plan?{_plan_qs(br, wid)}", _("Planta removida: a caminhada volta a ser estimada pelo endereço."),
                    "secondary")


def _branch_abc(db: Session, branch_id: int) -> dict:
    from models import PickHistory
    from services.slotting import classify_abc, popularity

    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    return classify_abc(popularity(h.data, "lines")) if h is not None else {}


@router.get("/layout/plan/3d")
def plan_3d_page(request: Request, branch_id: str = "", warehouse_id: str = "", cmp: str = "",
                 db: Session = Depends(get_db)):
    from routers.common import redirect

    br, wid, wlabel = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return redirect("/layout", _("Filial não encontrada."), "danger")
    qs = _plan_qs(br, wid)
    v3 = {"name": f"{br.code} - {br.name} ({wlabel})", "back_url": f"/layout/plan?{qs}", "back_label": _("Voltar à planta"),
          "links": [(f"/layout?{qs}", "grid-3x3", _("Mapa do estoque")),
                    (f"/optimization/slotting/routes?branch_id={br.id}&warehouse_id={wid if wid else 'none'}", "signpost-2",
                     _("Simulador de trajeto"))],
          "data_url": f"/layout/plan/plan3d.json?{qs}", "routes_url": f"/layout/plan/routes3d.json?{qs}",
          "fix_url": f"/layout/plan?{qs}", "fix_label": _("Desenhar a planta")}
    if cmp:  # caminhada hoje × proposta de uma otimização
        from urllib.parse import quote

        v3["routes_url"] += f"&cmp={quote(cmp)}"
        v3["compare"] = True
        v3["name"] += " · " + _("hoje × proposta")
    return render(request, "site/view3d.html", v3=v3)


@router.get("/layout/plan/plan3d.json")
def plan_3d_json(branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from services.branch_layout import geometry, layout_3d

    br, wid, wlabel = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    g = geometry(db, br.id, wid)
    if g is None:
        return JSONResponse({"error": _("Esta filial ainda não tem planta desenhada. Desenhe a planta para ver o 3D.")})
    from services.appearance import enrich

    data = enrich(db, layout_3d(db, g, br.id, wid, _branch_abc(db, br.id)))
    data["building"]["name"] = f"{br.code} - {br.name}"
    if g.unplaced:
        data["note"] = _("fora da planta: {v}{v2}", v=', '.join(g.unplaced[:6]), v2='…' if len(g.unplaced) > 6 else '')
    return data


def branch_route_setup(db: Session, branch_id: int, wid: Optional[int]):
    """Amostra de pedidos do histórico + endereços com saldo na planta desenhada → (sample, nodes, start, ref, g, erro).
    Cada SKU pode ser buscado em qualquer endereço dele com saldo (o simulador usa o mais perto da referência)."""
    import dataclasses

    from models import PickHistory, Product, StockItem
    from services.addressing import scope_filter
    from services.branch_layout import geometry

    g = geometry(db, branch_id, wid)
    if g is None:
        return None, None, None, None, None, _("Esta filial ainda não tem planta desenhada.")
    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    sample = (h.data or {}).get("sample") if h else None
    if not sample:
        return None, None, None, None, g, (_("O simulador usa a amostra de pedidos do histórico de saídas da filial (aba Pedidos). "
                                           "Envie o histórico na etapa Posição pela saída (Por endereço ou Pool)."))
    from services.branch_layout import address_golden

    gold = address_golden(db, branch_id, wid)  # zona de ouro da pega pelo nível real das bins de cada endereço
    rows = db.execute(scope_filter(select(Product.sku, StorageLocation.aisle, StorageLocation.bay, StorageLocation.address,
                                          StockItem.quantity, StorageLocation.id)
                                   .join(StockItem, StockItem.product_id == Product.id)
                                   .join(StorageLocation, StorageLocation.id == StockItem.location_id)
                                   .where(StockItem.quantity >= 0), branch_id, wid))
    nodes: dict[str, list] = {}
    zero: dict[str, list] = {}  # SKU só atribuído ao endereço (saldo zero, como na filial recém-criada pelo projeto)
    from services.branch_layout import mark_back

    mark_back(db, g, branch_id, wid)  # pega pelos dois lados: endereços com as bins no fundo param na face de trás
    for sku, a, bay, addr, qty, lid in rows:
        n = g.node(a, bay, addr)
        if n is not None:
            (nodes if qty and qty > 0 else zero).setdefault(sku, []).append(dataclasses.replace(n, key=addr,
                                                                                                gold=gold.get(lid)))
    for sku, ns in zero.items():
        nodes.setdefault(sku, ns)
    return sample, nodes, g.ref_node(), g.ref, g, ""


def assign_nodes(db: Session, g, branch_id: int, wid: Optional[int], assign: dict) -> dict:
    """SKU -> pontos da planta dos endereços de uma distribuição (SKU -> [(endereço, unidades)])."""
    import dataclasses

    from services.branch_layout import address_golden

    lids = sorted({lid for lst in assign.values() for lid, q in lst if q and q > 0})
    loc = {}
    for i in range(0, len(lids), 900):
        loc.update({r[0]: r[1:] for r in db.execute(select(StorageLocation.id, StorageLocation.aisle, StorageLocation.bay,
                                                           StorageLocation.address)
                                                    .where(StorageLocation.id.in_(lids[i:i + 900])))})
    gold = address_golden(db, branch_id, wid)
    out: dict[str, list] = {}
    for sku, lst in assign.items():
        for lid, q in lst:
            if not q or q <= 0 or lid not in loc:
                continue
            a, bay, addr = loc[lid]
            n = g.node(a, bay, addr)
            if n is not None and all(x.key != addr for x in out.get(sku, [])):
                out.setdefault(sku, []).append(dataclasses.replace(n, key=addr, gold=gold.get(lid)))
    return out


@router.get("/layout/plan/routes3d.json")
def plan_routes_json(branch_id: str = "", warehouse_id: str = "", order: str = "", day: str = "", cmp: str = "",
                     kind: str = "opt", t_batch: str = "", t_bwait: str = "", release: str = "",
                     db: Session = Depends(get_db)):
    from services.route_sim import day_payload, order_payload, orders_payload, time_params

    tp = time_params({"t_batch": t_batch, "t_bwait": t_bwait})
    batch = tp["t_batch"]  # lote (4º separador): pedidos por trajeto; t_bwait: espera máx. para completar o lote
    rel = release == "1"  # horário de liberação dos pedidos (se o histórico tiver a coluna)
    br, wid, _w = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": _("Filial não encontrada.")}, status_code=404)
    sample, nodes, start, ref, g, err = branch_route_setup(db, br.id, wid)
    if err:
        return {"error": err}
    if cmp:  # hoje × proposta de uma otimização (link do resultado)
        from services import route_compare

        data = route_compare.load(cmp)
        if data is None:
            return {"error": _("A comparação expirou (o servidor foi reiniciado ou houve muitas otimizações depois). "
                               "Rode a otimização de novo e abra o 3D pelo resultado.")}
        return route_compare.payload_3d(sample, assign_nodes(db, g, br.id, wid, data["before"]),
                                        assign_nodes(db, g, br.id, wid, data["after"]), start, ref, order, day, kind,
                                        batch=batch, release=rel, tp=tp)
    if day:
        return day_payload(sample, nodes, start, ref, day, batch=batch, release=rel, tp=tp)
    return orders_payload(sample, nodes, start, ref) if not order else \
        order_payload(sample, nodes, start, ref, order, batch=batch, release=rel, tp=tp)


# --------------------------------------------------------------------------- #
# Proposta de rearranjo das estantes (planta desenhada)
# --------------------------------------------------------------------------- #
def _rearr_owner(br, wid) -> str:
    return f"rearr:{br.id}:{wid or ''}"


@router.post("/layout/plan/rearrange")
async def rearrange_start(request: Request, db: Session = Depends(get_db)):
    """Proposta de rearranjo: passagens transversais e troca de corredores na planta desenhada, medidas pelos pedidos
    do histórico (em segundo plano). Com `cmp` (token da otimização), os SKUs ficam onde a proposta os põe."""
    from database import SessionLocal
    from routers.common import redirect
    from services import jobs, route_compare
    from services.rearrange import propose_branch

    form = await request.form()
    br, wid, wlabel = _plan_scope(db, form.get("branch_id"), form.get("warehouse_id"))
    if br is None:
        return redirect("/layout", _("Filial não encontrada."), "danger")
    stored = route_compare.load(str(form.get("cmp") or ""))
    assign = stored.get("after") if stored and stored.get("branch_id") == br.id else None
    bid = br.id

    cmp = str(form.get("cmp") or "") if assign is not None else ""

    def fn(progress, cancelled):
        from services.proposals import stamp

        with SessionLocal() as s:
            st0 = stamp(s, bid, wid)
            res = propose_branch(s, bid, wid, assign=assign, progress=progress, cancelled=cancelled)
            return {**res, "stamp": st0, "cmp": cmp} if not res.get("error") else res

    token = jobs.start(_rearr_owner(br, wid), fn)
    back = f"/layout/plan?{_plan_qs(br, wid)}"
    return render(request, "site/running.html", project={"id": 0, "name": f"{br.code} - {br.name} · {wlabel}"}, token=token,
                  title=_("Proposta de rearranjo das estantes"), icon="bi-arrows-move",
                  job_base=f"/layout/plan/rearrange/job/{token}",
                  back_url=back, back_label=_("Voltar à planta"),
                  intro=_("O sistema testa obras na planta desenhada — passagens transversais e troca de corredores de lugar — e "
                          "percorre os pedidos do histórico em cada uma. Leva alguns segundos (mais em plantas grandes)."))


def _rearr_job(db: Session, token: str):
    """(filial, armazém, texto do armazém, tarefa) da proposta pelo token (o dono da tarefa diz a filial)."""
    from services import jobs

    j = jobs.get(token, jobs.owner_of(token) or "")
    if j is None or not j["owner"].startswith("rearr:"):
        return None, None, "", None
    _k, bid, wid = j["owner"].split(":")
    br, wid2, wlabel = _plan_scope(db, bid, wid or "none")
    return br, wid2, wlabel, j


@router.get("/layout/plan/rearrange/job/{token}/status")
def rearrange_status(token: str, db: Session = Depends(get_db)):
    from services import jobs

    _br, _wid, _wl, j = _rearr_job(db, token)
    st = jobs.status(j)
    return JSONResponse(st, status_code=404 if j is None else 200)


@router.post("/layout/plan/rearrange/job/{token}/cancel")
def rearrange_cancel(token: str, db: Session = Depends(get_db)):
    _br, _wid, _wl, j = _rearr_job(db, token)
    if j is not None and not j["done"]:
        j["cancel"] = True
    return {"ok": j is not None}


@router.get("/layout/plan/rearrange/job/{token}/result")
def rearrange_result(token: str, request: Request, db: Session = Depends(get_db)):
    from routers.common import redirect
    from services.branch_layout import build, scope_bays, svg as layout_svg

    br, wid, wlabel, j = _rearr_job(db, token)
    back = f"/layout/plan?{_plan_qs(br, wid)}" if br is not None else "/layout"
    if j is None or not j["done"]:
        return redirect(back, _("A busca expirou ou ainda não terminou: rode de novo."), "warning")
    res = j["res"]
    if j["error"] or not res or res.get("error"):
        return redirect(back, j["error"] or (res or {}).get("error") or _("Busca sem resultado."), "danger")
    bays = scope_bays(db, br.id, wid)
    svgs = []
    for v in res["variants"]:
        if (v.get("current") or v.get("best")) and v.get("data"):
            svgs.append((_("Atual") if v.get("current") else _("Melhor trajeto: {label}", label=v["label"]),
                         layout_svg(build(v["data"], bays), 560)))
    svgs.sort(key=lambda t: 0 if t[0] == _("Atual") else 1)
    from services.proposals import stale_reason

    return render(request, "layout/rearrange.html", res=res, branch=br, wid=wid, wlabel=wlabel, qs=_plan_qs(br, wid),
                  token=token, svgs=svgs, back=back, stale=stale_reason(db, br.id, wid, res.get("stamp"), "rearrange"))


@router.post("/layout/plan/rearrange/apply")
async def rearrange_apply(request: Request, db: Session = Depends(get_db)):
    """Grava a variação escolhida como a planta da filial (a anterior fica guardada para desfazer)."""
    from routers.common import redirect
    from services.branch_layout import clean, get_layout

    form = await request.form()
    br, wid, _wl, j = _rearr_job(db, str(form.get("token") or ""))
    back = f"/layout/plan?{_plan_qs(br, wid)}" if br is not None else "/layout"
    try:
        v = j["res"]["variants"][int(form.get("idx"))] if j and j.get("res") else None
    except (ValueError, TypeError, IndexError, KeyError):
        v = None
    lay = get_layout(db, br.id, wid) if br is not None else None
    if v is None or v.get("current") or not v.get("data") or lay is None:
        return redirect(back, _("A proposta expirou: gere de novo."), "warning")
    prev = {k: x for k, x in (lay.data or {}).items() if k != "_prev"}
    lay.data = {**clean(v["data"]), "_prev": prev}
    db.commit()
    return redirect(back, _("Rearranjo aplicado à planta: {label}. Use Desfazer rearranjo para voltar à anterior.", label=v["label"]))


@router.post("/layout/plan/rearrange/undo")
async def rearrange_undo(request: Request, db: Session = Depends(get_db)):
    from routers.common import redirect
    from services.branch_layout import clean, get_layout

    form = await request.form()
    br, wid, _wl = _plan_scope(db, form.get("branch_id"), form.get("warehouse_id"))
    lay = get_layout(db, br.id, wid) if br is not None else None
    back = f"/layout/plan?{_plan_qs(br, wid)}" if br is not None else "/layout"
    if lay is None or not (lay.data or {}).get("_prev"):
        return redirect(back, _("Não há rearranjo para desfazer."), "warning")
    lay.data = clean(lay.data["_prev"])
    db.commit()
    return redirect(back, _("Rearranjo desfeito: a planta voltou a ser a anterior."), "secondary")


# --------------------------------------------------------------------------- #
# Proposta combinada: rearranjo da planta + recomposição das estruturas em cada variação
# --------------------------------------------------------------------------- #
@router.post("/layout/plan/combined")
async def combined_start(request: Request, db: Session = Depends(get_db)):
    """Para cada variação da planta (passagens, troca de corredores), a recomposição das estruturas calculada nela
    (em segundo plano). Com `cmp` (token da otimização), os SKUs ficam onde a proposta os põe."""
    from database import SessionLocal
    from routers.common import redirect
    from services import jobs, route_compare
    from services.proposals import propose_combined

    form = await request.form()
    br, wid, wlabel = _plan_scope(db, form.get("branch_id"), form.get("warehouse_id"))
    if br is None:
        return redirect("/layout", _("Filial não encontrada."), "danger")
    stored = route_compare.load(str(form.get("cmp") or ""))
    assign = stored.get("after") if stored and stored.get("branch_id") == br.id else None
    pieces = "unlimited" if form.get("pieces") == "unlimited" else "existing"
    try:
        min_gain = max(1, int(form.get("min_gain") or 1))
    except ValueError:
        min_gain = 1
    bid, cmp = br.id, (str(form.get("cmp") or "") if assign is not None else "")

    def fn(progress, cancelled):
        with SessionLocal() as s:
            res = propose_combined(s, bid, wid, assign=assign, pieces=pieces, min_gain=min_gain, progress=progress,
                                   cancelled=cancelled)
            return {**res, "cmp": cmp} if not res.get("error") else res

    token = jobs.start(_rearr_owner(br, wid), fn)
    back = str(form.get("back") or "")
    return render(request, "site/running.html", project={"id": 0, "name": f"{br.code} - {br.name} · {wlabel}"}, token=token,
                  title=_("Proposta combinada: rearranjo + recomposição"), icon="bi-intersect",
                  job_base=f"/layout/plan/combined/job/{token}",
                  back_url=back if back.startswith("/") else f"/layout/plan?{_plan_qs(br, wid)}", back_label=_("Voltar"),
                  intro=_("O sistema testa as variações da planta (passagens transversais e troca de corredores) e, em cada "
                          "uma, a recomposição das estruturas com as estantes vizinhas daquela planta. Leva mais que as "
                          "propostas separadas: uma recomposição por variação."))


@router.get("/layout/plan/combined/job/{token}/status")
def combined_status(token: str, db: Session = Depends(get_db)):
    return rearrange_status(token, db)


@router.post("/layout/plan/combined/job/{token}/cancel")
def combined_cancel(token: str, db: Session = Depends(get_db)):
    return rearrange_cancel(token, db)


@router.get("/layout/plan/combined/job/{token}/result")
def combined_result(token: str, request: Request, db: Session = Depends(get_db)):
    from routers.common import redirect
    from services.proposals import stale_reason

    br, wid, wlabel, j = _rearr_job(db, token)
    back = f"/layout/plan?{_plan_qs(br, wid)}" if br is not None else "/layout"
    if j is None or not j["done"]:
        return redirect(back, _("A busca expirou ou ainda não terminou: rode de novo."), "warning")
    res = j["res"]
    if j["error"] or not res or res.get("error"):
        return redirect(back, j["error"] or (res or {}).get("error") or _("Busca sem resultado."), "danger")
    svgs, cur_svg, sel = _combined_svgs(db, br.id, wid, res)
    return render(request, "layout/combined.html", res=res, branch=br, wid=wid, wlabel=wlabel, token=token, back=back,
                  stale=stale_reason(db, br.id, wid, res.get("stamp"), "combined"), svgs=svgs, cur_svg=cur_svg,
                  sel=sel)


MARK_RECOMP, MARK_MOVED, MARK_BOTH = "#f4a259", "#8ec5ff", "#c39bd3"


def _combined_svgs(db: Session, bid: int, wid: Optional[int], res: dict) -> tuple[list, str, int]:
    """Desenho de cada opção da proposta combinada, com as estantes a recompor e as que mudam de lugar pintadas;
    a planta atual; e a opção mostrada de início (melhor trajeto, senão mais bins, senão a primeira)."""
    from services.branch_layout import build, get_layout, scope_bays, svg as layout_svg

    bays = scope_bays(db, bid, wid)
    lay = get_layout(db, bid, wid)
    try:
        g_cur = build(next((o["variant"]["data"] for o in res["options"] if o["variant"].get("current")), None)
                      or lay.data, bays)
    except Exception:  # noqa: BLE001 — planta apagada ou inválida: sem desenhos
        return [], "", 0
    pos = {(p.bay.aisle, p.bay.bay): (round(p.x), round(p.y)) for p in g_cur.placed.values()}
    out = []
    for o in res["options"]:
        v = o["variant"]
        g = g_cur if v.get("current") else build(v["data"], bays)
        recomp = {(c["aisle"], c["bay"]) for c in o["recomp"].get("changes") or []}
        moved = {(p.bay.aisle, p.bay.bay) for p in g.placed.values()
                 if pos.get((p.bay.aisle, p.bay.bay)) not in (None, (round(p.x), round(p.y)))}
        mark = {}
        for k in recomp | moved:
            both = k in recomp and k in moved
            mark[k] = (MARK_BOTH if both else MARK_RECOMP if k in recomp else MARK_MOVED,
                       _("recompor e mudar de lugar") if both else _("recompor") if k in recomp else _("muda de lugar"))
        out.append({"svg": layout_svg(g, 560, mark), "recomp": len(recomp), "moved": len(moved)})
    sel = next((i for i, o in enumerate(res["options"]) if o["best_walk"]), None)
    if sel is None:
        sel = next((i for i, o in enumerate(res["options"]) if o["best_gain"]), 0)
    return out, layout_svg(g_cur, 560), sel


@router.post("/layout/plan/combined/apply")
async def combined_apply(request: Request, db: Session = Depends(get_db)):
    """Grava a opção escolhida: a planta da variação e a recomposição calculada nela."""
    from routers.common import redirect
    from services.proposals import apply_combined

    form = await request.form()
    br, wid, _wl, j = _rearr_job(db, str(form.get("token") or ""))
    back = f"/layout/plan?{_plan_qs(br, wid)}" if br is not None else "/layout"
    if j is None or not j.get("res") or "options" not in j["res"]:
        return redirect(back, _("A proposta expirou: gere de novo."), "warning")
    try:
        idx = int(form.get("idx"))
    except (TypeError, ValueError):
        idx = -1
    msg, level = apply_combined(db, j["res"], idx)
    if level == "success":
        j["res"]["applied"] = True
    return redirect(back, msg, level)
