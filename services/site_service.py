"""Projeto de filial nova: configuração (JSON do projeto), carga da demanda/estruturas do banco, execução,
exportação para Excel e geração do cadastro da filial (endereços, níveis e bins) a partir do plano."""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    BayStructure,
    BinType,
    Branch,
    LevelCapacity,
    LocationBin,
    Product,
    ShelfLevel,
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
    CAP_ALIGN,
    ORIENTATIONS,
    SITE_OBJECTIVES,
    ZONE_ORIENTATIONS,
    STRUCT_MODES,
    Building,
    Demand,
    SiteParams,
    SitePlan,
    StructureSpec,
    ZoneSpec,
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
    ("avenue_m", "Largura das avenidas entre zonas (m)", "2", 0),
)
DEFAULT_CONFIG = {
    "building": {k: d for k, _, d, _ in BUILDING_FIELDS} | {"orientation": "auto", "end_caps": "1", "cap_align": "row",
                                                          "zones": []},
    "structures": {},
    "demand": {"source": "catalog", "branch_id": "", "warehouse_id": "", "sku": "", "material": "", "basis": "max", "growth": "0",
               "factor": "100"},
    "params": {"objective": "modules", "bin_objective": "min_volume", "fill_limit": "85", "rotation": "upright",
               "single_type": "1", "weight": "1", "level_fill": "100", "top_gap_cm": "5", "side_gap_cm": "0",
               "types": [], "time_limit": "15", "priority": ["", "", ""]},
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
    cfg["building"]["end_caps"] = on("b_end_caps")
    cfg["building"]["cap_align"] = form.get("b_cap_align") if form.get("b_cap_align") in CAP_ALIGN else "row"
    zones = []
    for zi in (form.getlist("z_idx") if hasattr(form, "getlist") else []):
        ori = form.get(f"z_ori_{zi}")
        sts = form.getlist(f"z_st_{zi}") if hasattr(form, "getlist") else []
        zones.append({"name": str(form.get(f"z_name_{zi}") or "").strip()[:40],
                      "length_m": str(form.get(f"z_len_{zi}") or "").strip(),
                      "orientation": ori if ori in ZONE_ORIENTATIONS else "along",
                      "aisle_m": str(form.get(f"z_aisle_{zi}") or "").strip(),
                      "cross_aisles": str(form.get(f"z_cross_{zi}") or "").strip(),
                      "structures": [x for x in sts if str(x).isdigit()]})
    cfg["building"]["zones"] = zones
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
    p["priority"] = [form.get(f"p_prio{i}") or "" for i in (2, 3, 4)]
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
                 cross_aisle_m=vals["cross_aisle_m"] or 0, orientation=bc.get("orientation") or "auto",
                 end_caps=bool(bc.get("end_caps")), avenue_m=vals["avenue_m"] or 0,
                 cap_align=bc.get("cap_align") if bc.get("cap_align") in CAP_ALIGN else "row")
    for i, zc in enumerate(bc.get("zones") or [], start=1):
        name = zc.get("name") or f"Zona {i}"
        ln = _num(zc.get("length_m"), f"{name}: comprimento", errors, None, lo=0.5)
        al = _num(zc.get("aisle_m"), f"{name}: largura do corredor", errors, None, lo=0.5)
        cr = _num(zc.get("cross_aisles"), f"{name}: corredores transversais", errors, 0, lo=0)
        b.zones.append(ZoneSpec(length_m=ln, orientation=zc.get("orientation") if zc.get("orientation") in ZONE_ORIENTATIONS
                                else "along", aisle_m=al, cross_aisles=int(cr or 0),
                                structures={int(x) for x in zc.get("structures") or [] if str(x).isdigit()} or None,
                                name=name))
    if len(b.zones) == 1:
        errors.append("Com zonas, informe ao menos duas (uma zona só é o mesmo que não usar zonas).")
    if b.zones and b.avenue_m <= 0:
        errors.append("Com zonas, a largura das avenidas entre elas deve ser maior que zero.")
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
        priority=[c for c in (p.get("priority") or []) if c in SITE_OBJECTIVES] or None,
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
# Gerar o cadastro da filial
# --------------------------------------------------------------------------- #
DEFAULT_LOCATION_MODES = {
    "empty": "Só nos produtos sem endereço padrão (novos ou ainda sem endereço)",
    "all": "Em todos os produtos do plano (passam a ter o endereço padrão na filial nova)",
    "none": "Não alterar o endereço padrão",
}


def create_branch(db: Session, plan: SitePlan, code: str, name: str, wh_code: str, wh_name: str,
                  link_skus: bool = True, items: Optional[list] = None, create_products: bool = True,
                  update_products: bool = False, default_mode: str = "empty",
                  launch_stock: bool = False) -> tuple[Branch, dict]:
    """Cria a filial a partir do plano: armazém (opcional), estantes com a estrutura, níveis com vão e capacidade,
    endereços com as bins e, para os SKUs:
      - `create_products`: cadastra os produtos que ainda não existem (nome, material, dimensões, peso, min/max level,
        vindos do arquivo do projeto ou da demanda);
      - `update_products`: atualiza esses dados nos produtos que já existem (só com valores informados no arquivo);
      - `link_skus`: atribui cada SKU ao(s) seu(s) endereço(s) por um registro de saldo (zerado ou, com `launch_stock`,
        o saldo do arquivo dividido entre os endereços do SKU na proporção do plano);
      - `default_mode`: endereço padrão do produto = 1º endereço dele na filial nova ("empty": só para quem não tem;
        "all": todos; "none": não altera)."""
    from services.bin_content_service import refresh_contents
    from services.stock_service import refresh_occupancy

    by_item = {it["sku"]: it for it in (items or [])}
    demand = {d.sku: d for d in plan.demands}
    stats = {"levels": 0, "locations": 0, "bins": 0, "bays": 0, "products_created": 0, "products_updated": 0,
             "assigned": 0, "defaults": 0, "stock_units": 0.0, "skus_without_product": 0, "unplaced": 0}
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

    # ---- produtos ----
    placed = list(dict.fromkeys(p.sku for p in plan.positions))
    # SKUs do arquivo que ficaram fora do plano (sem dimensões, sem quantidade...) também são cadastrados, sem endereço
    skus = placed + [s for s in by_item if s not in set(placed)]
    products = {}
    for i in range(0, len(skus), 900):
        for pr in db.scalars(select(Product).where(Product.sku.in_(skus[i:i + 900]))):
            products[pr.sku] = pr
    dec = lambda v: Decimal(str(v)) if v is not None else None  # noqa: E731
    sku_set = set(placed)
    for sku in skus:
        it, d = by_item.get(sku, {}), demand.get(sku)
        vals = {"name": (it.get("name") or (d.name if d else "") or sku)[:255],
                "material_type": (it.get("material") or (d.material if d else "") or "")[:60],
                "height": dec(it.get("h") if it.get("h") is not None else (d.unit.h if d else None)),
                "width": dec(it.get("w") if it.get("w") is not None else (d.unit.w if d else None)),
                "length": dec(it.get("l") if it.get("l") is not None else (d.unit.l if d else None)),
                "weight": dec(it.get("wt") if it.get("wt") is not None else (d.unit.weight if d else None)),
                "min_level": dec(it.get("min")), "max_level": dec(it.get("max"))}
        pr = products.get(sku)
        if pr is None:
            if not create_products:
                stats["skus_without_product"] += 1
                continue
            pr = Product(sku=sku[:60], **vals)
            db.add(pr)
            products[sku] = pr
            stats["products_created"] += 1
            stats["unplaced"] += int(sku not in sku_set)
        elif update_products and it:
            changed = False
            src = {"name": "name", "material_type": "material", "height": "h", "width": "w", "length": "l",
                   "weight": "wt", "min_level": "min", "max_level": "max"}
            for k, v in vals.items():
                informed = it.get(src[k]) not in (None, "")  # só o que veio preenchido no arquivo
                if informed and getattr(pr, k) != v:
                    setattr(pr, k, v)
                    changed = True
            stats["products_updated"] += int(changed)
    db.flush()

    # ---- estantes, níveis, endereços e bins ----
    new_ids: list[int] = []
    sku_locs: dict[str, list[tuple[int, int]]] = {}  # sku -> [(endereço, unidades planejadas)]
    for m in plan.modules:
        db.add(BayStructure(branch_id=br.id, warehouse_id=wh.id if wh else None, aisle=m.aisle, bay=m.bay,
                            structure_type_id=m.struct.id))
        stats["bays"] += 1
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
                sku_locs.setdefault(p.sku, []).append((loc.id, p.units))
                new_ids.append(loc.id)
                stats["locations"] += 1
                stats["bins"] += p.bins

    # ---- atribuição dos produtos aos endereços ----
    if link_skus:
        for sku, locs in sku_locs.items():
            pr = products.get(sku)
            if pr is None:
                continue
            qty = [0.0] * len(locs)
            stock = (by_item.get(sku) or {}).get("stock") if launch_stock else None
            if stock:
                total = sum(u for _, u in locs) or 1
                left = float(stock)
                for i, (_, u) in enumerate(locs):  # proporcional ao planejado; o resto fica no último endereço
                    q = left if i == len(locs) - 1 else round(float(stock) * u / total, 4)
                    q = min(q, left)
                    qty[i], left = q, left - q
                stats["stock_units"] += float(stock)
            for (lid, _), q in zip(locs, qty):
                db.add(StockItem(product_id=pr.id, location_id=lid, quantity=Decimal(str(q))))
                stats["assigned"] += 1
            if default_mode == "all" or (default_mode == "empty" and pr.default_location_id is None):
                pr.default_location_id = locs[0][0]
                stats["defaults"] += 1
    db.flush()
    refresh_occupancy(db, new_ids)
    refresh_contents(db, new_ids)
    db.flush()
    return br, stats
