"""Exportação do plano de ação em Excel (openpyxl) e PDF (reportlab)."""
from __future__ import annotations

import io

from services.action_plan import ActionPlan, _fmt
from services.i18n import N_, _, fmt_datetime, tr, translate_workbook

ACCENT = "0F766E"
GOOD, BAD = "1A7A4C", "B42318"


def _kpi_rows(plan: ActionPlan):
    for k in plan.kpis:
        d = k.delta
        yield (k.label, k.before, k.after, d, None if k.pct is None else round(k.pct, 1), k.unit.strip(), k.good)


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def to_xlsx(plan: ActionPlan) -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=ACCENT)
    thin = Side(style="thin", color="D0D7DC")

    def header(ws, row, values):
        ws.append(values) if row is None else None
        r = ws.max_row
        for c in ws[r]:
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        return r

    def widths(ws, mn=8, mx=60):
        for col in ws.columns:
            letter = get_column_letter(col[0].column)
            w = max((len(str(c.value)) for c in col if c.value is not None), default=8) + 2
            ws.column_dimensions[letter].width = max(mn, min(mx, w))

    # ---- Resumo ----
    ws = wb.active
    ws.title = _("Resumo")
    ws.append([plan.title])
    ws["A1"].font = Font(bold=True, size=14, color=ACCENT)
    ws.append([_("Gerado em {created}", created=fmt_datetime(plan.created))])
    ws.append([])
    ws.append([_("Escopo"), ""])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    for k, v in plan.scope:
        ws.append([k, v])
    ws.append([])
    ws.append([_("Parâmetros"), ""])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    for k, v in plan.params:
        ws.append([k, v])
    ws.append([])
    ws.append([_("Indicador"), _("Antes"), _("Depois"), _("Variação"), _("Variação %"), _("Unidade")])
    hr = header(ws, 0, None)
    first_kpi = ws.max_row + 1
    for label, b, a, d, pct, unit, good in _kpi_rows(plan):
        ws.append([label, b, a, d, pct, unit])
        if good is not None:
            ws.cell(ws.max_row, 4).font = Font(bold=True, color=GOOD if good else BAD)
    last_kpi = ws.max_row
    for row in ws.iter_rows(min_row=hr, max_row=last_kpi, max_col=6):
        for c in row:
            c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
    for row in ws.iter_rows(min_row=first_kpi, max_row=last_kpi, min_col=2, max_col=4):
        for c in row:
            c.number_format = "#,##0.0" if isinstance(c.value, float) and c.value % 1 else "#,##0"
    ws.append([])
    if plan.warnings:
        ws.append([_("Avisos")])
        ws.cell(ws.max_row, 1).font = Font(bold=True, color=BAD)
        for w in plan.warnings:
            ws.append([w])
    ws.append([])
    ws.append([_("Passos no plano: {n_steps}", n_steps=plan.n_steps) + ("" if plan.applicable else _(" — plano informativo (não pode ser aplicado)"))])
    ws.column_dimensions["A"].width = 34
    for c in "BCDEF":
        ws.column_dimensions[c].width = 16
    # gráfico antes × depois dos indicadores com os dois valores
    pairs = [i for i, k in enumerate(plan.kpis) if k.before is not None and k.after is not None]
    if pairs:
        ch = BarChart()
        ch.type, ch.title, ch.height, ch.width = "col", _("Antes × depois"), 7, 16
        data = Reference(ws, min_col=2, max_col=3, min_row=first_kpi - 1, max_row=first_kpi + max(pairs))
        cats = Reference(ws, min_col=1, min_row=first_kpi, max_row=first_kpi + max(pairs))
        ch.add_data(data, titles_from_data=True)
        ch.set_categories(cats)
        ws.add_chart(ch, "H4")

    # ---- Passo a passo ----
    ws = wb.create_sheet(_("Passo a passo"))
    ws.append(["Nº", _("Grupo"), _("Ação"), _("Onde / origem"), _("Destino"), _("Qtd"), _("Descrição"), _("Destino no momento"), "Feito?"])
    header(ws, 0, None)
    fills = {"remove": "FDECEA", "add": "E7F5EC", "move": "E8F0FE", "info": "F4F6F8"}
    for s in plan.steps:
        ws.append([s.n, s.group, s.action, s.where, s.to, s.qty, s.detail, s.status, "☐"])
        ws.cell(ws.max_row, 3).fill = PatternFill("solid", fgColor=fills.get(s.kind, "FFFFFF"))
    ws.freeze_panes = "A2"
    if plan.steps:
        ws.auto_filter.ref = ws.dimensions
    widths(ws)
    ws.column_dimensions["G"].width = 70

    # ---- De-para ----
    ws = wb.create_sheet("De-para")
    ws.append(plan.depara_cols)
    header(ws, 0, None)
    for row in plan.depara:
        ws.append(list(row))
    ws.freeze_panes = "A2"
    if plan.depara:
        ws.auto_filter.ref = ws.dimensions
    widths(ws)

    # ---- Bins por tipo ----
    ws = wb.create_sheet(_("Bins por tipo"))
    unlimited = plan.mode == "pool_unlimited"
    ws.append([N_("Tipo de bin"), _("Existentes") if plan.mode.startswith("pool") else _("Antes"),
               N_("Necessárias") if unlimited else (_("Usadas no plano") if plan.mode == "pool" else _("Depois")), _("Diferença")])
    header(ws, 0, None)
    for c, b, a in plan.by_type:
        ws.append([c, b, a, a - b])
    widths(ws, 12)
    if plan.by_type:
        ch = BarChart()
        ch.type, ch.title, ch.height, ch.width = "col", _("Bins por tipo"), 8, 18
        n = len(plan.by_type) + 1
        ch.add_data(Reference(ws, min_col=2, max_col=3, min_row=1, max_row=n), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n))
        ws.add_chart(ch, "F2")

    buf = io.BytesIO()
    translate_workbook(wb)
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
_SAFE = {"→": "->", "Δ": "Var.", "☐": "", "↳": ">", "≤": "<=", "≥": ">=", "—": "-", "…": "..."}


def _t(s) -> str:
    s = "" if s is None else str(s)
    for a, b in _SAFE.items():
        s = s.replace(a, b)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_pdf(plan: ActionPlan) -> bytes:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    accent = colors.HexColor("#" + ACCENT)
    good, bad = colors.HexColor("#" + GOOD), colors.HexColor("#" + BAD)
    grid = colors.HexColor("#D0D7DC")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], textColor=accent, fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=accent, fontSize=12, spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8, leading=10, alignment=TA_LEFT)
    cell = ParagraphStyle("cell", parent=small, fontSize=7.5, leading=9)
    P = lambda s, st=cell: Paragraph(_t(s), st)  # noqa: E731

    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=plan.title, author="Neokioski")
    width = page[0] - 24 * mm

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(12 * mm, 7 * mm, _t(f"{plan.title} - " + _("gerado em {created}", created=fmt_datetime(plan.created))).replace("&amp;", "&"))
        canvas.drawRightString(page[0] - 12 * mm, 7 * mm, _("Página {page}", page=d.page))
        canvas.restoreState()

    def table(data, col_widths=None, header=True, zebra=True):
        data = [[tr(c) if isinstance(c, str) else c for c in row] for row in data]  # textos fixos no idioma atual
        t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
        st = [("GRID", (0, 0), (-1, -1), 0.4, grid), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                   ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
        if zebra:
            for i in range(1, len(data)):
                if i % 2 == 0:
                    st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F4F6F8")))
        t.setStyle(TableStyle(st))
        return t

    story = [Paragraph(_t(plan.title), h1),
             Paragraph(_t(_("Gerado em {created} · {n_steps} passo(s)", created=fmt_datetime(plan.created), n_steps=plan.n_steps)
                          + ("" if plan.applicable else _(" · plano informativo (não pode ser aplicado)"))), small),
             Spacer(1, 4)]
    # escopo e parâmetros lado a lado
    sp = [[P(_("Escopo"), small), P("", small), P(_("Parâmetros"), small), P("", small)]]
    rows = max(len(plan.scope), len(plan.params))
    for i in range(rows):
        a = plan.scope[i] if i < len(plan.scope) else ("", "")
        b = plan.params[i] if i < len(plan.params) else ("", "")
        sp.append([P(a[0]), P(a[1]), P(b[0]), P(b[1])])
    story.append(table(sp, [35 * mm, 95 * mm, 50 * mm, width - 180 * mm], zebra=False))

    # indicadores
    story.append(Paragraph(_("Antes × depois"), h2))
    kd = [[_("Indicador"), _("Antes"), _("Depois"), _("Variação"), "Var. %"]]
    colors_rows = []
    for i, (label, b, a, d, pct, unit, ok) in enumerate(_kpi_rows(plan), start=1):
        places = plan.kpis[i - 1].places
        kd.append([label, _fmt(b, places) + (f" {unit}" if b is not None and unit else ""),
                   _fmt(a, places) + (f" {unit}" if a is not None and unit else ""),
                   ("+" if d and d > 0 else "") + _fmt(d, places) if d is not None else "—",
                   (("+" if pct > 0 else "") + _fmt(pct, 1) + "%") if pct is not None else "—"])
        if ok is not None:
            colors_rows.append(("TEXTCOLOR", (3, i), (4, i), good if ok else bad))
    kt = table(kd, [60 * mm, 30 * mm, 30 * mm, 30 * mm, 25 * mm])
    kt.setStyle(TableStyle(colors_rows + [("ALIGN", (1, 1), (-1, -1), "RIGHT")]))

    # gráfico de bins por tipo
    chart = None
    if plan.by_type:
        dw = Drawing(width - 185 * mm, 62 * mm)
        bc = VerticalBarChart()
        bc.x, bc.y, bc.height, bc.width = 30, 28, 62 * mm - 50, width - 185 * mm - 50
        bc.data = [[b for _x, b, _x in plan.by_type], [a for _x, _x, a in plan.by_type]]
        bc.categoryAxis.categoryNames = [c for c, _x, _x in plan.by_type]
        bc.categoryAxis.labels.fontSize = 7
        bc.valueAxis.labels.fontSize = 7
        bc.valueAxis.valueMin = 0
        bc.bars[0].fillColor = colors.HexColor("#9AA5AD")
        bc.bars[1].fillColor = accent
        bc.barSpacing, bc.groupSpacing = 1, 6
        dw.add(bc)
        lg = Legend()
        lg.x, lg.y, lg.fontSize, lg.alignment = width - 185 * mm - 150, 62 * mm - 6, 7, "right"
        lg.dx, lg.dy, lg.deltax = 8, 8, 70
        names = (_("Existentes"), N_("Necessárias")) if plan.mode == "pool_unlimited" else (
            (_("Existentes"), _("Usadas no plano")) if plan.mode == "pool" else (_("Antes"), _("Depois")))
        lg.colorNamePairs = [(colors.HexColor("#9AA5AD"), names[0]), (accent, names[1])]
        lg.columnMaximum = 1
        lg.boxAnchor = "nw"
        dw.add(lg)
        dw.add(String(0, 62 * mm - 10, _("Bins por tipo"), fontSize=8, fontName="Helvetica-Bold"))
        chart = dw
    top = Table([[kt, chart or ""]], colWidths=[180 * mm, width - 180 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(top)

    if plan.warnings:
        story.append(Paragraph(_("Avisos"), h2))
        for w in plan.warnings:
            story.append(Paragraph("• " + _t(w), small))

    # passo a passo (checklist)
    story.append(PageBreak())
    story.append(Paragraph(_("Passo a passo"), h2))
    if plan.steps:
        sd = [["Nº", _("Grupo"), _("Ação"), _("Onde / origem"), _("Destino"), _("Descrição"), _("Destino no momento"), _("Feito")]]
        bg = {"remove": "#FDECEA", "add": "#E7F5EC", "move": "#E8F0FE", "info": "#F4F6F8"}
        st = []
        for i, s in enumerate(plan.steps, start=1):
            sd.append([str(s.n), P(s.group), P(s.action), P(s.where), P(s.to), P(s.detail), P(s.status), ""])
            st.append(("BACKGROUND", (2, i), (2, i), colors.HexColor(bg.get(s.kind, "#FFFFFF"))))
        t = table(sd, [10 * mm, 22 * mm, 22 * mm, 38 * mm, 38 * mm, width - 172 * mm, 30 * mm, 12 * mm], zebra=False)
        t.setStyle(TableStyle(st))
        story.append(t)
    else:
        story.append(Paragraph(_("Nenhuma ação necessária."), small))

    # visual por endereço (modo por endereço: bins antes x depois; pool: bins usadas)
    if plan.visuals:
        story.append(PageBreak())
        story.append(Paragraph(_("Visual por endereço (antes × depois)"), h2))
        vd = [[N_("Endereço"), _("Antes"), _("Depois")]]
        for v in plan.visuals:
            if "before" in v:
                vd.append([P(v["label"]), _bins_drawing(v["before"], 110 * mm), _bins_drawing(v["after"], 110 * mm)])
            else:
                vd.append([P(v["label"]), _bar_drawing(v["before_used"], v["total"], 110 * mm),
                           _bar_drawing(v["after_used"], v["total"], 110 * mm)])
        story.append(table(vd, [35 * mm, 118 * mm, 118 * mm], zebra=False))

    # de-para
    story.append(PageBreak())
    story.append(Paragraph("De-para", h2))
    if plan.depara:
        n = len(plan.depara_cols)
        dd = [plan.depara_cols] + [[P(_fmt(c, 1) if isinstance(c, float) else c) for c in row] for row in plan.depara]
        story.append(table([[P(c, ParagraphStyle("hc", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")) for c in dd[0]]] + dd[1:],
                           [width / n] * n))
    else:
        story.append(Paragraph(_("Sem linhas."), small))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _bins_drawing(bins, max_w):
    """Bins lado a lado, largura proporcional (cm), com o código."""
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors

    h = 14
    d = Drawing(max_w, h + 2)
    units = [(c, w or 30) for c, n, w in bins for _x in range(min(n, 40))]
    if not units:
        d.add(String(2, 4, _("nenhuma bin"), fontSize=7, fillColor=colors.grey))
        return d
    total = sum(w for _x, w in units)
    scale = min(1.0, (max_w - 4) / total) if total else 1
    x = 1
    for c, w in units:
        ww = max(4, w * scale)
        d.add(Rect(x, 1, ww - 1, h, fillColor=colors.HexColor("#DDF3EA"), strokeColor=colors.HexColor("#6C7A84"), strokeWidth=0.5))
        if ww > 18:
            d.add(String(x + 2, 5, c, fontSize=6))
        x += ww
    return d


def _bar_drawing(used, total, max_w):
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.lib import colors

    d = Drawing(max_w, 14)
    total = max(total, 1)
    w = max_w - 50
    d.add(Rect(1, 2, w, 10, fillColor=colors.HexColor("#F4F6F8"), strokeColor=colors.HexColor("#D0D7DC"), strokeWidth=0.5))
    d.add(Rect(1, 2, w * used / total, 10, fillColor=colors.HexColor("#" + ACCENT), strokeWidth=0))
    d.add(String(w + 5, 4, f"{used}/{total} bins", fontSize=7))
    return d
