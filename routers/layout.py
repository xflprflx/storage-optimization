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

router = APIRouter(tags=["Mapa do estoque"])


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
        return JSONResponse({"detail": "Filial não encontrada."}, status_code=404)
    wid = None if warehouse_id in ("", "none") else to_int(warehouse_id)
    pv = get_preview(preview) if preview else None
    if preview and pv is None:
        return JSONResponse({"detail": "A pré-visualização expirou: clique de novo em Pré-visualizar."}, status_code=404)
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
    return br, wid, (f"{w.code} - {w.name}" if w else "sem armazém")


def _plan_qs(br: Branch, wid: Optional[int]) -> str:
    return f"branch_id={br.id}&warehouse_id={wid if wid else 'none'}"


def _geom_info(g) -> dict:
    rows = g.data.get("rows") or []
    return {"corridors": [{"orient": c.orient, "center": round(c.center, 1), "width": round(c.width, 1), "lo": round(c.lo, 1),
                           "hi": round(c.hi, 1), "ends": [round(e, 1) for e in c.ends], "flow": c.flow,
                           "rows": [rows[ri]["id"] for ri in c.rows]} for c in g.corridors],
            "pairs": [[rows[i]["id"], rows[j]["id"], round(gap, 1), manual] for i, j, gap, manual in g.pairs],
            "unplaced": g.unplaced, "dup": g.dup[:50], "placed": len(g.placed)}


@router.get("/layout/plan")
def plan_editor(request: Request, branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from routers.common import redirect

    lk = _lookups(db)
    br, wid, wlabel = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        if lk["branches"]:
            return redirect(f"/layout/plan?branch_id={lk['branches'][0].id}")
        return redirect("/layout", "Cadastre uma filial e os endereços antes de desenhar a planta.", "warning")
    from models import SiteProject

    projects = list(db.execute(select(SiteProject.id, SiteProject.name).order_by(SiteProject.updated_at.desc())))
    return render(request, "layout/plan.html", branch=br, wid=wid, wlabel=wlabel, qs=_plan_qs(br, wid), projects=projects,
                  **lk)


@router.get("/layout/plan/data")
def plan_data(branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from services.branch_layout import auto_layout, bays_json, build, clean, get_layout, scope_bays

    br, wid, _w = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
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
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
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
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
    pr = db.get(SiteProject, to_int(body.get("project_id")) or 0)
    if pr is None:
        return JSONResponse({"error": "Projeto não encontrado."}, status_code=404)
    plan, errors, _cfg = _plan_for(db, pr)
    if errors or plan is None or not plan.modules:
        return JSONResponse({"error": "O projeto não tem plano válido: " + (" ".join(errors) or "rode a otimização.")})
    data = clean(from_plan(plan))
    return {"data": data, **_geom_info(build(data, scope_bays(db, br.id, wid)))}


@router.post("/layout/plan/check")
async def plan_check(request: Request, db: Session = Depends(get_db)):
    """Corredores e pendências da planta em edição (sem salvar)."""
    from services.branch_layout import build, clean, scope_bays

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
    data = clean(body.get("data") or {})
    return {"data": data, **_geom_info(build(data, scope_bays(db, br.id, wid)))}


@router.post("/layout/plan/save")
async def plan_save(request: Request, db: Session = Depends(get_db)):
    from models import BranchLayout
    from services.branch_layout import build, clean, get_layout, scope_bays

    body = await request.json()
    br, wid, _w = _plan_scope(db, body.get("branch_id"), body.get("warehouse_id"))
    if br is None:
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
    data = clean(body.get("data") or {})
    if not data["rows"]:
        return JSONResponse({"error": "A planta não tem fileiras."}, status_code=400)
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
        return redirect("/layout", "Filial não encontrada.", "danger")
    lay = get_layout(db, br.id, wid)
    if lay is not None:
        db.delete(lay)
        db.commit()
    return redirect(f"/layout/plan?{_plan_qs(br, wid)}", "Planta removida: a caminhada volta a ser estimada pelo endereço.",
                    "secondary")


def _branch_abc(db: Session, branch_id: int) -> dict:
    from models import PickHistory
    from services.slotting import classify_abc, popularity

    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    return classify_abc(popularity(h.data, "lines")) if h is not None else {}


@router.get("/layout/plan/3d")
def plan_3d_page(request: Request, branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from routers.common import redirect

    br, wid, wlabel = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return redirect("/layout", "Filial não encontrada.", "danger")
    qs = _plan_qs(br, wid)
    v3 = {"name": f"{br.code} - {br.name} ({wlabel})", "back_url": f"/layout/plan?{qs}", "back_label": "Voltar à planta",
          "links": [(f"/layout?{qs}", "grid-3x3", "Mapa do estoque"),
                    (f"/optimization/slotting/routes?branch_id={br.id}&warehouse_id={wid if wid else 'none'}", "signpost-2",
                     "Simulador de trajeto")],
          "data_url": f"/layout/plan/plan3d.json?{qs}", "routes_url": f"/layout/plan/routes3d.json?{qs}",
          "fix_url": f"/layout/plan?{qs}", "fix_label": "Desenhar a planta"}
    return render(request, "site/view3d.html", v3=v3)


@router.get("/layout/plan/plan3d.json")
def plan_3d_json(branch_id: str = "", warehouse_id: str = "", db: Session = Depends(get_db)):
    from services.branch_layout import geometry, layout_3d

    br, wid, wlabel = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
    g = geometry(db, br.id, wid)
    if g is None:
        return JSONResponse({"error": "Esta filial ainda não tem planta desenhada. Desenhe a planta para ver o 3D."})
    data = layout_3d(db, g, br.id, wid, _branch_abc(db, br.id))
    data["building"]["name"] = f"{br.code} - {br.name}"
    if g.unplaced:
        data["note"] = f"fora da planta: {', '.join(g.unplaced[:6])}{'…' if len(g.unplaced) > 6 else ''}"
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
        return None, None, None, None, None, "Esta filial ainda não tem planta desenhada."
    h = db.scalar(select(PickHistory).where(PickHistory.branch_id == branch_id))
    sample = (h.data or {}).get("sample") if h else None
    if not sample:
        return None, None, None, None, g, ("O simulador usa a amostra de pedidos do histórico de saídas da filial (aba Pedidos). "
                                           "Envie o histórico na etapa Posição pela saída (Por endereço ou Pool).")
    from services.branch_layout import address_golden

    gold = address_golden(db, branch_id, wid)  # zona de ouro da pega pelo nível real das bins de cada endereço
    rows = db.execute(scope_filter(select(Product.sku, StorageLocation.aisle, StorageLocation.bay, StorageLocation.address,
                                          StockItem.quantity, StorageLocation.id)
                                   .join(StockItem, StockItem.product_id == Product.id)
                                   .join(StorageLocation, StorageLocation.id == StockItem.location_id)
                                   .where(StockItem.quantity >= 0), branch_id, wid))
    nodes: dict[str, list] = {}
    zero: dict[str, list] = {}  # SKU só atribuído ao endereço (saldo zero, como na filial recém-criada pelo projeto)
    for sku, a, bay, addr, qty, lid in rows:
        n = g.node(a, bay)
        if n is not None:
            (nodes if qty and qty > 0 else zero).setdefault(sku, []).append(dataclasses.replace(n, key=addr,
                                                                                                gold=gold.get(lid)))
    for sku, ns in zero.items():
        nodes.setdefault(sku, ns)
    return sample, nodes, g.ref_node(), g.ref, g, ""


@router.get("/layout/plan/routes3d.json")
def plan_routes_json(branch_id: str = "", warehouse_id: str = "", order: str = "", db: Session = Depends(get_db)):
    from services.route_sim import order_payload, orders_payload

    br, wid, _w = _plan_scope(db, branch_id, warehouse_id)
    if br is None:
        return JSONResponse({"error": "Filial não encontrada."}, status_code=404)
    sample, nodes, start, ref, _g, err = branch_route_setup(db, br.id, wid)
    if err:
        return {"error": err}
    return orders_payload(sample, nodes, start, ref) if not order else order_payload(sample, nodes, start, ref, order)
