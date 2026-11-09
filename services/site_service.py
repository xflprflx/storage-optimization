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
    Obstacle,
    SECTOR_PLACES,
    SectorSpec,
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
    "building": {k: d for k, _, d, _ in BUILDING_FIELDS} | {"orientation": "auto", "end_caps": "1", "cap_align": "row", "address_mode": "aisle", "serpentine": "",
                                                          "zones": [], "obstacles": [], "sectors": []},
    "structures": {},
    "demand": {"source": "catalog", "branch_id": "", "warehouse_id": "", "sku": "", "material": "", "basis": "max", "growth": "0",
               "factor": "100"},
    "params": {"objective": "modules", "bin_objective": "min_volume", "fill_limit": "85", "rotation": "upright",
               "single_type": "1", "weight": "1", "level_fill": "100", "top_gap_cm": "5", "side_gap_cm": "0",
               "types": [], "time_limit": "15", "tl_unlimited": "", "priority": ["", "", ""]},
    # posição dos SKUs: popularidade (auto = pedidos > coluna Saída), medida, ponto de referência, peso da zona de
    # ouro (metros de caminhada que ela vale) e itens que saem juntos lado a lado
    "slotting": {"on": "1", "source": "auto", "metric": "lines", "ref": "auto", "ref_x": "", "ref_y": "",
                 "golden_m": "5", "affinity": "1", "walk_m": ""},
}
SLOT_SOURCES = {"auto": "Automático (histórico de pedidos; senão a coluna Saída)", "orders": "Histórico de pedidos",
                "column": "Coluna Saída da aba SKUs", "max": "Quantidade planejada (max level) — aproximação"}
SLOT_METRICS = {"lines": "Linhas de pedido (visitas ao endereço)", "units": "Unidades"}


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
    out["orders"] = dict(cfg.get("orders") or {})  # resumo do histórico de pedidos (popularidade e pares)
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
    cfg["building"]["address_mode"] = "row" if form.get("b_address_mode") == "row" else "aisle"
    cfg["building"]["serpentine"] = on("b_serpentine")
    zones = []
    for zi in (form.getlist("z_idx") if hasattr(form, "getlist") else []):
        ori = form.get(f"z_ori_{zi}")
        sts = form.getlist(f"z_st_{zi}") if hasattr(form, "getlist") else []
        zones.append({"name": str(form.get(f"z_name_{zi}") or "").strip()[:40],
                      "length_m": str(form.get(f"z_len_{zi}") or "").strip(),
                      "orientation": ori if ori in ZONE_ORIENTATIONS else "along",
                      "aisle_m": str(form.get(f"z_aisle_{zi}") or "").strip(),
                      "cross_aisles": str(form.get(f"z_cross_{zi}") or "").strip(),
                      "x_m": str(form.get(f"z_x_{zi}") or "").strip(),
                      "y_m": str(form.get(f"z_y_{zi}") or "").strip(),
                      "width_m": str(form.get(f"z_wid_{zi}") or "").strip(),
                      "structures": [x for x in sts if str(x).isdigit()]})
    cfg["building"]["zones"] = zones
    obs = []
    for oi in (form.getlist("o_idx") if hasattr(form, "getlist") else []):
        row = {k: str(form.get(f"o_{k}_{oi}") or "").strip() for k in ("name", "x_m", "y_m", "length_m", "width_m")}
        if any(row[k] for k in ("x_m", "y_m", "length_m", "width_m")):
            row["name"] = row["name"][:40]
            obs.append(row)
    cfg["building"]["obstacles"] = obs
    secs = []
    for si in (form.getlist("sec_idx") if hasattr(form, "getlist") else []):
        row = {k: str(form.get(f"sec_{k}_{si}") or "").strip()
               for k in ("name", "area_m2", "min_side_m", "place", "x_m", "y_m", "length_m", "width_m")}
        if row["area_m2"] or row["name"] or row["length_m"]:
            row["name"] = row["name"][:40]
            row["place"] = row["place"] if row["place"] in SECTOR_PLACES else "auto"
            secs.append(row)
    cfg["building"]["sectors"] = secs
    src, met = form.get("slot_source"), form.get("slot_metric")
    cfg["slotting"] = {"on": on("slot_on"), "source": src if src in SLOT_SOURCES else "auto",
                       "metric": met if met in SLOT_METRICS else "lines",
                       "ref": str(form.get("slot_ref") or "auto")[:60],
                       "ref_x": str(form.get("slot_ref_x") or "").strip(), "ref_y": str(form.get("slot_ref_y") or "").strip(),
                       "golden_m": str(form.get("slot_golden_m") or "").strip(), "affinity": on("slot_affinity"),
                       "walk_m": str(form.get("slot_walk_m") or "").strip()}
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
    p["tl_unlimited"] = on("p_tl_unlimited")
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
                 cap_align=bc.get("cap_align") if bc.get("cap_align") in CAP_ALIGN else "row",
                 address_mode="row" if bc.get("address_mode") == "row" else "aisle",
                 serpentine=bool(bc.get("serpentine")))
    for i, zc in enumerate(bc.get("zones") or [], start=1):
        name = zc.get("name") or f"Zona {i}"
        ln = _num(zc.get("length_m"), f"{name}: comprimento", errors, None, lo=0.5)
        al = _num(zc.get("aisle_m"), f"{name}: largura do corredor", errors, None, lo=0.5)
        cr = _num(zc.get("cross_aisles"), f"{name}: corredores transversais", errors, 0, lo=0)
        zx = _num(zc.get("x_m"), f"{name}: X", errors, None, lo=0)
        zy = _num(zc.get("y_m"), f"{name}: Y", errors, None, lo=0)
        zw = _num(zc.get("width_m"), f"{name}: largura em Y", errors, None, lo=0.5)
        if (zx is None) != (zy is None):
            errors.append(f"{name}: informe X e Y juntos (ou deixe os dois vazios para empilhar a zona).")
        b.zones.append(ZoneSpec(length_m=ln, orientation=zc.get("orientation") if zc.get("orientation") in ZONE_ORIENTATIONS
                                else "along", aisle_m=al, cross_aisles=int(cr or 0),
                                structures={int(x) for x in zc.get("structures") or [] if str(x).isdigit()} or None,
                                name=name, x_m=zx, y_m=zy, width_m=zw))
    for i, oc in enumerate(bc.get("obstacles") or [], start=1):
        name = oc.get("name") or f"Obstáculo {i}"
        vals_o = {k: _num(oc.get(k), f"{name}: {lab}", errors, None, lo=0, required=True)
                  for k, lab in (("x_m", "X"), ("y_m", "Y"), ("length_m", "comprimento"), ("width_m", "largura"))}
        if any(v is None for v in vals_o.values()):
            continue
        if vals_o["length_m"] <= 0 or vals_o["width_m"] <= 0:
            errors.append(f"{name}: comprimento e largura devem ser maiores que zero.")
            continue
        if b.length_m and b.width_m and (vals_o["x_m"] + vals_o["length_m"] > b.length_m + 1e-6
                                         or vals_o["y_m"] + vals_o["width_m"] > b.width_m + 1e-6):
            errors.append(f"{name}: fica fora do imóvel ({b.length_m:g} × {b.width_m:g} m).")
            continue
        b.obstacles.append(Obstacle(name, **vals_o))
    for i, sc in enumerate(bc.get("sectors") or [], start=1):
        name = sc.get("name") or f"Setor {i}"
        place = sc.get("place") if sc.get("place") in SECTOR_PLACES else "auto"
        if place == "fixed":
            v = {k: _num(sc.get(k), f"{name}: {lab}", errors, None, lo=0, required=True)
                 for k, lab in (("x_m", "X"), ("y_m", "Y"), ("length_m", "comprimento"), ("width_m", "largura"))}
            if any(x is None for x in v.values()):
                continue
            if v["length_m"] <= 0 or v["width_m"] <= 0:
                errors.append(f"{name}: comprimento e largura devem ser maiores que zero.")
                continue
            if b.length_m and b.width_m and (v["x_m"] + v["length_m"] > b.length_m + 1e-6
                                             or v["y_m"] + v["width_m"] > b.width_m + 1e-6):
                errors.append(f"{name}: fica fora do imóvel ({b.length_m:g} × {b.width_m:g} m).")
                continue
            b.sectors.append(SectorSpec(name, v["length_m"] * v["width_m"], 0.0, "fixed", **v))
            continue
        area = _num(sc.get("area_m2"), f"{name}: área", errors, None, lo=0.5, required=True)
        side = _num(sc.get("min_side_m"), f"{name}: lado mínimo", errors, 2.0, lo=0)
        if area is None:
            continue
        b.sectors.append(SectorSpec(name, area, side if side is not None else 2.0, place))
    positioned = [z for z in b.zones if z.positioned]
    if positioned:  # zonas em qualquer lugar do imóvel
        if len(positioned) != len(b.zones):
            errors.append("Zonas posicionadas: informe X e Y em todas as zonas (ou em nenhuma, para empilhá-las a partir "
                          "da frente com avenidas).")
        else:
            from services.site_planner import _overlap, zone_rects

            for z in b.zones:
                if not z.length_m:
                    errors.append(f"{z.name}: zona posicionada precisa do comprimento em X.")
                elif b.length_m and b.width_m and (z.x_m >= b.length_m or z.y_m >= b.width_m
                                                   or z.x_m + z.length_m > b.length_m + 1e-6
                                                   or (z.width_m and z.y_m + z.width_m > b.width_m + 1e-6)):
                    errors.append(f"{z.name}: fica fora do imóvel ({b.length_m:g} × {b.width_m:g} m).")
            if not errors and b.length_m and b.width_m:
                rects = zone_rects(b)
                for i in range(len(rects)):
                    for j in range(i + 1, len(rects)):
                        if _overlap(rects[i], rects[j]):
                            errors.append(f"As zonas {b.zones[i].name} e {b.zones[j].name} se sobrepõem.")
    else:
        if len(b.zones) == 1:
            errors.append("Com zonas empilhadas, informe ao menos duas (uma zona só é o mesmo que não usar zonas); "
                          "para uma zona só, informe X e Y.")
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
        # sem limite (None): a solução do solver a qualquer custo — cada critério vai até provar o ótimo
        time_limit=None if p.get("tl_unlimited") else (_num(p.get("time_limit"), "Tempo limite", errors, 15, 1, 3600) or 15),
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
                                 float(st.cost) if st.cost is not None else None, **md,
                                 golden_from=float(st.golden_from) if st.golden_from is not None else None,
                                 golden_to=float(st.golden_to) if st.golden_to is not None else None,
                                 base=float(st.first_level_cm or 0), kind=st.kind or "",
                                 level_hs=st.vaos() if st.level_heights else None))
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
    # min/max level: política de estoque da filial de referência (todas as filiais = soma das políticas)
    levels: dict[int, float] = {}
    if basis != "stock":
        from models import StockPolicy

        col = StockPolicy.min_level if basis == "min" else StockPolicy.max_level
        lq = select(StockPolicy.product_id, func.sum(col)).where(col.is_not(None))
        if scoped:
            lq = lq.where(StockPolicy.branch_id == int(bid))
        levels = {pid: float(v or 0) for pid, v in db.execute(lq.group_by(StockPolicy.product_id))}
    out, no_level = [], 0
    for p in db.scalars(stmt.order_by(Product.sku)):
        q = stock.get(p.id, 0.0)
        if basis != "stock":
            lvl = levels.get(p.id)
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
    caps = cfg["demand"].get("caps") or {}
    for d in demands:  # limites por SKU (sugestão de redução da demanda)
        if d.sku in caps:
            d.cap = int(caps[d.sku])
    if caps:
        notes.append(f"{sum(1 for d in demands if d.cap is not None)} SKU(s) com limite de quantidade (redução da demanda).")
    prm.slotting, slot_note = slotting_spec(cfg, demands)
    if slot_note:
        notes.append(slot_note)
    plan = plan_site(b, structs, types, demands, prm, parents_of(db, types))
    plan.notes = notes + plan.notes
    return plan, []


def popularity(cfg: dict, demands: list[Demand]) -> tuple[dict, str, str]:
    """Popularidade por SKU conforme a fonte escolhida: ({sku: valor}, rótulo da fonte, fonte usada)."""
    sl, orders = cfg.get("slotting") or {}, cfg.get("orders") or {}
    src, metric = sl.get("source") or "auto", sl.get("metric") or "lines"
    col = {it["sku"]: it["out"] for it in cfg.get("items") or [] if it.get("out")}
    if src in ("auto", "orders") and orders.get("skus"):
        k = 0 if metric == "lines" else 1
        return ({s: v[k] for s, v in orders["skus"].items()},
                f"histórico de pedidos ({orders.get('orders', 0)} pedidos, "
                f"{'linhas de pedido' if metric == 'lines' else 'unidades'})", "orders")
    if src in ("auto", "column") and col:
        return col, "coluna Saída da aba SKUs", "column"
    if src == "max":
        return {d.sku: d.units for d in demands}, "quantidade planejada (max level)", "max"
    return {}, "", ""


def slotting_spec(cfg: dict, demands: list[Demand]) -> tuple[Optional[dict], str]:
    """Popularidade nos SKUs e parâmetros do slotting para o planejador (+ aviso quando não há dados)."""
    sl = cfg.get("slotting") or {}
    pop, label, used = popularity(cfg, demands)
    for d in demands:
        d.pop = float(pop.get(d.sku) or 0.0) if pop else None
    ref = sl.get("ref") or "auto"
    ref_xy = None
    if ref == "xy":
        try:
            ref_xy = (float(parse_decimal(sl.get("ref_x")) or 0), float(parse_decimal(sl.get("ref_y")) or 0))
        except (ValueError, TypeError):
            ref_xy = None
    try:
        golden_m = float(parse_decimal(str(sl.get("golden_m") or "0")) or 0)
    except ValueError:
        golden_m = 0.0
    from services.slotting import affinity as pair_affinity

    orders = cfg.get("orders") or {}
    affinity, counts = pair_affinity(orders) if sl.get("affinity") and used == "orders" else ({}, {})
    try:
        walk_m = float(parse_decimal(str(sl.get("walk_m") or "")) or 0) or None
    except ValueError:
        walk_m = None
    spec = {"on": bool(sl.get("on")), "ref": ref, "ref_xy": ref_xy, "golden_m": golden_m, "affinity": affinity,
            "walk_golden_m": walk_m,
            "source": label, "metric": sl.get("metric") or "lines", "pair_counts": counts}
    note = ""
    if sl.get("on") and not pop and (sl.get("source") or "auto") != "auto":  # no automático, o cartão já explica
        note = ("Posição dos SKUs: a fonte de popularidade escolhida não tem dados (aba Pedidos ou coluna Saída) — os SKUs "
                "ficam na ordem de material/código. Importe os dados ou escolha a aproximação pelo max level.")
    return spec, note


def suggest_reduction(db: Session, cfg: dict, steps: int = 7, per_run_s: float = 4.0) -> dict:
    """Quanto da demanda cabe com as estruturas e o imóvel atuais.
    - fator: busca binária do maior fator de demanda (relativo ao atual) sem falta de espaço;
    - por SKU: no plano atual, quanto coube de cada SKU que ficou sem lugar (limite sugerido);
    - maiores consumidores: SKUs que mais ocupam volume de bins (candidatos a revisão)."""
    import copy
    import math

    base, errors = run_project(db, cfg)
    if errors or base is None:
        return {"error": " ".join(errors) or "Não foi possível rodar o plano."}
    f0 = float(parse_decimal(str(cfg["demand"].get("factor") or "100")) or 100)
    out = {"factor": f0, "shortage": base.shortage, "bins_needed": base.bins_needed, "plan": base, "fit_factor": None,
           "cuts": [], "top": [], "limits": []}
    # maiores consumidores (volume das bins de cada SKU)
    vol = {t.spec.id: (t.spec.volume_cm3 or 0) / 1000.0 for t in base.types}
    total = sum(n * vol.get(t, 0) for d in base.demands for t, n in (d.bins or {}).items()) or 1
    top = sorted(((sum(n * vol.get(t, 0) for t, n in (d.bins or {}).items()), d) for d in base.demands if d.bins),
                 key=lambda x: -x[0])[:15]
    out["top"] = [{"sku": d.sku, "name": d.name, "units": d.units, "bins": d.total_bins, "volume_l": v,
                   "share": v / total * 100} for v, d in top]
    for s in base.structs:  # estruturas que limitam (quantidade fixa ou máximo atingido)
        st = s.struct
        if st.mode == "fixed" or (st.mode == "auto" and st.max_qty is not None and s.qty >= st.max_qty):
            out["limits"].append(f"{st.code}: {'quantidade fixa' if st.mode == 'fixed' else 'máximo'} de "
                                 f"{st.qty if st.mode == 'fixed' else st.max_qty} módulo(s) atingido")
        elif s.floor_max and s.qty >= s.floor_max:
            out["limits"].append(f"{st.code}: a planta comporta no máximo {s.floor_max} módulo(s)")
    if not base.shortage:
        return out
    # cortes por SKU: o que coube de cada SKU sem lugar
    placed = {}
    for p in base.positions:
        placed[p.sku] = placed.get(p.sku, 0) + p.units
    for d in base.demands:
        if d.status == "falta":
            out["cuts"].append({"sku": d.sku, "name": d.name, "units": d.units, "fits": placed.get(d.sku, 0),
                                "missing_bins": d.total_bins - d.placed_bins})
    # fator que cabe (relativo ao atual)
    lo, hi = 0.0, 1.0
    for _ in range(steps):
        mid = (lo + hi) / 2
        c2 = copy.deepcopy(cfg)
        c2["demand"]["factor"] = f"{f0 * mid:.4f}"
        c2["params"]["time_limit"] = str(min(float(parse_decimal(str(cfg["params"].get("time_limit") or 15)) or 15),
                                             per_run_s))
        c2["params"]["tl_unlimited"] = ""  # a busca do fator roda várias vezes: sempre com tempo curto
        plan, errs = run_project(db, c2)
        if plan is not None and not errs and not plan.errors and plan.shortage == 0:
            lo = mid
        else:
            hi = mid
    out["fit_factor"] = math.floor(f0 * lo * 10) / 10 if lo > 0 else None
    return out


# --------------------------------------------------------------------------- #
# Gerar o cadastro da filial
# --------------------------------------------------------------------------- #
DEFAULT_LOCATION_MODES = {  # o endereço padrão é da política de estoque da filial: as outras filiais não mudam
    "empty": "Definir o endereço padrão de cada produto na filial nova (o 1º endereço dele no plano)",
    "none": "Não definir o endereço padrão",
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
    pending_levels: dict[str, dict] = {}  # sku -> min/max da filial nova (gravados na política depois)
    for sku in skus:
        it, d = by_item.get(sku, {}), demand.get(sku)
        vals = {"name": (it.get("name") or (d.name if d else "") or sku)[:255],
                "material_type": (it.get("material") or (d.material if d else "") or "")[:60],
                "height": dec(it.get("h") if it.get("h") is not None else (d.unit.h if d else None)),
                "width": dec(it.get("w") if it.get("w") is not None else (d.unit.w if d else None)),
                "length": dec(it.get("l") if it.get("l") is not None else (d.unit.l if d else None)),
                "weight": dec(it.get("wt") if it.get("wt") is not None else (d.unit.weight if d else None))}
        levels = {"min_level": dec(it.get("min")), "max_level": dec(it.get("max"))}  # política da filial nova
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
                   "weight": "wt"}
            for k, v in vals.items():
                informed = it.get(src[k]) not in (None, "")  # só o que veio preenchido no arquivo
                if informed and getattr(pr, k) != v:
                    setattr(pr, k, v)
                    changed = True
            stats["products_updated"] += int(changed)
        if any(v is not None for v in levels.values()):
            pending_levels[sku] = levels
    db.flush()
    # política de estoque da filial nova: min/max level do arquivo (o cadastro do produto é o mesmo das outras filiais)
    from models import StockPolicy

    policies: dict[str, StockPolicy] = {}

    def policy_of(sku: str) -> StockPolicy:
        if sku not in policies:
            policies[sku] = StockPolicy(branch_id=br.id, product_id=products[sku].id)
            db.add(policies[sku])
        return policies[sku]

    for sku, lv in pending_levels.items():
        if sku in products:
            pol = policy_of(sku)
            pol.min_level, pol.max_level = lv["min_level"], lv["max_level"]
            stats["policies"] = stats.get("policies", 0) + 1

    # ---- estantes, níveis, endereços e bins ----
    new_ids: list[int] = []
    sku_locs: dict[str, list[tuple[int, int]]] = {}  # sku -> [(endereço, unidades planejadas)]
    for m in plan.modules:
        db.add(BayStructure(branch_id=br.id, warehouse_id=wh.id if wh else None, aisle=m.aisle, bay=m.bay,
                            structure_type_id=m.struct.id))
        stats["bays"] += 1
        for lv in m.levels:
            sl = ShelfLevel(branch_id=br.id, warehouse_id=wh.id if wh else None, aisle=m.aisle, bay=m.bay,
                            level=str(lv.n), height_cm=Decimal(str(m.struct.vao(lv.n))),
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
            pol = policy_of(sku)  # filial nova: a política começa sem endereço padrão
            if default_mode in ("all", "empty") and pol.default_location_id is None:
                pol.default_location_id = locs[0][0]
                stats["defaults"] += 1
    db.flush()
    refresh_occupancy(db, new_ids)
    refresh_contents(db, new_ids)
    # planta da filial: a do plano (o simulador de trajeto, a caminhada da posição pela saída e o 3D usam os
    # corredores do projeto, sem precisar desenhar)
    from models import BranchLayout
    from services.branch_layout import from_plan

    data = from_plan(plan)
    if data["rows"]:
        db.add(BranchLayout(branch_id=br.id, warehouse_id=wh.id if wh else None, data=data))
        stats["layout_rows"] = len(data["rows"])
    db.flush()
    return br, stats
