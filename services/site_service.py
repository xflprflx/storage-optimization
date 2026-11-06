"""Projeto de filial nova: configuração (JSON do projeto), carga da demanda/estruturas do banco, execução,
exportação para Excel e geração do cadastro da filial (endereços, níveis e bins) a partir do plano."""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    BinType,
    Branch,
    LevelCapacity,
    LocationBin,
    Product,
    ShelfLevel,
    SiteProject,
    StockItem,
    StorageLocation,
    StructureType,
    Warehouse,
)
from services.bin_optimizer import BASES, OBJECTIVES, BinSpec, ProductUnit
from services.file_reader import parse_decimal
from services.material_service import material_condition
from services.optimization_service import load_types
from services.site_planner import (
    ORIENTATIONS,
    SITE_OBJECTIVES,
    STRUCT_MODES,
    Building,
    Demand,
    SiteParams,
    SitePlan,
    StructureSpec,
    plan_site,
)

BUILDING_FIELDS = (  # chave, rótulo, padrão, mínimo
    ("length_m", "Comprimento (m)", "", 1),
    ("width_m", "Largura (m)", "", 1),
    ("clear_height_m", "Pé-direito útil (m)", "", 1),
    ("aisle_m", "Largura dos corredores (m)", "3", 0.5),
    ("wall_gap_m", "Afastamento das paredes (m)", "0,3", 0),
    ("front_reserved_m", "Faixa reservada na frente — docas/expedição (m)", "0", 0),
    ("back_reserved_m", "Faixa reservada no fundo (m)", "0", 0),
    ("cross_aisles", "Corredores transversais (qtd.)", "0", 0),
    ("cross_aisle_m", "Largura dos transversais (m)", "2", 0),
)
DEFAULT_CONFIG = {
    "building": {k: d for k, _, d, _ in BUILDING_FIELDS} | {"orientation": "auto"},
    "structures": {},
    "demand": {"source": "catalog", "branch_id": "", "warehouse_id": "", "sku": "", "material": "", "basis": "max", "growth": "0",
               "factor": "100"},
    "params": {"objective": "modules", "bin_objective": "min_volume", "fill_limit": "85", "rotation": "upright",
               "single_type": "1", "weight": "1", "level_fill": "100", "top_gap_cm": "5", "side_gap_cm": "0",
               "types": [], "time_limit": "15"},
}


def _num(raw, label: str, errors: list, default: Optional[float] = None, lo: Optional[float] = None,
         hi: Optional[float] = None, required: bool = False) -> Optional[float]:
    try:
        v = parse_decimal(str(raw or "").strip())
    except ValueError:
        errors.append(f"{label}: número inválido.")
        return default
    if v is None:
        if required:
            errors.append(f"{label}: obrigatório.")
        return default
    f = float(v)
    if lo is not None and f < lo:
        errors.append(f"{label}: mínimo {lo:g}.")
    if hi is not None and f > hi:
        errors.append(f"{label}: máximo {hi:g}.")
    return f


def merged(config: Optional[dict]) -> dict:
    """Configuração do projeto com os padrões preenchidos (projetos antigos continuam abrindo)."""
    cfg = config or {}
    out = {}
    for sec, dflt in DEFAULT_CONFIG.items():
        if isinstance(dflt, dict) and sec != "structures":
            out[sec] = {**dflt, **(cfg.get(sec) or {})}
        else:
            out[sec] = dict(cfg.get(sec) or {})
    out["items"] = list(cfg.get("items") or [])  # SKUs enviados por arquivo (fonte "upload")
    out["import_mapping"] = dict(cfg.get("import_mapping") or {})  # colunas da última importação
    return out


def config_from_form(form: Mapping) -> dict:
    """Lê o formulário da tela do projeto (checkbox ausente = desligado)."""
    on = lambda k: "1" if str(form.get(k) or "").lower() in {"1", "on", "true"} else ""  # noqa: E731
    cfg = merged(None)
    for k, *_ in BUILDING_FIELDS:
        cfg["building"][k] = str(form.get(f"b_{k}") or "").strip()
    cfg["building"]["orientation"] = form.get("b_orientation") if form.get("b_orientation") in ORIENTATIONS else "auto"
    sids = form.getlist("s_id") if hasattr(form, "getlist") else []
    for sid in sids:
        mode = form.get(f"s_mode_{sid}")
        cfg["structures"][str(sid)] = {
            "mode": mode if mode in STRUCT_MODES else "off",
            "qty": str(form.get(f"s_qty_{sid}") or "").strip(),
            "min_qty": str(form.get(f"s_min_{sid}") or "").strip(),
            "max_qty": str(form.get(f"s_max_{sid}") or "").strip(),
        }
    for k in ("branch_id", "warehouse_id", "sku", "material", "growth", "factor"):
        cfg["demand"][k] = str(form.get(f"d_{k}") or "").strip()
    cfg["demand"]["basis"] = form.get("d_basis") if form.get("d_basis") in BASES else "stock"
    cfg["demand"]["source"] = "upload" if form.get("d_source") == "upload" else "catalog"
    p = cfg["params"]
    p["objective"] = form.get("p_objective") if form.get("p_objective") in SITE_OBJECTIVES else "modules"
    p["bin_objective"] = form.get("p_bin_objective") if form.get("p_bin_objective") in OBJECTIVES else "min_volume"
    p["rotation"] = "any" if form.get("p_rotation") == "any" else "upright"
    for k in ("fill_limit", "level_fill", "top_gap_cm", "side_gap_cm", "time_limit"):
        p[k] = str(form.get(f"p_{k}") or "").strip()
    p["single_type"], p["weight"] = on("p_single_type"), on("p_weight")
    types = form.getlist("p_types") if hasattr(form, "getlist") else []
    p["types"] = [t for t in types if str(t).isdigit()]
    return cfg


def parse_config(cfg: dict) -> tuple[Building, dict, SiteParams, list[str]]:
    """Valida a configuração. Retorna (imóvel, modos das estruturas, parâmetros, erros)."""
    errors: list[str] = []
    bc = cfg["building"]
    labels = {k: lab for k, lab, _, _ in BUILDING_FIELDS}
    req = {"length_m", "width_m", "clear_height_m"}
    vals = {k: _num(bc.get(k), labels[k], errors, default=float(parse_decimal(d) or 0) if d else 0.0, lo=mn,
                    required=k in req)
            for k, _, d, mn in BUILDING_FIELDS}
    b = Building(length_m=vals["length_m"] or 0, width_m=vals["width_m"] or 0,
                 clear_height_m=vals["clear_height_m"] or 0, aisle_m=vals["aisle_m"] or 0,
                 wall_gap_m=vals["wall_gap_m"] or 0, front_reserved_m=vals["front_reserved_m"] or 0,
                 back_reserved_m=vals["back_reserved_m"] or 0, cross_aisles=int(vals["cross_aisles"] or 0),
                 cross_aisle_m=vals["cross_aisle_m"] or 0, orientation=bc.get("orientation") or "auto")
    modes = {}
    for sid, sc in cfg["structures"].items():
        mode = sc.get("mode") if sc.get("mode") in STRUCT_MODES else "off"
        qty = _num(sc.get("qty"), f"Quantidade fixa ({sid})", errors, 0, lo=0)
        mn = _num(sc.get("min_qty"), f"Mínimo ({sid})", errors, 0, lo=0)
        mx = _num(sc.get("max_qty"), f"Máximo ({sid})", errors, None, lo=0)
        if mode == "fixed" and not qty:
            errors.append("Estrutura com quantidade fixa: informe a quantidade (ou use \"Otimizar quantidade\").")
        if mode == "auto" and mx is not None and mn and mn > mx:
            errors.append("Estrutura: o mínimo é maior que o máximo.")
        modes[int(sid)] = {"mode": mode, "qty": int(qty or 0), "min_qty": int(mn or 0),
                           "max_qty": int(mx) if mx is not None else None}
    p = cfg["params"]
    prm = SiteParams(
        objective=p.get("objective") if p.get("objective") in SITE_OBJECTIVES else "modules",
        bin_objective=p.get("bin_objective") if p.get("bin_objective") in OBJECTIVES else "min_volume",
        fill_limit=(_num(p.get("fill_limit"), "Taxa máxima de enchimento das bins", errors, 85, 5, 100) or 85) / 100,
        rotation="any" if p.get("rotation") == "any" else "upright",
        single_type=bool(p.get("single_type")),
        enforce_weight=bool(p.get("weight")),
        growth=(_num(cfg["demand"].get("growth"), "Margem de crescimento", errors, 0, 0, 1000) or 0) / 100,
        level_fill=(_num(p.get("level_fill"), "Aproveitamento da frente do nível", errors, 100, 10, 100) or 100) / 100,
        top_gap_cm=_num(p.get("top_gap_cm"), "Folga acima da bin", errors, 5, 0) or 0,
        side_gap_cm=_num(p.get("side_gap_cm"), "Folga entre bins", errors, 0, 0) or 0,
        allowed_types={int(t) for t in p.get("types") or [] if str(t).isdigit()} or None,
        time_limit=_num(p.get("time_limit"), "Tempo limite", errors, 15, 1, 600) or 15,
    )
    if b.length_m and b.width_m and b.front_reserved_m + b.back_reserved_m >= max(b.length_m, b.width_m):
        errors.append("As faixas reservadas ocupam todo o imóvel.")
    return b, modes, prm, errors


def structure_specs(db: Session, modes: dict) -> list[StructureSpec]:
    out = []
    for st in db.scalars(select(StructureType).order_by(StructureType.code)):
        md = modes.get(st.id, {"mode": "off", "qty": 0, "min_qty": 0, "max_qty": None})
        out.append(StructureSpec(st.id, st.code, st.name, float(st.width), float(st.depth), float(st.height),
                                 int(st.levels), float(st.level_height),
                                 float(st.max_load) if st.max_load is not None else None,
                                 float(st.cost) if st.cost is not None else None, **md))
    return out


def parents_of(db: Session, types: dict[int, BinSpec]) -> dict:
    out = {}
    for bt in db.scalars(select(BinType).where(BinType.parent_id.is_not(None))):
        if bt.is_fraction and bt.parent_id in types:
            out[bt.id] = (types[bt.parent_id], bt.per_parent)
    return out


def _factor(dc: dict) -> float:
    try:
        return float(parse_decimal(str(dc.get("factor") or "100")) or 100) / 100
    except ValueError:
        return 1.0


def load_uploaded_demand(db: Session, items: list[dict], dc: dict) -> tuple[list[Demand], list[str]]:
    """Demanda a partir dos SKUs enviados no arquivo do projeto (dados da filial NOVA).
    Dimensões/peso/nome vazios vêm do cadastro de produtos quando o SKU existe nele."""
    notes: list[str] = []
    basis = dc.get("basis") if dc.get("basis") in BASES else "stock"
    factor = _factor(dc)
    term = (dc.get("sku") or "").strip().casefold()
    mat = (dc.get("material") or "").strip()
    skus = [it["sku"] for it in items]
    cat = {}
    for i in range(0, len(skus), 900):
        for p in db.scalars(select(Product).where(Product.sku.in_(skus[i:i + 900]))):
            cat[p.sku] = p
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    out, no_level, from_cat = [], 0, 0
    for it in items:
        p = cat.get(it["sku"])
        name = it.get("name") or (p.name if p else "")
        material = it.get("material") or (p.material_type if p else "") or ""
        if term and term not in it["sku"].casefold() and term not in name.casefold():
            continue
        if mat and ((mat == "__none__" and material) or (mat != "__none__" and material != mat)):
            continue
        q = it.get("stock") or 0.0
        if basis != "stock":
            lvl = it.get(basis)
            if lvl:
                q = max(lvl, q)
            elif q > 0:
                no_level += 1
        if q <= 0:
            continue
        dims = [it.get(k) for k in ("h", "w", "l", "wt")]
        if p is not None and any(v is None for v in dims):
            filled = [v if v is not None else f(pv) for v, pv in zip(dims, (p.height, p.width, p.length, p.weight))]
            if filled != dims:
                from_cat += 1
            dims = filled
        out.append(Demand(it["sku"], name, material, ProductUnit(it["sku"], q * factor, *dims)))
    if no_level:
        notes.append(f"{no_level} SKU(s) do arquivo sem {'min' if basis == 'min' else 'max'} level: usou o saldo.")
    if from_cat:
        notes.append(f"{from_cat} SKU(s) do arquivo tiveram dimensões/peso completados pelo cadastro de produtos.")
    return out, notes


def load_demand(db: Session, dc: dict) -> tuple[list[Demand], list[str]]:
    """SKUs e quantidades a guardar na filial nova, a partir dos produtos cadastrados.
    Base: saldo atual (da filial/armazém de referência, ou de todas) ou o nível mínimo/máximo do produto
    (sem nível cadastrado, usa o saldo). O fator (%) escala a demanda da referência para a filial nova."""
    notes: list[str] = []
    stock_q = select(StockItem.product_id, func.sum(StockItem.quantity)).join(
        StorageLocation, StorageLocation.id == StockItem.location_id)
    bid = dc.get("branch_id")
    if str(bid).isdigit():
        stock_q = stock_q.where(StorageLocation.branch_id == int(bid))
        wid = dc.get("warehouse_id")
        if wid == "none":
            stock_q = stock_q.where(StorageLocation.warehouse_id.is_(None))
        elif str(wid).isdigit():
            stock_q = stock_q.where(StorageLocation.warehouse_id == int(wid))
    stock = {pid: float(q or 0) for pid, q in db.execute(stock_q.group_by(StockItem.product_id))}
    stmt = select(Product)
    cond = material_condition(dc.get("material"))
    if cond is not None:
        stmt = stmt.where(cond)
    term = (dc.get("sku") or "").strip()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(Product.sku.ilike(like) | Product.name.ilike(like))
    basis = dc.get("basis") if dc.get("basis") in BASES else "stock"
    factor = _factor(dc)
    scoped = str(bid).isdigit()
    out, no_level = [], 0
    for p in db.scalars(stmt.order_by(Product.sku)):
        q = stock.get(p.id, 0.0)
        if basis != "stock":
            lvl = p.min_level if basis == "min" else p.max_level
            if lvl is not None and lvl > 0:
                if scoped and p.id not in stock:
                    continue  # referência = filial: só os produtos que ela trabalha
                q = max(float(lvl), q)
            elif q > 0:
                no_level += 1
        if q <= 0:
            continue
        f = lambda v: float(v) if v is not None else None  # noqa: E731
        out.append(Demand(p.sku, p.name, p.material_type or "",
                          ProductUnit(p.sku, q * factor, f(p.height), f(p.width), f(p.length), f(p.weight))))
    if no_level:
        label = "nível mínimo" if basis == "min" else "nível máximo"
        notes.append(f"{no_level} SKU(s) sem {label} cadastrado: usou o saldo atual.")
    return out, notes


def run_project(db: Session, cfg: dict) -> tuple[Optional[SitePlan], list[str]]:
    b, modes, prm, errors = parse_config(cfg)
    if errors:
        return None, errors
    types = load_types(db)
    structs = structure_specs(db, modes)
    if cfg["demand"].get("source") == "upload":
        if not cfg.get("items"):
            return None, ["Fonte da demanda = arquivo, mas nenhum SKU foi enviado. Importe a planilha (aba SKUs)."]
        demands, notes = load_uploaded_demand(db, cfg["items"], cfg["demand"])
    else:
        demands, notes = load_demand(db, cfg["demand"])
    plan = plan_site(b, structs, types, demands, prm, parents_of(db, types))
    plan.notes = notes + plan.notes
    return plan, []


# --------------------------------------------------------------------------- #
# Exportação
# --------------------------------------------------------------------------- #
def to_xlsx(plan: SitePlan, project: SiteProject, types: dict[int, BinSpec]) -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F2A2A"))

    def table(ws, cols, rows):
        ws.append(cols)
        for c in ws[ws.max_row]:
            c.font, c.fill = head
        for r in rows:
            ws.append(list(r))
        for col in ws.columns:
            w = max((len(str(c.value)) for c in col if c.value is not None), default=8) + 2
            ws.column_dimensions[get_column_letter(col[0].column)].width = max(8, min(50, w))
        ws.freeze_panes = "A2"

    b = plan.building
    ws = wb.active
    ws.title = "Resumo"
    ws.append([f"Projeto de filial: {project.name}"])
    ws["A1"].font = Font(bold=True, size=14)
    rows = [
        ("Imóvel (m)", f"{b.length_m:g} × {b.width_m:g} × {b.clear_height_m:g} (pé-direito)"),
        ("Área total (m²)", round(b.area_m2, 1)),
        ("Fileiras / corredores", f"{len(plan.floor.rows)} / {len(plan.floor.aisles)}"),
        ("Módulos de estrutura", plan.total_modules),
        ("Custo das estruturas (R$)", round(plan.total_cost, 2) if plan.total_cost is not None else "—"),
        ("Área ocupada pelas estruturas (m²)", round(plan.footprint_m2, 1)),
        ("Uso das fileiras (%)", round(plan.row_use or 0, 1)),
        ("Bins necessárias / alocadas", f"{plan.bins_needed} / {plan.bins_placed}"),
        ("Ocupação dos níveis atribuídos (%)", round(plan.fill or 0, 1)),
        ("Níveis: total / com tipo de bin / em uso", f"{plan.levels_total} / {plan.levels_assigned} / {plan.levels_used}"),
        ("Solver", f"{plan.solver.get('status', '')} ({plan.solver.get('time', 0):.1f}s)"),
    ]
    for r in rows:
        ws.append(list(r))
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 40
    if plan.notes:
        ws.append([])
        ws.append(["Observações"])
        for n in plan.notes:
            ws.append([n])

    table(wb.create_sheet("Estruturas"),
          ["Código", "Nome", "Quantidade", "Frente (cm)", "Profundidade (cm)", "Altura (cm)", "Níveis", "Vão (cm)",
           "Custo unit. (R$)", "Custo total (R$)", "Metros de fileira", "Área (m²)", "Máx. que cabe na planta"],
          [(s.struct.code, s.struct.name, s.qty, s.struct.width, s.struct.depth, s.struct.height, s.struct.levels,
            s.struct.level_h, s.struct.cost, s.cost, round(s.front_m, 2), round(s.footprint_m2, 2), s.floor_max)
           for s in plan.structs if s.qty or s.struct.mode != "off"])
    table(wb.create_sheet("Bins"),
          ["Tipo", "Necessárias", "Alocadas", "Falta", "Capacidade nos níveis", "Ocupação (%)", "Bin pai",
           "Bins pai a comprar"],
          [(t.spec.code, t.needed, t.placed, t.shortage, t.capacity, round(t.fill or 0, 1), t.parent_code,
            t.parents or "") for t in plan.types])
    table(wb.create_sheet("Endereços"),
          ["Endereço", "Corredor", "Estante", "Nível", "Posição", "Estrutura", "Tipo de bin", "Bins", "SKU",
           "Produto", "Unidades", "Peso (kg)"],
          [(p.address, p.aisle, p.bay, p.level, p.position, p.struct_code,
            types[p.type_id].code if p.type_id in types else "?", p.bins, p.sku, p.name, p.units, round(p.weight, 2))
           for p in plan.positions])
    table(wb.create_sheet("SKUs"),
          ["SKU", "Produto", "Material", "Quantidade", "Bins", "Situação", "Observação"],
          [(d.sku, d.name, d.material, d.units,
            ", ".join(f"{n}× {types[t].code}" for t, n in (d.bins or {}).items()), d.status_label, d.note)
           for d in plan.demands])
    table(wb.create_sheet("Níveis"),
          ["Corredor", "Estante", "Estrutura", "Nível", "Tipo de bin", "Capacidade", "Em uso", "Carga (kg)"],
          [(m.aisle, m.bay, m.struct.code, lv.n, types[lv.type_id].code if lv.type_id in types else "livre", lv.cap,
            lv.used, round(lv.load, 1)) for m in plan.modules for lv in m.levels])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Gerar o cadastro da filial
# --------------------------------------------------------------------------- #
def create_branch(db: Session, plan: SitePlan, code: str, name: str, wh_code: str, wh_name: str,
                  link_skus: bool = True) -> tuple[Branch, dict]:
    """Cria filial (+ armazém opcional), níveis com vão e capacidade, endereços e as bins de cada endereço.
    Com `link_skus`, cada SKU fica vinculado ao(s) seu(s) endereço(s) por um registro de saldo ZERADO (o endereço
    aparece ocupado pelo SKU previsto). Não muda o endereço padrão dos produtos que já têm saldo em outra filial."""
    from services.bin_content_service import refresh_contents
    from services.stock_service import refresh_occupancy

    pids = dict(db.execute(select(Product.sku, Product.id)).all()) if link_skus else {}
    new_ids: list[int] = []
    br = Branch(code=code, name=name)
    db.add(br)
    wh = None
    if wh_code:
        wh = db.scalar(select(Warehouse).where(Warehouse.code == wh_code))
        if wh is None:
            wh = Warehouse(code=wh_code, name=wh_name or wh_code)
            db.add(wh)
        br.warehouses.append(wh)
    db.flush()
    stats = {"levels": 0, "locations": 0, "bins": 0}
    for m in plan.modules:
        for lv in m.levels:
            sl = ShelfLevel(branch_id=br.id, warehouse_id=wh.id if wh else None, aisle=m.aisle, bay=m.bay,
                            level=str(lv.n), height_cm=Decimal(str(m.struct.level_h)),
                            notes=f"Estrutura {m.struct.code}")
            db.add(sl)
            db.flush()
            stats["levels"] += 1
            if lv.type_id is not None and lv.cap:
                db.add(LevelCapacity(shelf_level_id=sl.id, bin_type_id=lv.type_id, max_bins=lv.cap))
            for p in lv.positions:
                loc = StorageLocation(branch_id=br.id, warehouse_id=wh.id if wh else None, aisle=p.aisle, bay=p.bay,
                                      level=p.level, position=p.position, compartment="", address=p.address,
                                      is_occupied=False)
                db.add(loc)
                db.flush()
                db.add(LocationBin(location_id=loc.id, bin_type_id=p.type_id, quantity=p.bins))
                if p.sku in pids:
                    db.add(StockItem(product_id=pids[p.sku], location_id=loc.id, quantity=Decimal("0")))
                new_ids.append(loc.id)
                stats["locations"] += 1
                stats["bins"] += p.bins
    db.flush()
    refresh_occupancy(db, new_ids)
    refresh_contents(db, new_ids)
    db.flush()
    return br, stats
