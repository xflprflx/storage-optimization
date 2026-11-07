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
        ("Fileiras", f"{len(fl.rows)} fileira(s) ao longo do {'comprimento' if fl.along == 'length' else 'largura'}"
                     f" ({ORIENTATIONS.get(b.orientation, b.orientation)}) · {len(fl.aisles)} corredor(es) · "
                     f"profundidade {_fmt(fl.depth, 0)} cm"),
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
        ("Custo total", _money(plan.total_cost), f"estruturas {_money(plan.structures_cost)} + bins {_money(plan.bins_cost)}"),
        ("Endereços", _fmt(len(plan.positions)), f"{_fmt(plan.levels_total)} níveis em {len(plan.modules)} estantes"),
        ("Ocupação dos níveis", (_fmt(plan.fill, 1) + "%") if plan.fill is not None else "—",
         f"{plan.levels_used} de {plan.levels_total} níveis em uso"),
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
    for m in plan.modules:
        rep.modules.append({"bay": f"{m.aisle}-{m.bay}", "aisle": m.aisle, "struct": m.struct.code,
                            "row": m.row.index + 1, "side": side_lbl.get(m.row.side, m.row.side),
                            "x": m.x0 / 100.0, "y": m.row.y / 100.0, "w": m.struct.width, "d": m.struct.depth,
                            "levels": m.struct.levels, "level_h": m.struct.level_h,
                            "config": ", ".join(f"N{lv.n}: {lv.cap}× {code(lv.type_id)}" if lv.type_id is not None
                                                else f"N{lv.n}: livre" for lv in m.levels)})
        for lv in m.levels:
            rep.levels.append({"bay": f"{m.aisle}-{m.bay}", "level": lv.n, "struct": m.struct.code,
                               "type": code(lv.type_id) if lv.type_id is not None else "livre", "cap": lv.cap,
                               "used": lv.used, "load": lv.load, "max_load": m.struct.max_load,
                               "positions": len(lv.positions)})
    for pos in plan.positions:
        rep.addresses.append({"address": pos.address, "struct": pos.struct_code, "type": code(pos.type_id),
                              "bins": pos.bins, "sku": pos.sku, "name": pos.name, "units": pos.units,
                              "weight": pos.weight})
    for dm in plan.demands:
        rep.skus.append({"sku": dm.sku, "name": dm.name, "material": dm.material, "units": dm.units,
                         "bins": ", ".join(f"{n}× {code(t)}" for t, n in (dm.bins or {}).items()),
                         "status": dm.status_label, "note": dm.note, "ok": dm.status == "ok"})
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
    along = "comprimento" if fl.along == "length" else "largura"
    for m in plan.modules:
        add(2, f"Corredor {m.aisle}", "Montar", f"Estante {m.aisle}-{m.bay}",
            f"{m.struct.code} na fileira {m.row.index + 1} (lado {side_lbl.get(m.row.side, '')} do corredor {m.aisle}): "
            f"a {_fmt(m.x0 / 100, 2)} m da parede da frente (ao longo do {along}) e {_fmt(m.row.y / 100, 2)} m da "
            f"parede lateral; {m.struct.levels} níveis com vão de {_fmt(m.struct.level_h)} cm")
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
    table(ws, ["Estante", "Corredor", "Estrutura", "Fileira", "Lado do corredor", "X: da parede da frente (m)",
               "Y: da parede lateral (m)", "Frente (cm)", "Profundidade (cm)", "Níveis", "Vão (cm)", "Níveis × bins"],
          [(r["bay"], r["aisle"], r["struct"], r["row"], r["side"], round(r["x"], 2), round(r["y"], 2), r["w"], r["d"],
            r["levels"], r["level_h"], r["config"]) for r in rep.modules], {12: 70})
    ws = wb.create_sheet("Níveis")
    table(ws, ["Estante", "Nível", "Estrutura", "Tipo de bin", "Capacidade (bins)", "Em uso", "Endereços",
               "Carga (kg)", "Carga máx. (kg)"],
          [(r["bay"], r["level"], r["struct"], r["type"], r["cap"], r["used"], r["positions"], round(r["load"], 1),
            r["max_load"]) for r in rep.levels])
    ws = wb.create_sheet("Endereços")
    table(ws, ["Endereço", "Estrutura", "Tipo de bin", "Bins", "SKU", "Produto", "Unidades", "Peso (kg)"],
          [(r["address"], r["struct"], r["type"], r["bins"], r["sku"], r["name"], r["units"], round(r["weight"], 2))
           for r in rep.addresses], {6: 40})
    ws = wb.create_sheet("SKUs")
    table(ws, ["SKU", "Produto", "Material", "Quantidade", "Bins", "Situação", "Observação"],
          [(r["sku"], r["name"], r["material"], r["units"], r["bins"], r["status"], r["note"]) for r in rep.skus],
          {2: 40, 7: 40})
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _floor_drawing(plan: SitePlan, max_w: float, max_h: float):
    """Planta vista de cima (reportlab), na mesma escala do SVG da tela."""
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors

    fl, b = plan.floor, plan.building
    if not fl.run_cm or not fl.cross_cm:
        return None
    sc = min(max_w / fl.run_cm, (max_h - 14) / fl.cross_cm)
    W, H = fl.run_cm * sc, fl.cross_cm * sc
    dw = Drawing(W + 2, H + 16)
    Y = lambda y, h: 14 + H - (y + h) * sc  # noqa: E731  (eixo y para baixo, como na tela)
    dw.add(Rect(0, 14, W, H, fillColor=colors.HexColor("#FBFCFD"), strokeColor=colors.HexColor("#1F2D33"), strokeWidth=1.2))
    gap = b.wall_gap_m * 100
    for x, w in ((gap, b.front_reserved_m * 100), (fl.run_cm - gap - b.back_reserved_m * 100, b.back_reserved_m * 100)):
        if w > 0:
            dw.add(Rect(x * sc, Y(gap, fl.cross_cm - 2 * gap), w * sc, (fl.cross_cm - 2 * gap) * sc,
                        fillColor=colors.HexColor("#FFF3CD"), strokeColor=colors.HexColor("#E0C068"), strokeWidth=0.5))
    for y, a in fl.aisles:
        dw.add(Rect(fl.x_start * sc, Y(y, a), (fl.x_end - fl.x_start) * sc, a * sc, fillColor=colors.HexColor("#EEF2F4"),
                    strokeColor=None))
    color = {k: colors.HexColor(v) for k, v in struct_colors(plan).items()}
    for r in fl.rows:
        for x0, x1 in fl.segments:
            dw.add(Rect(x0 * sc, Y(r.y, r.depth), (x1 - x0) * sc, r.depth * sc, fillColor=None,
                        strokeColor=colors.HexColor("#C9D1D6"), strokeWidth=0.3, strokeDashArray=[2, 2]))
    for m in plan.modules:
        dw.add(Rect(m.x0 * sc, Y(m.row.y, m.row.depth), m.struct.width * sc, m.row.depth * sc,
                    fillColor=color.get(m.struct.id, colors.grey), strokeColor=colors.white, strokeWidth=0.3))
    labels = {}
    for m in plan.modules:
        labels.setdefault(m.row.aisle, m.aisle)
    for i, (y, a) in enumerate(fl.aisles):
        if i in labels and a * sc >= 5:
            dw.add(String(fl.x_start * sc + 3, Y(y, a) + a * sc / 2 - 2.5, f"Corredor {labels[i]}", fontSize=6,
                          fillColor=colors.HexColor("#5B6770")))
    dw.add(String(0, 2, f"{_fmt(b.length_m, 1)} m × {_fmt(b.width_m, 1)} m · fileiras ao longo do "
                        f"{'comprimento' if fl.along == 'length' else 'largura'} · faixa amarela = docas/áreas reservadas",
                  fontSize=7, fillColor=colors.HexColor("#5B6770")))
    return dw


def to_pdf(rep: SiteReport) -> bytes:
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
    fd = _floor_drawing(rep.plan, width, 105 * mm) if rep.plan else None
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
    story.append(Paragraph("X = distância da parede da frente (docas), ao longo das fileiras; Y = distância da parede "
                           "lateral. Medidas do canto do módulo.", small))
    story.append(PageBreak())
    story.append(Paragraph("Endereços", h2))
    ad = [["Endereço", "Estrutura", "Tipo de bin", "Bins", "SKU", "Produto", "Unidades", "Peso (kg)"]]
    for r in rep.addresses:
        ad.append([r["address"], r["struct"], r["type"], str(r["bins"]), r["sku"], P(r["name"]), _fmt(r["units"]),
                   _fmt(r["weight"], 1)])
    story.append(table(ad, [26 * mm, 24 * mm, 22 * mm, 12 * mm, 26 * mm, width - 172 * mm, 20 * mm, 18 * mm],
                       extra=[("ALIGN", (6, 1), (7, -1), "RIGHT")]))
    pend = [r for r in rep.skus if not r["ok"]]
    if pend:
        story.append(Paragraph("SKUs não alocados", h2))
        sd = [["SKU", "Produto", "Situação", "Observação"]] + [[r["sku"], P(r["name"]), P(r["status"]), P(r["note"])]
                                                              for r in pend]
        story.append(table(sd, [28 * mm, 90 * mm, 45 * mm, width - 163 * mm]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
