"""
Plano detalhado de implantação da filial nova (projeto greenfield), numa estrutura única usada pela tela, pelo Excel
e pelo PDF:

  - cabeçalho: projeto, data, imóvel, demanda, regras e ordem de prioridade dos critérios
  - indicadores do plano
  - lista de compras (estruturas e bins, com custo)
  - passo a passo (checklist) em fases: 1. compras, 2. montagem das estantes, 3. configuração dos níveis
    e etiquetagem dos endereços, 4. abastecimento (bins e produtos por endereço), 5. pendências
  - montagem: cada estante com estrutura, fileira, lado do corredor e posição na planta
  - níveis: tipo de bin, capacidade, uso e carga de cada nível
  - endereços: bins, SKU e unidades de cada endereço
  - SKUs: bins escolhidas e situação
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from services.site_planner import DEMAND_STATUS, ORIENTATIONS, SITE_CRITERIA, STRUCT_MODES, SitePlan, struct_colors

PHASES = {
    1: "Compras",
    2: "Montagem das estantes",
    3: "Configuração dos níveis e etiquetagem",
    4: "Abastecimento (bins e produtos)",
    5: "Pendências",
}
PHASE_KIND = {1: "info", 2: "move", 3: "change", 4: "add", 5: "remove"}


def _fmt(v, places=0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{places}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _money(v) -> str:
    return "—" if v is None else "R$ " + _fmt(v, 2)


@dataclass
class Step:
    n: int
    phase: int
    group: str
    action: str
    where: str
    detail: str
    qty: str = ""

    @property
    def kind(self) -> str:
        return PHASE_KIND.get(self.phase, "info")

    @property
    def phase_label(self) -> str:
        return f"{self.phase}. {PHASES[self.phase]}"


@dataclass
class SiteReport:
    title: str
    project: str
    created: datetime
    scope: list  # [(rótulo, valor)] imóvel e demanda
    params: list  # [(rótulo, valor)] regras e critérios
    kpis: list = field(default_factory=list)  # [(rótulo, valor formatado, complemento)]
    purchases: list = field(default_factory=list)  # [{"kind", "code", "name", "qty", "unit", "total", "note"}]
    steps: list = field(default_factory=list)
    modules: list = field(default_factory=list)  # linhas da montagem
    levels: list = field(default_factory=list)  # linhas dos níveis
    addresses: list = field(default_factory=list)  # linhas dos endereços
    skus: list = field(default_factory=list)  # linhas dos SKUs
    warnings: list = field(default_factory=list)
    plan: Optional[SitePlan] = None

    @property
    def purchase_total(self) -> Optional[float]:
        vals = [p["total"] for p in self.purchases]
        return None if not vals or any(v is None for v in vals) else sum(vals)

    def steps_by_phase(self) -> list[tuple[str, list]]:
        out: dict[int, list] = {}
        for s in self.steps:
            out.setdefault(s.phase, []).append(s)
        return [(f"{k}. {PHASES[k]}", v) for k, v in sorted(out.items())]


def build_report(plan: SitePlan, project_name: str, cfg: dict, types: dict, bin_names: Optional[dict] = None) -> SiteReport:
    """`types` = {id: BinSpec}; `bin_names` = {id: nome do tipo} (opcional)."""
    b, fl = plan.building, plan.floor
    d, p = cfg.get("demand", {}), cfg.get("params", {})
    code = lambda t: types[t].code if t in types else "?"  # noqa: E731
    bin_names = bin_names or {}
    src = "Arquivo importado" if d.get("source") == "upload" else (
        "Cadastro de produtos" + (" / filial de referência" if d.get("branch_id") else " (todas as filiais)"))
    scope = [
        ("Imóvel", f"{_fmt(b.length_m, 2)} × {_fmt(b.width_m, 2)} m · pé-direito útil {_fmt(b.clear_height_m, 2)} m "
                   f"· {_fmt(b.area_m2, 0)} m²"),
        ("Corredores", f"{_fmt(b.aisle_m, 2)} m · afastamento das paredes {_fmt(b.wall_gap_m, 2)} m"),
        ("Faixas reservadas", f"frente {_fmt(b.front_reserved_m, 2)} m · fundo {_fmt(b.back_reserved_m, 2)} m"),
        ("Corredores transversais", f"{b.cross_aisles} × {_fmt(b.cross_aisle_m, 2)} m"),
        ("Cabeceiras", ("permitidas nas pontas com acesso" if b.end_caps else "não usar")
         + (f" · {plan.end_caps} módulo(s) girado(s)" if plan.end_caps else "")),
        ("Fileiras", (f"{len(fl.rows)} fileira(s) ao longo do {'comprimento' if fl.along == 'length' else 'largura'}"
                      f" ({ORIENTATIONS.get(b.orientation, b.orientation)}) · " if not fl.mixed else
                      f"{len(fl.rows)} fileira(s) em {len(fl.zones)} zonas · ")
         + f"{fl.real_aisles} corredor(es) · profundidade {_fmt(fl.depth, 0)} cm"),
    ] + ([("Zonas", " · ".join(
        f"{z.name}: {_fmt(z.cross_cm / 100 if z.rot else z.run_cm / 100, 2)} m, fileiras ao longo da "
        f"{'largura' if z.rot else 'comprimento'}, {len(z.rows)} fileira(s), corredor {_fmt(z.aisle_cm / 100, 2)} m"
        for z in fl.zones) + f" · avenidas de {_fmt(b.avenue_m, 2)} m")] if fl.mixed else []) + [
        ("Demanda", f"{src} · base {d.get('basis', '')} · fator {d.get('factor', '100')}% · crescimento "
                    f"{d.get('growth', '0')}%"),
        ("SKUs", f"{len(plan.demands)} SKU(s) · {sum(1 for x in plan.demands if x.status == 'ok')} alocado(s)"),
    ]
    order = [st["label"] for st in plan.solver.get("stages", [])]
    params = [
        ("Prioridade dos critérios", " > ".join(order) or SITE_CRITERIA.get(p.get("objective"), "")),
        ("Bins de cada SKU", p.get("bin_objective", "")),
        ("Enchimento máx. da bin", f"{p.get('fill_limit', '')}%"),
        ("Uso da frente do nível", f"{p.get('level_fill', '')}%"),
        ("Folgas", f"acima da bin {p.get('top_gap_cm', '')} cm · entre bins {p.get('side_gap_cm', '')} cm"),
        ("Rotação / tipo único / peso", f"{'qualquer' if p.get('rotation') == 'any' else 'altura na vertical'} · "
                                       f"{'1 tipo de bin por SKU' if p.get('single_type') else 'combina tipos'} · "
                                       f"{'respeita peso' if p.get('weight') else 'sem peso'}"),
        ("Estruturas", "; ".join(f"{s.struct.code}: {STRUCT_MODES[s.struct.mode].lower()}"
                                 + (f" {s.struct.qty}" if s.struct.mode == "fixed" else "")
                                 for s in plan.structs if s.struct.mode != "off") or "—"),
        ("Solver", f"{plan.solver.get('status', '')} em {_fmt(plan.solver.get('time', 0), 1)} s"),
    ]
    rep = SiteReport(title=f"Plano de implantação — {project_name}", project=project_name, created=datetime.now(),
                     scope=scope, params=params, plan=plan)
    rep.kpis = [
        ("Módulos de estrutura", _fmt(plan.total_modules), ", ".join(f"{s.qty}× {s.struct.code}" for s in plan.structs if s.qty)),
        ("Bins", _fmt(plan.bins_needed), ", ".join(f"{t.needed}× {t.spec.code}" for t in plan.types if t.needed)),
        ("Custo total", _money(plan.total_cost), f"estruturas {_money(plan.structures_cost)} + bins {_money(plan.bins_cost)}"
         + (f" · sem custo (R$ 0): {', '.join(dict.fromkeys(plan.missing_costs))}" if plan.missing_costs else "")),
        ("Endereços", _fmt(len(plan.positions)), f"{_fmt(plan.levels_total)} níveis em {len(plan.modules)} estantes"
         + (f" ({plan.end_caps} cabeceiras)" if plan.end_caps else "")),
        ("Ocupação dos níveis", (_fmt(plan.fill, 1) + "%") if plan.fill is not None else "—",
         f"{plan.levels_used} de {plan.levels_total} níveis em uso"),
        ("Ocupação das bins", (_fmt(plan.bin_fill, 1) + "%") if plan.bin_fill is not None else "—",
         "volume dos produtos ÷ volume útil das bins"),
        ("Uso das fileiras", (_fmt(plan.row_use, 1) + "%") if plan.row_use is not None else "—",
         f"{_fmt(plan.front_m, 1)} de {_fmt(fl.total_row_m, 1)} m · {_fmt(plan.footprint_m2, 1)} m² de estruturas"),
    ]

    # ---- compras ----
    for s in plan.structs:
        if s.qty:
            rep.purchases.append({"kind": "Estrutura", "code": s.struct.code, "name": s.struct.name, "qty": s.qty,
                                  "unit": s.struct.cost, "total": s.cost,
                                  "note": f"{_fmt(s.struct.width)} × {_fmt(s.struct.depth)} × {_fmt(s.struct.height)} cm, "
                                          f"{s.struct.levels} níveis × {_fmt(s.struct.level_h)} cm"})
    for t in plan.types:
        if not t.needed:
            continue
        if t.parent_code:
            rep.purchases.append({"kind": "Bin (inteira, para compartimentos)", "code": t.parent_code, "name": "",
                                  "qty": t.parents, "unit": t.parent_cost, "total": t.cost,
                                  "note": f"dividida em compartimentos {t.spec.code} ({t.needed} compartimentos)"})
        else:
            rep.purchases.append({"kind": "Bin", "code": t.spec.code, "name": bin_names.get(t.spec.id, ""),
                                  "qty": t.needed, "unit": t.unit_cost, "total": t.cost,
                                  "note": f"{_fmt(t.spec.h)} × {_fmt(t.spec.w)} × {_fmt(t.spec.l)} cm"})

    # ---- montagem / níveis / endereços ----
    side_lbl = {"L": "esquerdo", "R": "direito"}

    def side_of(m) -> str:
        if m.rotated:
            return f"cabeceira (girada 90°, de frente para {m.faces})"
        zn = m.zone
        if zn is not None and zn.aisles[m.row.aisle][1] <= 0:  # fileira de frente para a avenida/faixa
            return f"de frente para {zn.edge_names.get(m.row.aisle, 'a área livre')}"
        return side_lbl.get(m.row.side, m.row.side)

    for m in plan.modules:
        X, Y, _, _ = m.grect
        rep.modules.append({"bay": f"{m.aisle}-{m.bay}", "aisle": m.aisle, "struct": m.struct.code,
                            "zone": m.zone.name if m.zone is not None else "",
                            "row": m.row.index + 1, "side": side_of(m),
                            "x": X / 100.0, "y": Y / 100.0, "w": m.struct.width, "d": m.struct.depth,
                            "levels": m.struct.levels, "level_h": m.struct.level_h,
                            "config": ", ".join(f"N{lv.n}: {lv.cap}× {code(lv.type_id)}" if lv.type_id is not None
                                                else f"N{lv.n}: livre" for lv in m.levels)})
        for lv in m.levels:
            rep.levels.append({"bay": f"{m.aisle}-{m.bay}", "level": lv.n, "struct": m.struct.code,
                               "type": code(lv.type_id) if lv.type_id is not None else "livre", "cap": lv.cap,
                               "used": lv.used, "load": lv.load, "max_load": m.struct.max_load,
                               "positions": len(lv.positions), "golden": m.struct.is_golden(lv.n)})
    for pos in plan.positions:
        rep.addresses.append({"address": pos.address, "struct": pos.struct_code, "type": code(pos.type_id),
                              "bins": pos.bins, "sku": pos.sku, "name": pos.name, "units": pos.units,
                              "weight": pos.weight, "fill": pos.fill})
    for dm in plan.demands:
        rep.skus.append({"sku": dm.sku, "name": dm.name, "material": dm.material, "units": dm.units,
                         "bins": ", ".join(f"{n}× {code(t)}" for t, n in (dm.bins or {}).items()),
                         "status": dm.status_label, "note": dm.note, "ok": dm.status == "ok",
                         "fill": dm.bin_fill})
    rep.warnings = list(plan.errors) + list(plan.notes)

    # ---- passo a passo ----
    n = 0

    def add(phase, group, action, where, detail, qty=""):
        nonlocal n
        n += 1
        rep.steps.append(Step(n, phase, group, action, where, detail, qty))

    for pc in rep.purchases:
        add(1, pc["kind"], "Comprar", pc["code"], f"{pc['name'] or pc['code']} — {pc['note']}"
            + (f" · {_money(pc['total'])}" if pc["total"] is not None else ""), _fmt(pc["qty"]))
    add(1, "Etiquetas", "Providenciar", "Endereços",
        f"Etiquetas de {len(plan.positions)} endereço(s) e de {len(plan.modules)} estante(s)", _fmt(len(plan.positions)))
    for m in plan.modules:
        X, Y, _, _ = m.grect
        where = f"X = {_fmt(X / 100, 2)} m, Y = {_fmt(Y / 100, 2)} m"
        zone = f"{m.zone.name}, " if fl.mixed and m.zone is not None else ""
        if m.rotated:
            blk = m.zone.blocks[m.row.block] if m.zone is not None and m.zone.blocks else None
            kind = "dupla" if blk and len(blk.rows) > 1 else "simples"
            add(2, f"Corredor {m.aisle}", "Montar", f"Cabeceira {m.aisle}-{m.bay}",
                f"{m.struct.code} GIRADA 90° na ponta da fileira {kind} ({zone}fileiras "
                f"{', '.join(str(r + 1) for r in (blk.rows if blk else [m.row.index]))}), de frente para {m.faces}: "
                f"{where}; {m.struct.levels} níveis com vão de {_fmt(m.struct.level_h)} cm")
            continue
        s_txt = side_of(m)
        face = s_txt if s_txt.startswith("de frente") else f"lado {s_txt} do corredor {m.aisle}"
        add(2, f"Corredor {m.aisle}", "Montar", f"Estante {m.aisle}-{m.bay}",
            f"{m.struct.code} na fileira {m.row.index + 1} ({zone}{face}"
            f"{'; fileira ao longo da largura' if m.zone is not None and m.zone.rot else ''}): {where}; "
            f"{m.struct.levels} níveis com vão de {_fmt(m.struct.level_h)} cm")
    for m in plan.modules:
        used = [lv for lv in m.levels if lv.positions]
        add(3, f"Corredor {m.aisle}", "Configurar", f"Estante {m.aisle}-{m.bay}",
            "; ".join(f"N{lv.n}: {lv.cap}× {code(lv.type_id)}" if lv.type_id is not None else f"N{lv.n}: livre"
                      for lv in m.levels)
            + f" · etiquetar {sum(len(lv.positions) for lv in used)} endereço(s)")
    for m in plan.modules:
        for lv in m.levels:
            for pos in lv.positions:
                add(4, f"Corredor {m.aisle}", "Abastecer", pos.address,
                    f"{pos.bins}× {code(pos.type_id)} com {pos.sku} — {pos.name}", f"{_fmt(pos.units)} un.")
    for dm in plan.demands:
        if dm.status not in ("ok", "sem_qtd"):
            add(5, DEMAND_STATUS.get(dm.status, dm.status), "Resolver", dm.sku, f"{dm.name} — {dm.note or dm.status_label}")
    return rep


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def to_xlsx(rep: SiteReport) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F766E"))
    phase_fill = {k: PatternFill("solid", fgColor=c) for k, c in
                  {1: "F4F6F8", 2: "E8F0FE", 3: "FFF4E5", 4: "E7F5EC", 5: "FDECEA"}.items()}

    def table(ws, cols, rows, widths=None, money_cols=()):
        ws.append(cols)
        start = ws.max_row
        for c in ws[start]:
            c.font, c.fill = head
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(list(r))
        for i in range(1, len(cols) + 1):
            letter = get_column_letter(i)
            w = (widths or {}).get(i)
            if w is None:
                w = max((len(str(c.value)) for c in ws[letter] if c.value is not None), default=8) + 2
            ws.column_dimensions[letter].width = max(8, min(60, w))
            if i in money_cols:
                for c in ws[letter][start:]:
                    c.number_format = 'R$ #,##0.00'
        if start == 1:
            ws.freeze_panes = "A2"
        return start

    # Resumo
    ws = wb.active
    ws.title = "Resumo"
    ws.append([rep.title])
    ws["A1"].font = Font(bold=True, size=14, color="0F766E")
    ws.append([f"Gerado em {rep.created:%d/%m/%Y %H:%M}"])
    for title, rows in (("Imóvel e demanda", rep.scope), ("Regras e critérios", rep.params),
                        ("Indicadores", [(k, v, c) for k, v, c in rep.kpis])):
        ws.append([])
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        for r in rows:
            ws.append(list(r))
    if rep.warnings:
        ws.append([])
        ws.append(["Avisos"])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        for w in rep.warnings:
            ws.append([w])
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 70
    ws.column_dimensions["C"].width = 60

    ws = wb.create_sheet("Lista de compras")
    table(ws, ["Item", "Código", "Nome", "Quantidade", "Custo unit.", "Custo total", "Especificação"],
          [(pc["kind"], pc["code"], pc["name"], pc["qty"], pc["unit"], pc["total"], pc["note"]) for pc in rep.purchases]
          + [("Total", "", "", None, None, rep.purchase_total, "")], {3: 34, 7: 50}, money_cols=(5, 6))
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.cell(ws.max_row, 6).font = Font(bold=True)

    ws = wb.create_sheet("Passo a passo")
    table(ws, ["Nº", "Fase", "Grupo", "Ação", "Onde", "Descrição", "Qtd.", "Feito"],
          [(s.n, s.phase_label, s.group, s.action, s.where, s.detail, s.qty, "☐") for s in rep.steps],
          {1: 6, 2: 30, 3: 16, 4: 12, 5: 18, 6: 90, 7: 10, 8: 7})
    for i, s in enumerate(rep.steps, start=2):
        ws.cell(i, 2).fill = phase_fill.get(s.phase)
    ws.auto_filter.ref = f"A1:H{len(rep.steps) + 1}"

    ws = wb.create_sheet("Montagem")
    table(ws, ["Estante", "Corredor", "Zona", "Estrutura", "Fileira", "Posição em relação ao corredor",
               "X: ao longo do comprimento (m)", "Y: ao longo da largura (m)", "Frente (cm)", "Profundidade (cm)",
               "Níveis", "Vão (cm)", "Níveis × bins"],
          [(r["bay"], r["aisle"], r["zone"], r["struct"], r["row"], r["side"], round(r["x"], 2), round(r["y"], 2),
            r["w"], r["d"], r["levels"], r["level_h"], r["config"]) for r in rep.modules], {6: 40, 13: 70})
    ws = wb.create_sheet("Níveis")
    table(ws, ["Estante", "Nível", "Estrutura", "Tipo de bin", "Capacidade (bins)", "Em uso", "Endereços",
               "Carga (kg)", "Carga máx. (kg)", "Zona de ouro"],
          [(r["bay"], r["level"], r["struct"], r["type"], r["cap"], r["used"], r["positions"], round(r["load"], 1),
            r["max_load"], "sim" if r.get("golden") else "") for r in rep.levels])
    ws = wb.create_sheet("Endereços")
    pc = lambda v: round(v, 1) if v is not None else None  # noqa: E731
    table(ws, ["Endereço", "Estrutura", "Tipo de bin", "Bins", "SKU", "Produto", "Unidades", "Ocupação das bins (%)",
               "Peso (kg)"],
          [(r["address"], r["struct"], r["type"], r["bins"], r["sku"], r["name"], r["units"], pc(r["fill"]),
            round(r["weight"], 2))
           for r in rep.addresses], {6: 40})
    ws = wb.create_sheet("SKUs")
    table(ws, ["SKU", "Produto", "Material", "Quantidade", "Bins", "Ocupação das bins (%)", "Situação", "Observação"],
          [(r["sku"], r["name"], r["material"], r["units"], r["bins"], pc(r["fill"]), r["status"], r["note"])
           for r in rep.skus],
          {2: 40, 8: 40})
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _floor_drawing(plan: SitePlan, max_w: float, max_h: float, dims: str = "summary", pos: str = "out"):
    """Planta vista de cima (reportlab), na mesma escala do SVG da tela. `dims`: none | summary | full (cotas);
    `pos`: cotas completas por fora da planta (out) ou por dentro, em cada zona e obstáculo (in)."""
    from reportlab.graphics.shapes import Drawing, Group, Line, Rect, String
    from reportlab.lib import colors

    from services.site_planner import SHAPE_STYLE, _m, aisle_labels, area_label, dim_chains, inner_dims

    fl, b = plan.floor, plan.building
    if not fl.length_cm or not fl.width_cm or not fl.zones:
        return None
    inside = dims == "full" and pos == "in"
    full = dims == "full" and not inside
    M = 26 if full else 0  # margem das cotas (em cima e à esquerda)
    sc = min((max_w - M) / fl.length_cm, (max_h - 14 - M) / fl.width_cm)
    W, H = fl.length_cm * sc, fl.width_cm * sc
    dw = Drawing(W + 2 + M, H + 16 + M)
    Yp = lambda y, h: 14 + H - (y + h) * sc  # noqa: E731  (eixo y para baixo, como na tela)
    hexc = lambda c: None if c in (None, "none") else colors.HexColor(c)  # noqa: E731
    dw.add(Rect(0, 14, W, H, fillColor=colors.HexColor("#FBFCFD"), strokeColor=colors.HexColor("#1F2D33"), strokeWidth=1.2))
    for kind, X, Y, w, d, label in fl.shapes():
        fill, stroke, dash = SHAPE_STYLE.get(kind, ("none", "#999999", ""))
        dw.add(Rect(X * sc, Yp(Y, d), w * sc, d * sc, fillColor=hexc(fill), strokeColor=hexc(stroke),
                    strokeWidth=0.3 if kind == "lane" else 0.5, strokeDashArray=[2, 2] if dash else None))
        if label and kind == "zone":
            dw.add(String((X + 15) * sc, Yp(Y, 0) + 1.5, label, fontSize=5.5, fillColor=colors.HexColor("#4A6A94")))
            continue
        if label and kind in ("reserved", "avenue", "obstacle", "sector") and min(w, d) * sc >= 6:
            txt = area_label(plan, label) if kind in ("reserved", "avenue") else label
            st = String(0, 0, txt, fontSize=6, fillColor=colors.HexColor(
                {"reserved": "#8A6D1C", "avenue": "#3B5B8C", "sector": "#24613A"}.get(kind, "#FFFFFF")), textAnchor="middle")
            if d > w:  # faixa vertical: texto girado
                from reportlab.graphics.shapes import Group

                g = Group(st)
                g.translate((X + w / 2) * sc + 2, Yp(Y, d) + d * sc / 2)
                g.rotate(90)
                dw.add(g)
            else:
                st.x, st.y = (X + w / 2) * sc, Yp(Y, d) + d * sc / 2 - 2
                dw.add(st)
    color = {k: colors.HexColor(v) for k, v in struct_colors(plan).items()}
    for m in plan.modules:
        X, Y, w, d = m.grect
        dw.add(Rect(X * sc, Yp(Y, d), w * sc, d * sc, fillColor=color.get(m.struct.id, colors.grey),
                    strokeColor=colors.HexColor("#1F2D33") if m.rotated else colors.white,
                    strokeWidth=0.6 if m.rotated else 0.3, strokeDashArray=[1.5, 1] if m.rotated else None))
    for txt, X, Y, vertical in aisle_labels(plan):
        st = String(0, 0, txt, fontSize=6, fillColor=colors.HexColor("#5B6770"))
        if vertical:
            from reportlab.graphics.shapes import Group

            g = Group(st)
            g.translate(X * sc, 14 + H - Y * sc)
            g.rotate(-90)
            dw.add(g)
        else:
            st.x, st.y = X * sc, 14 + H - Y * sc - 2
            dw.add(st)
    desc = (f"{len(fl.zones)} zonas" if fl.mixed else
            f"fileiras ao longo do {'comprimento' if fl.along == 'length' else 'largura'}")
    size = f"{_fmt(b.length_m, 1)} m × {_fmt(b.width_m, 1)} m (comprimento na horizontal) · " if dims != "none" else ""
    dw.add(String(0, 2, f"{size}{desc} · "
                        "amarelo = docas/áreas reservadas · azul = avenidas · verde = setores · preto = obstáculos · tracejado = cabeceiras",
                  fontSize=7, fillColor=colors.HexColor("#5B6770")))
    if inside:
        blue = colors.HexColor("#24548C")
        for x1, y1, x2, y2, label, vertical, kind in inner_dims(fl, 6 / sc, 14 / sc):
            X1, Y1, X2, Y2 = x1 * sc, 14 + H - y1 * sc, x2 * sc, 14 + H - y2 * sc
            ob = kind == "obstacle"  # sobre fundo escuro: linha e texto claros, sem fundo
            ink = colors.HexColor("#DFE5EA") if ob else blue
            dw.add(Line(X1, Y1, X2, Y2, strokeColor=ink, strokeWidth=0.4))
            for X, Y in ((X1, Y1), (X2, Y2)):
                dw.add(Line(X - 1.5 if vertical else X, Y if vertical else Y - 1.5,
                            X + 1.5 if vertical else X, Y if vertical else Y + 1.5, strokeColor=ink, strokeWidth=0.4))
            cx, cy = (X1 + X2) / 2, (Y1 + Y2) / 2
            st = String(0, 0, label, fontSize=5, textAnchor="middle", fillColor=colors.white if ob else blue)
            g = Group(st) if ob else Group(Rect(-len(label) * 1.4 - 1, -1, len(label) * 2.8 + 2, 5.5,
                                                fillColor=colors.white, strokeColor=None), st)
            g.translate(cx - 1.5 if vertical else cx, cy if vertical else cy + 1.2)
            if vertical:
                g.rotate(90)
            dw.add(g)
    if full:
        ink = colors.HexColor("#5B6770")
        top = 14 + H  # borda de cima do imóvel no desenho
        xs, ys = dim_chains(fl, 16 / sc, 16 / sc)
        for _, X, Y, w, d, _ in (k for k in fl.shapes() if k[0] == "obstacle"):  # zonas: as cotas encadeadas bastam
            if w * sc >= 30 and d * sc >= 14:
                dw.add(String(X * sc + 2, Yp(Y, d) + 2, f"{_m(w)} × {_m(d)} m", fontSize=5,
                              fillColor=colors.HexColor("#DFE5EA")))
        for y_off, pts, total in ((20, [0, fl.length_cm], True), (8, xs, False)):
            y = top + y_off
            dw.add(Line(0, y, W, y, strokeColor=ink, strokeWidth=0.4))
            for p in pts:
                dw.add(Line(p * sc, y - 2, p * sc, y + 2, strokeColor=ink, strokeWidth=0.4))
            segs = [(0, fl.length_cm, f"{_m(fl.length_cm)} m (comprimento)")] if total else \
                [(a, c, _m(c - a)) for a, c in zip(pts, pts[1:])]
            for a, c, t in segs:
                dw.add(String((a + c) / 2 * sc, y + 1.5, t, fontSize=6 if total else 5, textAnchor="middle", fillColor=ink))
        for x_off, pts, total in ((20, [0, fl.width_cm], True), (8, ys, False)):
            x = -x_off
            dw.add(Line(x, 14, x, top, strokeColor=ink, strokeWidth=0.4))
            for p in pts:
                dw.add(Line(x - 2, top - p * sc, x + 2, top - p * sc, strokeColor=ink, strokeWidth=0.4))
            segs = [(0, fl.width_cm, f"{_m(fl.width_cm)} m (largura)")] if total else \
                [(a, c, _m(c - a)) for a, c in zip(pts, pts[1:])]
            for a, c, t in segs:
                g = Group(String(0, 0, t, fontSize=6 if total else 5, textAnchor="middle", fillColor=ink))
                g.translate(x - 1.5, top - (a + c) / 2 * sc)
                g.rotate(90)
                dw.add(g)
        # desloca tudo para dar lugar às cotas à esquerda
        allg = Group(*dw.contents)
        allg.translate(M, 0)
        dw.contents = [allg]
    return dw


def to_pdf(rep: SiteReport, dims: str = "summary", pos: str = "out") -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from services.plan_export import _t

    accent = colors.HexColor("#0F766E")
    grid = colors.HexColor("#D0D7DC")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], textColor=accent, fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=accent, fontSize=12, spaceBefore=8, spaceAfter=4)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontSize=9.5, spaceBefore=6, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, leading=10)
    cell = ParagraphStyle("cell", parent=small, fontSize=7.2, leading=8.8)
    big = ParagraphStyle("big", parent=small, fontSize=12, leading=14, fontName="Helvetica-Bold")
    P = lambda s, st=cell: Paragraph(_t(s), st)  # noqa: E731

    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=rep.title, author="Neokioski")
    width = page[0] - 24 * mm

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(12 * mm, 7 * mm, _t(f"{rep.title} - gerado em {rep.created:%d/%m/%Y %H:%M}").replace("&amp;", "&"))
        canvas.drawRightString(page[0] - 12 * mm, 7 * mm, f"Página {d.page}")
        canvas.restoreState()

    def table(data, widths, header=True, zebra=True, extra=()):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        st = [("GRID", (0, 0), (-1, -1), 0.4, grid), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("FONTSIZE", (0, 0), (-1, -1), 7.2), ("TOPPADDING", (0, 0), (-1, -1), 1.6),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                   ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
        if zebra:
            st += [("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F4F6F8")) for i in range(2, len(data), 2)]
        t.setStyle(TableStyle(st + list(extra)))
        return t

    story = [Paragraph(_t(rep.title), h1),
             Paragraph(_t(f"Gerado em {rep.created:%d/%m/%Y %H:%M} · {len(rep.steps)} passo(s)"), small), Spacer(1, 4)]
    sp = [[P("Imóvel e demanda", small), "", P("Regras e critérios", small), ""]]
    for i in range(max(len(rep.scope), len(rep.params))):
        a = rep.scope[i] if i < len(rep.scope) else ("", "")
        c = rep.params[i] if i < len(rep.params) else ("", "")
        sp.append([P(a[0]), P(a[1]), P(c[0]), P(c[1])])
    story.append(table(sp, [30 * mm, 105 * mm, 35 * mm, width - 170 * mm], zebra=False))
    story.append(Spacer(1, 5))
    kc = [[Table([[P(k, small)], [P(v, big)], [P(c, cell)]], colWidths=[width / len(rep.kpis) - 4])
           for k, v, c in rep.kpis]]
    kt = Table(kc, colWidths=[width / len(rep.kpis)] * len(rep.kpis))
    kt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, grid), ("INNERGRID", (0, 0), (-1, -1), 0.4, grid),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(kt)
    if rep.warnings:
        story.append(Paragraph("Avisos", h2))
        story += [Paragraph("• " + _t(w), small) for w in rep.warnings]

    # planta + compras
    story.append(PageBreak())
    story.append(Paragraph("Planta (vista de cima)", h2))
    fd = _floor_drawing(rep.plan, width, 105 * mm, dims, pos) if rep.plan else None
    if fd is not None:
        story.append(fd)
        leg = " · ".join(f"{s.struct.code} ({s.qty})" for s in rep.plan.structs if s.qty)
        story.append(Paragraph(_t("Estruturas: " + leg), small))
    story.append(Paragraph("Lista de compras", h2))
    pd_ = [["Item", "Código", "Nome", "Qtd.", "Custo unit.", "Custo total", "Especificação"]]
    for pc in rep.purchases:
        pd_.append([P(pc["kind"]), P(pc["code"]), P(pc["name"]), _fmt(pc["qty"]), _money(pc["unit"]),
                    _money(pc["total"]), P(pc["note"])])
    pd_.append([P("Total", small), "", "", "", "", _money(rep.purchase_total), ""])
    story.append(table(pd_, [42 * mm, 25 * mm, 45 * mm, 15 * mm, 25 * mm, 27 * mm, width - 179 * mm],
                       extra=[("ALIGN", (3, 1), (5, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))

    # passo a passo
    story.append(PageBreak())
    story.append(Paragraph("Passo a passo", h2))
    bg = {1: "#F4F6F8", 2: "#E8F0FE", 3: "#FFF4E5", 4: "#E7F5EC", 5: "#FDECEA"}
    for label, steps in rep.steps_by_phase():
        story.append(Paragraph(_t(label) + f" ({len(steps)})", h3))
        sd = [["Nº", "Grupo", "Ação", "Onde", "Descrição", "Qtd.", "Feito"]]
        for s in steps:
            sd.append([str(s.n), P(s.group), P(s.action), P(s.where), P(s.detail), P(s.qty), ""])
        ph = steps[0].phase
        story.append(table(sd, [11 * mm, 26 * mm, 22 * mm, 30 * mm, width - 121 * mm, 20 * mm, 12 * mm], zebra=False,
                           extra=[("BACKGROUND", (2, 1), (2, -1), colors.HexColor(bg[ph]))]))

    # montagem e endereços
    story.append(PageBreak())
    story.append(Paragraph("Montagem das estantes", h2))
    md = [["Estante", "Estrutura", "Fileira", "Lado", "X (m)", "Y (m)", "Níveis × vão", "Níveis × bins"]]
    for r in rep.modules:
        md.append([r["bay"], r["struct"], str(r["row"]), r["side"], _fmt(r["x"], 2), _fmt(r["y"], 2),
                   f"{r['levels']} × {_fmt(r['level_h'])} cm", P(r["config"])])
    story.append(table(md, [18 * mm, 24 * mm, 13 * mm, 15 * mm, 15 * mm, 15 * mm, 22 * mm, width - 122 * mm]))
    story.append(Paragraph("X = ao longo do comprimento, a partir da parede da esquerda da planta; Y = ao longo da largura, a partir da parede de cima (canto do módulo).", small))
    story.append(PageBreak())
    story.append(Paragraph("Endereços", h2))
    ad = [["Endereço", "Estrutura", "Tipo de bin", "Bins", "SKU", "Produto", "Unidades", "Ocup. bins", "Peso (kg)"]]
    for r in rep.addresses:
        ad.append([r["address"], r["struct"], r["type"], str(r["bins"]), r["sku"], P(r["name"]), _fmt(r["units"]),
                   (_fmt(r["fill"], 0) + "%") if r["fill"] is not None else "—", _fmt(r["weight"], 1)])
    story.append(table(ad, [26 * mm, 24 * mm, 22 * mm, 12 * mm, 26 * mm, width - 190 * mm, 20 * mm, 18 * mm, 18 * mm],
                       extra=[("ALIGN", (6, 1), (8, -1), "RIGHT")]))
    pend = [r for r in rep.skus if not r["ok"]]
    if pend:
        story.append(Paragraph("SKUs não alocados", h2))
        sd = [["SKU", "Produto", "Situação", "Observação"]] + [[r["sku"], P(r["name"]), P(r["status"]), P(r["note"])]
                                                              for r in pend]
        story.append(table(sd, [28 * mm, 90 * mm, 45 * mm, width - 163 * mm]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
