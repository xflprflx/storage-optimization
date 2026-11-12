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
from services.i18n import N_, _, fmt_datetime, localize_number_text, tr, translate_workbook

PHASES = {
    1: N_("Compras"),
    2: N_("Montagem das estantes"),
    3: N_("Configuração dos níveis e etiquetagem"),
    4: N_("Abastecimento (bins e produtos)"),
    5: N_("Pendências"),
}
PHASE_KIND = {1: "info", 2: "move", 3: "change", 4: "add", 5: "remove"}


def _fmt(v, places=0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{places}f}"
    return localize_number_text(s)


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
        return f"{self.phase}. {tr(PHASES[self.phase])}"


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
        return [(f"{k}. {tr(PHASES[k])}", v) for k, v in sorted(out.items())]


def build_report(plan: SitePlan, project_name: str, cfg: dict, types: dict, bin_names: Optional[dict] = None) -> SiteReport:
    """`types` = {id: BinSpec}; `bin_names` = {id: nome do tipo} (opcional)."""
    b, fl = plan.building, plan.floor
    d, p = cfg.get("demand", {}), cfg.get("params", {})
    code = lambda t: types[t].code if t in types else "?"  # noqa: E731
    bin_names = bin_names or {}
    src = _("Arquivo importado") if d.get("source") == "upload" else (
        _("Cadastro de produtos") + (_(" / filial de referência") if d.get("branch_id") else " (todas as filiais)"))
    scope = [
        (_("Imóvel"), _("{fmt} × {fmt2} m · pé-direito útil {fmt3} m · {fmt4} m²", fmt=_fmt(b.length_m, 2), fmt2=_fmt(b.width_m, 2), fmt3=_fmt(b.clear_height_m, 2), fmt4=_fmt(b.area_m2, 0))),
        (_("Corredores"), _("{fmt} m · afastamento das paredes {fmt2} m", fmt=_fmt(b.aisle_m, 2), fmt2=_fmt(b.wall_gap_m, 2))),
        (_("Faixas reservadas"), f"frente {_fmt(b.front_reserved_m, 2)} m · fundo {_fmt(b.back_reserved_m, 2)} m"),
        (_("Corredores transversais"), f"{b.cross_aisles} × {_fmt(b.cross_aisle_m, 2)} m"),
        (_("Cabeceiras"), (_("permitidas nas pontas com acesso") if b.end_caps else _("não usar"))
         + (_(" · {end_caps} módulo(s) girado(s)", end_caps=plan.end_caps) if plan.end_caps else "")),
        (_("Fileiras"), (_("{len} fileira(s) ao longo do {v} ({get}) · ", len=len(fl.rows), v='comprimento' if fl.along == 'length' else 'largura', get=ORIENTATIONS.get(b.orientation, b.orientation)) if not fl.mixed else
                      _("{len} fileira(s) em {len2} zonas · ", len=len(fl.rows), len2=len(fl.zones)))
         + f"{fl.real_aisles} corredor(es) · profundidade {_fmt(fl.depth, 0)} cm"),
    ] + ([(_("Zonas"), " · ".join(
        _("{name}: {fmt} m, fileiras ao longo da {v}, {len} fileira(s), corredor {fmt2} m", name=z.name, fmt=_fmt(z.cross_cm / 100 if z.rot else z.run_cm / 100, 2), v='largura' if z.rot else 'comprimento', len=len(z.rows), fmt2=_fmt(z.aisle_cm / 100, 2))
        for z in fl.zones) + _(" · avenidas de {fmt} m", fmt=_fmt(b.avenue_m, 2)))] if fl.mixed else []) + [
        (_("Demanda"), f"{src} · base {d.get('basis', '')} · fator {d.get('factor', '100')}% · crescimento "
                    f"{d.get('growth', '0')}%"),
        ("SKUs", _("{len} SKU(s) · {sum} alocado(s)", len=len(plan.demands), sum=sum(1 for x in plan.demands if x.status == 'ok'))),
    ]
    order = [tr(st["label"]) for st in plan.solver.get("stages", [])]
    params = [
        (_("Prioridade dos critérios"), " > ".join(order) or SITE_CRITERIA.get(p.get("objective"), "")),
        (_("Bins de cada SKU"), p.get("bin_objective", "")),
        (_("Enchimento máx. da bin"), f"{p.get('fill_limit', '')}%"),
        (_("Uso da frente do nível"), f"{p.get('level_fill', '')}%"),
        (_("Folgas"), _("acima da bin {top_gap_cm} cm · entre bins {side_gap_cm} cm", top_gap_cm=p.get('top_gap_cm', ''), side_gap_cm=p.get('side_gap_cm', ''))),
        (_("Rotação / tipo único / peso"), f"{'qualquer' if p.get('rotation') == 'any' else _('altura na vertical')} · "
                                       f"{_('1 tipo de bin por SKU') if p.get('single_type') else _('combina tipos')} · "
                                       f"{_('respeita peso') if p.get('weight') else _('sem peso')}"),
        (_("Estruturas"), "; ".join(f"{s.struct.code}: {STRUCT_MODES[s.struct.mode].lower()}"
                                 + (f" {s.struct.qty}" if s.struct.mode == "fixed" else "")
                                 for s in plan.structs if s.struct.mode != "off") or "—"),
        (_("Solver"), _("{status} em {time} s", status=plan.solver.get('status', ''), time=_fmt(plan.solver.get('time', 0), 1))),
    ]
    rep = SiteReport(title=_("Plano de implantação — {project_name}", project_name=project_name), project=project_name, created=datetime.now(),
                     scope=scope, params=params, plan=plan)
    rep.kpis = [
        (_("Módulos de estrutura"), _fmt(plan.total_modules), ", ".join(f"{s.qty}× {s.struct.code}" for s in plan.structs if s.qty)),
        (N_("Bins"), _fmt(plan.bins_needed), ", ".join(f"{t.needed}× {t.spec.code}" for t in plan.types if t.needed)),
        (_("Custo total"), _money(plan.total_cost), f"estruturas {_money(plan.structures_cost)} + bins {_money(plan.bins_cost)}"
         + (_(" · sem custo (R$ 0): {v}", v=', '.join(dict.fromkeys(plan.missing_costs))) if plan.missing_costs else "")),
        (_("Endereços"), _fmt(len(plan.positions)), _("{fmt} níveis em {len} estantes", fmt=_fmt(plan.levels_total), len=len(plan.modules))
         + (f" ({plan.end_caps} cabeceiras)" if plan.end_caps else "")),
        (_("Ocupação dos níveis"), (_fmt(plan.fill, 1) + "%") if plan.fill is not None else "—",
         _("{levels_used} de {levels_total} níveis em uso", levels_used=plan.levels_used, levels_total=plan.levels_total)),
        (_("Ocupação das bins"), (_fmt(plan.bin_fill, 1) + "%") if plan.bin_fill is not None else "—",
         _("volume dos produtos ÷ volume útil das bins")),
    ]
    k = plan.slotting or {}
    if k.get("avg_dist") is not None:
        rep.kpis.append((_("Caminhada por saída"), _fmt(k["avg_dist"], 1) + " m",
                         _("ao acaso {random_dist} m · até {ref}", random_dist=_fmt(k.get('random_dist'), 1), ref=(k.get('ref') or {}).get('label', ''))))
    rep.kpis += [
        (_("Uso das fileiras"), (_fmt(plan.row_use, 1) + "%") if plan.row_use is not None else "—",
         _("{fmt} de {fmt2} m · {fmt3} m² de estruturas", fmt=_fmt(plan.front_m, 1), fmt2=_fmt(fl.total_row_m, 1), fmt3=_fmt(plan.footprint_m2, 1))),
    ]

    # ---- compras ----
    for s in plan.structs:
        if s.qty:
            rep.purchases.append({"kind": N_("Estrutura"), "code": s.struct.code, "name": s.struct.name, "qty": s.qty,
                                  "unit": s.struct.cost, "total": s.cost,
                                  "note": _("{fmt} × {fmt2} × {fmt3} cm, {levels} níveis × {fmt4} cm", fmt=_fmt(s.struct.width), fmt2=_fmt(s.struct.depth), fmt3=_fmt(s.struct.height), levels=s.struct.levels, fmt4=_fmt(s.struct.level_h))})
    for t in plan.types:
        if not t.needed:
            continue
        if t.parent_code:
            rep.purchases.append({"kind": _("Bin (inteira, para compartimentos)"), "code": t.parent_code, "name": "",
                                  "qty": t.parents, "unit": t.parent_cost, "total": t.cost,
                                  "note": f"dividida em compartimentos {t.spec.code} ({t.needed} compartimentos)"})
        else:
            rep.purchases.append({"kind": _("Bin"), "code": t.spec.code, "name": bin_names.get(t.spec.id, ""),
                                  "qty": t.needed, "unit": t.unit_cost, "total": t.cost,
                                  "note": f"{_fmt(t.spec.h)} × {_fmt(t.spec.w)} × {_fmt(t.spec.l)} cm"})

    # ---- montagem / níveis / endereços ----
    side_lbl = {"L": "esquerdo", "R": "direito"}

    def side_of(m) -> str:
        if m.rotated:
            return _("cabeceira (girada 90°, de frente para {faces})", faces=m.faces)
        zn = m.zone
        if zn is not None and zn.aisles[m.row.aisle][1] <= 0:  # fileira de frente para a avenida/faixa
            return _("de frente para {get}", get=zn.edge_names.get(m.row.aisle, _('a área livre')))
        return side_lbl.get(m.row.side, m.row.side)

    for m in plan.modules:
        X, Y, _x, _x = m.grect
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
                              "weight": pos.weight, "fill": pos.fill, "dist": pos.dist_m, "golden": pos.golden})
    for dm in plan.demands:
        rep.skus.append({"sku": dm.sku, "name": dm.name, "material": dm.material, "units": dm.units,
                         "bins": ", ".join(f"{n}× {code(t)}" for t, n in (dm.bins or {}).items()),
                         "status": dm.status_label, "note": dm.note, "ok": dm.status == "ok",
                         "fill": dm.bin_fill, "pop": dm.pop, "abc": dm.abc})
    rep.warnings = list(plan.errors) + list(plan.notes)

    # ---- passo a passo ----
    n = 0

    def add(phase, group, action, where, detail, qty=""):
        nonlocal n
        n += 1
        rep.steps.append(Step(n, phase, group, action, where, detail, qty))

    for pc in rep.purchases:
        add(1, pc["kind"], _("Comprar"), pc["code"], f"{pc['name'] or pc['code']} — {pc['note']}"
            + (f" · {_money(pc['total'])}" if pc["total"] is not None else ""), _fmt(pc["qty"]))
    add(1, _("Etiquetas"), _("Providenciar"), _("Endereços"),
        _("Etiquetas de {len} endereço(s) e de {len2} estante(s)", len=len(plan.positions), len2=len(plan.modules)), _fmt(len(plan.positions)))
    for m in plan.modules:
        X, Y, _x, _x = m.grect
        where = f"X = {_fmt(X / 100, 2)} m, Y = {_fmt(Y / 100, 2)} m"
        zone = f"{m.zone.name}, " if fl.mixed and m.zone is not None else ""
        if m.rotated:
            blk = m.zone.blocks[m.row.block] if m.zone is not None and m.zone.blocks else None
            kind = _("dupla") if blk and len(blk.rows) > 1 else _("simples")
            add(2, _("Corredor {aisle}", aisle=m.aisle), _("Montar"), _("Cabeceira {bay}", bay=f"{m.aisle}-{m.bay}"),
                _("{code} GIRADA 90° na ponta da fileira {kind} ({zone}fileiras {v}), de frente para {faces}: {where}; {levels} níveis com vão de {fmt} cm", code=m.struct.code, kind=kind, zone=zone, v=', '.join(str(r + 1) for r in (blk.rows if blk else [m.row.index])), faces=m.faces, where=where, levels=m.struct.levels, fmt=_fmt(m.struct.level_h)))
            continue
        s_txt = side_of(m)
        zn = m.zone
        facing = zn is not None and zn.aisles[m.row.aisle][1] <= 0
        face = s_txt if facing else _("lado {s_txt} do corredor {aisle}", s_txt=s_txt, aisle=m.aisle)
        add(2, _("Corredor {aisle}", aisle=m.aisle), _("Montar"), _("Estante {bay}", bay=f"{m.aisle}-{m.bay}"),
            _("{code} na fileira {v} ({zone}{face}{v2}): {where}; {levels} níveis com vão de {fmt} cm", code=m.struct.code, v=m.row.index + 1, zone=zone, face=face, v2=N_('; fileira ao longo da largura') if m.zone is not None and m.zone.rot else '', where=where, levels=m.struct.levels, fmt=_fmt(m.struct.level_h)))
    for m in plan.modules:
        used = [lv for lv in m.levels if lv.positions]
        add(3, _("Corredor {aisle}", aisle=m.aisle), _("Configurar"), _("Estante {bay}", bay=f"{m.aisle}-{m.bay}"),
            "; ".join(f"N{lv.n}: {lv.cap}× {code(lv.type_id)}" if lv.type_id is not None else f"N{lv.n}: livre"
                      for lv in m.levels)
            + _(" · etiquetar {sum} endereço(s)", sum=sum(len(lv.positions) for lv in used)))
    for m in plan.modules:
        for lv in m.levels:
            for pos in lv.positions:
                add(4, _("Corredor {aisle}", aisle=m.aisle), _("Abastecer"), pos.address,
                    _("{bins}× {code} com {sku} — {name}", bins=pos.bins, code=code(pos.type_id), sku=pos.sku, name=pos.name), f"{_fmt(pos.units)} un.")
    for dm in plan.demands:
        if dm.status not in ("ok", "sem_qtd"):
            add(5, DEMAND_STATUS.get(dm.status, dm.status), _("Resolver"), dm.sku, f"{dm.name} — {dm.note or dm.status_label}")
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
    ws.title = _("Resumo")
    ws.append([rep.title])
    ws["A1"].font = Font(bold=True, size=14, color="0F766E")
    ws.append([_("Gerado em {created}", created=fmt_datetime(rep.created))])
    for title, rows in ((_("Imóvel e demanda"), rep.scope), (_("Regras e critérios"), rep.params),
                        (_("Indicadores"), [(k, v, c) for k, v, c in rep.kpis])):
        ws.append([])
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        for r in rows:
            ws.append(list(r))
    if rep.warnings:
        ws.append([])
        ws.append([_("Avisos")])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        for w in rep.warnings:
            ws.append([w])
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 70
    ws.column_dimensions["C"].width = 60

    ws = wb.create_sheet(_("Lista de compras"))
    table(ws, [_("Item"), _("Código"), _("Nome"), N_("Quantidade"), _("Custo unit."), _("Custo total"), _("Especificação")],
          [(pc["kind"], pc["code"], pc["name"], pc["qty"], pc["unit"], pc["total"], pc["note"]) for pc in rep.purchases]
          + [(_("Total"), "", "", None, None, rep.purchase_total, "")], {3: 34, 7: 50}, money_cols=(5, 6))
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.cell(ws.max_row, 6).font = Font(bold=True)

    ws = wb.create_sheet(_("Passo a passo"))
    table(ws, ["Nº", _("Fase"), _("Grupo"), _("Ação"), _("Onde"), _("Descrição"), "Qtd.", _("Feito")],
          [(s.n, s.phase_label, s.group, s.action, s.where, s.detail, s.qty, "☐") for s in rep.steps],
          {1: 6, 2: 30, 3: 16, 4: 12, 5: 18, 6: 90, 7: 10, 8: 7})
    for i, s in enumerate(rep.steps, start=2):
        ws.cell(i, 2).fill = phase_fill.get(s.phase)
    ws.auto_filter.ref = f"A1:H{len(rep.steps) + 1}"

    ws = wb.create_sheet(_("Montagem"))
    table(ws, [_("Estante"), _("Corredor"), _("Zona"), N_("Estrutura"), _("Fileira"), _("Posição em relação ao corredor"),
               _("X: ao longo do comprimento (m)"), _("Y: ao longo da largura (m)"), _("Frente (cm)"), _("Profundidade (cm)"),
               N_("Níveis"), _("Vão (cm)"), _("Níveis × bins")],
          [(r["bay"], r["aisle"], r["zone"], r["struct"], r["row"], r["side"], round(r["x"], 2), round(r["y"], 2),
            r["w"], r["d"], r["levels"], r["level_h"], r["config"]) for r in rep.modules], {6: 40, 13: 70})
    ws = wb.create_sheet(N_("Níveis"))
    table(ws, [_("Estante"), _("Nível"), N_("Estrutura"), N_("Tipo de bin"), _("Capacidade (bins)"), _("Em uso"), _("Endereços"),
               _("Carga (kg)"), _("Carga máx. (kg)"), _("Zona de ouro")],
          [(r["bay"], r["level"], r["struct"], r["type"], r["cap"], r["used"], r["positions"], round(r["load"], 1),
            r["max_load"], "sim" if r.get("golden") else "") for r in rep.levels])
    ws = wb.create_sheet(_("Endereços"))
    pc = lambda v: round(v, 1) if v is not None else None  # noqa: E731
    table(ws, [N_("Endereço"), N_("Estrutura"), N_("Tipo de bin"), N_("Bins"), "SKU", N_("Produto"), _("Unidades"), N_("Ocupação das bins (%)"),
               _("Peso (kg)"), "Caminhada (m)", _("Zona de ouro")],
          [(r["address"], r["struct"], r["type"], r["bins"], r["sku"], r["name"], r["units"], pc(r["fill"]),
            round(r["weight"], 2), r.get("dist"), "sim" if r.get("golden") else "")
           for r in rep.addresses], {6: 40})
    ws = wb.create_sheet("SKUs")
    table(ws, ["SKU", N_("Produto"), _("Material"), N_("Quantidade"), N_("Bins"), N_("Ocupação das bins (%)"), _("Saída (período)"), _("Curva ABC"),
               N_("Situação"), _("Observação")],
          [(r["sku"], r["name"], r["material"], r["units"], r["bins"], pc(r["fill"]), r.get("pop"), r.get("abc") or None,
            r["status"], r["note"]) for r in rep.skus],
          {2: 40, 10: 40})
    buf = io.BytesIO()
    translate_workbook(wb)
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
            _("fileiras ao longo do {v}", v='comprimento' if fl.along == 'length' else 'largura'))
    size = _("{fmt} m × {fmt2} m (comprimento na horizontal) · ", fmt=_fmt(b.length_m, 1), fmt2=_fmt(b.width_m, 1)) if dims != "none" else ""
    dw.add(String(0, 2, _("{size}{desc} · amarelo = docas/áreas reservadas · azul = avenidas · verde = setores · preto = obstáculos · tracejado = cabeceiras", size=size, desc=desc),
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
        for _x, X, Y, w, d, _x in (k for k in fl.shapes() if k[0] == "obstacle"):  # zonas: as cotas encadeadas bastam
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
        canvas.drawString(12 * mm, 7 * mm, _t(f"{rep.title} - " + _("gerado em {created}", created=fmt_datetime(rep.created))).replace("&amp;", "&"))
        canvas.drawRightString(page[0] - 12 * mm, 7 * mm, _("Página {page}", page=d.page))
        canvas.restoreState()

    def table(data, widths, header=True, zebra=True, extra=()):
        data = [[tr(c) if isinstance(c, str) else c for c in row] for row in data]  # textos fixos no idioma atual
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
             Paragraph(_t(_("Gerado em {created} · {len} passo(s)", created=fmt_datetime(rep.created), len=len(rep.steps))), small), Spacer(1, 4)]
    sp = [[P(_("Imóvel e demanda"), small), "", P(_("Regras e critérios"), small), ""]]
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
        story.append(Paragraph(_("Avisos"), h2))
        story += [Paragraph("• " + _t(w), small) for w in rep.warnings]

    # planta + compras
    story.append(PageBreak())
    story.append(Paragraph(_("Planta (vista de cima)"), h2))
    fd = _floor_drawing(rep.plan, width, 105 * mm, dims, pos) if rep.plan else None
    if fd is not None:
        story.append(fd)
        leg = " · ".join(f"{s.struct.code} ({s.qty})" for s in rep.plan.structs if s.qty)
        story.append(Paragraph(_t("Estruturas: " + leg), small))
    story.append(Paragraph(_("Lista de compras"), h2))
    pd_ = [[_("Item"), _("Código"), _("Nome"), "Qtd.", _("Custo unit."), _("Custo total"), _("Especificação")]]
    for pc in rep.purchases:
        pd_.append([P(pc["kind"]), P(pc["code"]), P(pc["name"]), _fmt(pc["qty"]), _money(pc["unit"]),
                    _money(pc["total"]), P(pc["note"])])
    pd_.append([P(_("Total"), small), "", "", "", "", _money(rep.purchase_total), ""])
    story.append(table(pd_, [42 * mm, 25 * mm, 45 * mm, 15 * mm, 25 * mm, 27 * mm, width - 179 * mm],
                       extra=[("ALIGN", (3, 1), (5, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))

    # passo a passo
    story.append(PageBreak())
    story.append(Paragraph(_("Passo a passo"), h2))
    bg = {1: "#F4F6F8", 2: "#E8F0FE", 3: "#FFF4E5", 4: "#E7F5EC", 5: "#FDECEA"}
    for label, steps in rep.steps_by_phase():
        story.append(Paragraph(_t(label) + f" ({len(steps)})", h3))
        sd = [["Nº", _("Grupo"), _("Ação"), _("Onde"), _("Descrição"), "Qtd.", _("Feito")]]
        for s in steps:
            sd.append([str(s.n), P(s.group), P(s.action), P(s.where), P(s.detail), P(s.qty), ""])
        ph = steps[0].phase
        story.append(table(sd, [11 * mm, 26 * mm, 22 * mm, 30 * mm, width - 121 * mm, 20 * mm, 12 * mm], zebra=False,
                           extra=[("BACKGROUND", (2, 1), (2, -1), colors.HexColor(bg[ph]))]))

    # montagem e endereços
    story.append(PageBreak())
    story.append(Paragraph(_("Montagem das estantes"), h2))
    md = [[_("Estante"), N_("Estrutura"), _("Fileira"), _("Lado"), "X (m)", "Y (m)", _("Níveis × vão"), _("Níveis × bins")]]
    for r in rep.modules:
        md.append([r["bay"], r["struct"], str(r["row"]), r["side"], _fmt(r["x"], 2), _fmt(r["y"], 2),
                   f"{r['levels']} × {_fmt(r['level_h'])} cm", P(r["config"])])
    story.append(table(md, [18 * mm, 24 * mm, 13 * mm, 15 * mm, 15 * mm, 15 * mm, 22 * mm, width - 122 * mm]))
    story.append(Paragraph(_("X = ao longo do comprimento, a partir da parede da esquerda da planta; Y = ao longo da largura, a partir da parede de cima (canto do módulo)."), small))
    story.append(PageBreak())
    story.append(Paragraph(_("Endereços"), h2))
    ad = [[N_("Endereço"), N_("Estrutura"), N_("Tipo de bin"), N_("Bins"), "SKU", N_("Produto"), _("Unidades"), _("Ocup. bins"), _("Peso (kg)")]]
    for r in rep.addresses:
        ad.append([r["address"], r["struct"], r["type"], str(r["bins"]), r["sku"], P(r["name"]), _fmt(r["units"]),
                   (_fmt(r["fill"], 0) + "%") if r["fill"] is not None else "—", _fmt(r["weight"], 1)])
    story.append(table(ad, [26 * mm, 24 * mm, 22 * mm, 12 * mm, 26 * mm, width - 190 * mm, 20 * mm, 18 * mm, 18 * mm],
                       extra=[("ALIGN", (6, 1), (8, -1), "RIGHT")]))
    pend = [r for r in rep.skus if not r["ok"]]
    if pend:
        story.append(Paragraph(_("SKUs não alocados"), h2))
        sd = [["SKU", N_("Produto"), N_("Situação"), _("Observação")]] + [[r["sku"], P(r["name"]), P(r["status"]), P(r["note"])]
                                                              for r in pend]
        story.append(table(sd, [28 * mm, 90 * mm, 45 * mm, width - 163 * mm]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
