"""Importação dos dados de uma filial nova para o projeto (planilha modelo).

Um Excel com até 4 abas (todas opcionais, identificadas pelo nome):
  - Imóvel      : Campo | Valor (comprimento, largura, pé-direito, corredores...)
  - Estruturas  : estruturas de armazenagem (cadastradas/atualizadas pelo código) e o modo de cada uma no projeto
  - Bins        : tipos de bin (os códigos novos são cadastrados; os existentes são mantidos)
  - SKUs        : SKU, produto, material, dimensões, peso, saldo, min level e max level da filial NOVA
CSV ou planilha de uma aba só sem nome reconhecido = lista de SKUs.

Os SKUs ficam no próprio projeto (não alteram o cadastro de produtos nem os saldos); dimensões vazias são
completadas pelo cadastro de produtos quando o SKU existe nele.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BinType, StructureType
from services.addressing import normalize_code
from services.file_reader import cell_to_str, iter_rows, normalize_header, parse_decimal

MAX_ITEMS = 200_000

SKU_COLS = {
    "sku": ["sku", "codigo", "codigosku", "codproduto", "codigodoproduto", "codigoproduto", "item", "material code"],
    "name": ["produto", "nome", "descricao", "descricaodoproduto", "nomedoproduto"],
    "material": ["material", "tipodematerial", "tipomaterial", "categoria", "familia"],
    "h": ["altura", "alturacm", "alt", "h"],
    "w": ["largura", "larguracm", "larg", "w"],
    "l": ["comprimento", "comprimentocm", "comp", "profundidade", "l"],
    "wt": ["peso", "pesokg", "pesounitario", "pesounitariokg"],
    "stock": ["saldo", "estoque", "quantidade", "qtd", "saldoatual", "estoqueatual", "qtde"],
    "min": ["min", "minlevel", "nivelminimo", "estoqueminimo", "minimo"],
    "max": ["max", "maxlevel", "nivelmaximo", "estoquemaximo", "maximo"],
}
STRUCT_COLS = {
    "code": ["codigo", "cod", "code"],
    "name": ["nome", "descricao"],
    "kind": ["tipo"],
    "width": ["frente", "frentecm", "largura", "larguracm", "larguraútil"],
    "depth": ["profundidade", "profundidadecm", "prof"],
    "height": ["altura", "alturacm", "alturatotal", "alturatotalcm"],
    "levels": ["niveis", "nniveis", "numerodeniveis", "qtdniveis"],
    "level_height": ["vao", "vaocm", "vaoutil", "vaoutilcm", "vaopornivel", "vaoutilpornivelcm"],
    "max_load": ["carga", "cargakg", "cargapornivel", "cargamaxpornivelkg", "cargamaxima"],
    "cost": ["custo", "custor", "customodulo", "custopormodulo", "custopormodulor"],
    "mode": ["modo", "quantidademodo", "uso"],
    "qty": ["qtdfixa", "quantidadefixa", "fixa", "quantidade", "qtd"],
    "min_qty": ["min", "minimo", "qtdminima"],
    "max_qty": ["max", "maximo", "qtdmaxima"],
}
BIN_COLS = {
    "code": ["codigo", "cod", "code"],
    "name": ["nome", "descricao"],
    "height": ["altura", "alturacm"],
    "width": ["largura", "larguracm"],
    "length": ["comprimento", "comprimentocm"],
    "max_weight": ["peso", "pesomax", "pesomaximo", "pesomaxkg", "pesomaximokg", "capacidadepeso"],
    "max_volume": ["volume", "volumel", "volumelitros", "capacidadevolume"],
}
BUILDING_KEYS = {
    "length_m": ["comprimento", "comprimentom"],
    "width_m": ["largura", "larguram"],
    "clear_height_m": ["pedireito", "pedireitom", "pedireitoutil", "pedireitoutilm", "alturautil"],
    "aisle_m": ["corredor", "corredores", "larguradoscorredores", "larguradoscorredoresm", "larguracorredor"],
    "wall_gap_m": ["afastamento", "afastamentodasparedes", "afastamentodasparedesm"],
    "front_reserved_m": ["faixafrente", "faixareservadanafrente", "faixareservadanafrentedocasexpedicaom", "docas",
                         "areadedocas", "faixareservadanafrentem"],
    "back_reserved_m": ["faixafundo", "faixareservadanofundo", "faixareservadanofundom"],
    "cross_aisles": ["corredorestransversais", "corredorestransversaisqtd", "transversais"],
    "cross_aisle_m": ["larguradostransversais", "larguradostransversaism", "larguratransversal"],
    "orientation": ["orientacao", "orientacaodasfileiras"],
}
MODE_WORDS = {"auto": ["otimizar", "otimizarquantidade", "auto", "automatico", "sim"],
              "fixed": ["fixa", "fixo", "quantidadefixa", "fixed"], "off": ["naousar", "nao", "off", "desligado"]}
SHEETS = {"building": ["imovel", "predio", "galpao"], "structures": ["estruturas", "estrutura"],
          "bins": ["bins", "tiposdebin", "tipos de bin"], "skus": ["skus", "sku", "produtos", "demanda", "itens"]}


def _map(headers: list[str], spec: dict) -> dict[str, Optional[int]]:
    norm = [normalize_header(h) for h in headers]
    out: dict[str, Optional[int]] = {}
    used: set[int] = set()
    for key, syns in spec.items():  # 1º nome exato
        cand = [normalize_header(s) for s in syns]
        idx = next((i for i, h in enumerate(norm) if h in cand and i not in used), None)
        out[key] = idx
        if idx is not None:
            used.add(idx)
    for key, syns in spec.items():  # 2º começa com (ex.: "Altura (cm) unit.")
        if out[key] is None:
            cand = [normalize_header(s) for s in syns if len(normalize_header(s)) >= 3]
            idx = next((i for i, h in enumerate(norm) if i not in used and any(h.startswith(c) for c in cand)), None)
            out[key] = idx
            if idx is not None:
                used.add(idx)
    return out


def _cell(row: list[str], idx: Optional[int]) -> str:
    return row[idx].strip() if idx is not None and idx < len(row) and row[idx] is not None else ""


def _dec(row, idx, label: str, errs: list, line: int) -> Optional[float]:
    raw = _cell(row, idx)
    try:
        v = parse_decimal(raw)
    except ValueError:
        errs.append(f"linha {line}: {label} inválido ({raw!r}).")
        return None
    if v is not None and v < 0:
        errs.append(f"linha {line}: {label} negativo.")
        return None
    return float(v) if v is not None else None


def _sheets(path: Path) -> dict[str, list[list[str]]]:
    """Abas reconhecidas -> linhas (cabeçalho incluso). CSV ou aba sem nome reconhecido = SKUs."""
    if path.suffix.lower() in {".csv", ".txt"}:
        rows = [r for _, r in iter_rows(path, has_header=False)]
        return {"skus": rows}
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out: dict[str, list[list[str]]] = {}
    try:
        for ws in wb.worksheets:
            name = normalize_header(ws.title)
            kind = next((k for k, names in SHEETS.items() if name in {normalize_header(n) for n in names}), None)
            if kind is None:
                continue
            out[kind] = [[cell_to_str(c) for c in r] for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
        if not out and wb.worksheets:
            ws = wb.worksheets[0]
            out["skus"] = [[cell_to_str(c) for c in r] for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r)]
    finally:
        wb.close()
    return out


def import_file(db: Session, path: Path, cfg: dict) -> tuple[dict, list[str], list[str]]:
    """Lê a planilha e atualiza `cfg` (imóvel, modos das estruturas, SKUs) e o cadastro de estruturas/bins.
    Retorna (cfg, mensagens, erros)."""
    sheets = _sheets(path)
    msgs, errs = [], []
    if not sheets:
        return cfg, msgs, ["Nenhuma aba reconhecida (use Imóvel, Estruturas, Bins e SKUs — baixe o modelo)."]

    # ---- Imóvel ----
    if "building" in sheets:
        n = 0
        from services.site_service import BUILDING_FIELDS

        lookup = {normalize_header(s): k for k, syns in BUILDING_KEYS.items() for s in syns}
        lookup.update({normalize_header(label): k for k, label, _, _ in BUILDING_FIELDS})
        lookup[normalize_header("Orientação das fileiras")] = "orientation"
        for row in sheets["building"]:
            key = lookup.get(normalize_header(row[0] if row else ""))
            if key is None or len(row) < 2:
                continue
            val = (row[1] or "").strip()
            if key == "orientation":
                v = normalize_header(val)
                cfg["building"][key] = ("length" if "compr" in v else "width" if "larg" in v else "auto")
            else:
                try:
                    parse_decimal(val)
                except ValueError:
                    errs.append(f"Imóvel: valor inválido para {row[0]} ({val!r}).")
                    continue
                cfg["building"][key] = val.replace(".", ",") if val.count(".") == 1 and "," not in val else val
            n += 1
        msgs.append(f"Imóvel: {n} campo(s) atualizado(s).")

    # ---- Bins ----
    if "bins" in sheets and len(sheets["bins"]) > 1:
        hdr, *rows = sheets["bins"]
        cm = _map(hdr, BIN_COLS)
        if cm["code"] is None:
            errs.append("Bins: coluna Código não encontrada.")
        else:
            created = kept = 0
            existing = set(db.scalars(select(BinType.code)))
            for i, row in enumerate(rows, start=2):
                code = normalize_code(_cell(row, cm["code"]))
                if not code:
                    continue
                if code in existing:
                    kept += 1
                    continue
                e: list[str] = []
                vals = {k: _dec(row, cm[k], k, e, i) for k in ("height", "width", "length", "max_weight", "max_volume")}
                if e or not all(vals[k] for k in ("height", "width", "length")):
                    errs.append(f"Bins linha {i} ({code}): informe altura, largura e comprimento válidos.")
                    continue
                db.add(BinType(code=code[:30], name=(_cell(row, cm["name"]) or code)[:150],
                               **{k: Decimal(str(v)) if v is not None else None for k, v in vals.items()}))
                existing.add(code)
                created += 1
            db.flush()
            msgs.append(f"Bins: {created} tipo(s) cadastrado(s), {kept} já existente(s) mantido(s).")

    # ---- Estruturas ----
    if "structures" in sheets and len(sheets["structures"]) > 1:
        hdr, *rows = sheets["structures"]
        cm = _map(hdr, STRUCT_COLS)
        if cm["code"] is None:
            errs.append("Estruturas: coluna Código não encontrada.")
        else:
            created = updated = 0
            by_code = {s.code: s for s in db.scalars(select(StructureType))}
            mode_of = {normalize_header(w): m for m, ws in MODE_WORDS.items() for w in ws}
            for i, row in enumerate(rows, start=2):
                code = normalize_code(_cell(row, cm["code"]))
                if not code:
                    continue
                e: list[str] = []
                v = {k: _dec(row, cm[k], k, e, i) for k in ("width", "depth", "height", "levels", "level_height",
                                                            "max_load", "cost", "qty", "min_qty", "max_qty")}
                st = by_code.get(code)
                req = ("width", "depth", "height", "levels", "level_height")
                if e or (st is None and not all(v[k] for k in req)):
                    errs.append(f"Estruturas linha {i} ({code}): informe frente, profundidade, altura, níveis e vão.")
                    continue
                if st is None:
                    st = StructureType(code=code[:30], name="", kind="")
                    db.add(st)
                    by_code[code] = st
                    created += 1
                else:
                    updated += 1
                st.name = (_cell(row, cm["name"]) or st.name or code)[:150]
                st.kind = (_cell(row, cm["kind"]) or st.kind or "")[:40]
                for k in ("width", "depth", "height", "level_height", "max_load", "cost"):
                    if v[k] is not None:
                        setattr(st, k, Decimal(str(v[k])))
                if v["levels"]:
                    st.levels = int(v["levels"])
                db.flush()
                mode = mode_of.get(normalize_header(_cell(row, cm["mode"])))
                if mode is None:
                    mode = "fixed" if v["qty"] and not _cell(row, cm["mode"]) else "auto"
                fmt = lambda x: str(int(x)) if x is not None else ""  # noqa: E731
                cfg["structures"][str(st.id)] = {"mode": mode, "qty": fmt(v["qty"]) if mode == "fixed" else "",
                                                 "min_qty": fmt(v["min_qty"]), "max_qty": fmt(v["max_qty"])}
            msgs.append(f"Estruturas: {created} cadastrada(s), {updated} atualizada(s).")

    # ---- SKUs ----
    if "skus" in sheets:
        if len(sheets["skus"]) < 2:
            errs.append("SKUs: a aba não tem linhas.")
        else:
            hdr, *rows = sheets["skus"]
            cm = _map(hdr, SKU_COLS)
            if cm["sku"] is None:
                errs.append("SKUs: coluna SKU não encontrada.")
            elif all(cm[k] is None for k in ("stock", "min", "max")):
                errs.append("SKUs: informe ao menos uma coluna de quantidade (Saldo, Min level ou Max level).")
            else:
                items: dict[str, dict] = {}
                bad = 0
                for i, row in enumerate(rows, start=2):
                    sku = _cell(row, cm["sku"])
                    if not sku:
                        continue
                    e: list[str] = []
                    it = {"sku": sku[:60], "name": _cell(row, cm["name"])[:255], "material": _cell(row, cm["material"])[:60]}
                    for k in ("h", "w", "l", "wt", "stock", "min", "max"):
                        it[k] = _dec(row, cm[k], k, e, i)
                    if e:
                        bad += 1
                        if bad <= 10:
                            errs.extend(f"SKUs {x}" for x in e)
                        continue
                    if sku in items:  # SKU repetido: soma as quantidades
                        prev = items[sku]
                        for k in ("stock", "min", "max"):
                            if it[k] is not None:
                                prev[k] = (prev[k] or 0) + it[k]
                        continue
                    items[sku] = it
                    if len(items) >= MAX_ITEMS:
                        errs.append(f"SKUs: limite de {MAX_ITEMS} SKUs atingido; o resto foi ignorado.")
                        break
                if bad > 10:
                    errs.append(f"SKUs: mais {bad - 10} linha(s) com erro.")
                cfg["items"] = list(items.values())
                cfg["demand"]["source"] = "upload"
                msgs.append(f"SKUs: {len(items)} SKU(s) carregado(s) no projeto (fonte da demanda = arquivo).")
    return cfg, msgs, errs


def template_xlsx(db: Session, cfg: dict) -> bytes:
    """Planilha modelo preenchida com os dados atuais do projeto."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    from services.site_service import BUILDING_FIELDS

    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F2A2A"))
    wb = Workbook()

    def sheet(ws, cols, rows, widths=None):
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head
        for r in rows:
            ws.append(list(r))
        for i, _ in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, 16)
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Instruções"
    for line in [
        "Modelo de importação — Projeto de filial nova",
        "",
        "Todas as abas são opcionais: envie só o que quiser atualizar. Não renomeie as abas.",
        "Imóvel: medidas em metros. Orientação: automática, comprimento ou largura.",
        "Estruturas: medidas em cm. Modo: Otimizar (o sistema decide a quantidade, com Mín./Máx. opcionais), "
        "Fixa (usa exatamente Qtd fixa) ou Não usar. Códigos existentes são atualizados.",
        "Bins: medidas internas em cm, peso máx. em kg, volume em litros (vazio = calculado). Códigos já "
        "cadastrados são mantidos.",
        "SKUs: dimensões em cm e peso em kg por unidade; Saldo, Min level e Max level em unidades da filial nova. "
        "Dimensões vazias são completadas pelo cadastro de produtos (se o SKU existir). SKU repetido soma as quantidades.",
        "Ao enviar a aba SKUs, o projeto passa a usar o arquivo como fonte da demanda (substitui a lista anterior).",
    ]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 130

    b = cfg["building"]
    orient = {"auto": "automática", "length": "comprimento", "width": "largura"}
    sheet(wb.create_sheet("Imóvel"), ["Campo", "Valor"],
          [(label, b.get(k, "")) for k, label, _, _ in BUILDING_FIELDS]
          + [("Orientação das fileiras", orient.get(b.get("orientation") or "auto", "automática"))],
          {1: 50, 2: 16})
    mode_lbl = {"auto": "Otimizar", "fixed": "Fixa", "off": "Não usar"}
    rows = []
    for st in db.scalars(select(StructureType).order_by(StructureType.code)):
        sc = cfg["structures"].get(str(st.id), {})
        f = lambda v: float(v) if v is not None else None  # noqa: E731
        rows.append((st.code, st.name, st.kind, f(st.width), f(st.depth), f(st.height), st.levels, f(st.level_height),
                     f(st.max_load), f(st.cost), mode_lbl.get(sc.get("mode") or "off"), sc.get("qty") or None,
                     sc.get("min_qty") or None, sc.get("max_qty") or None))
    sheet(wb.create_sheet("Estruturas"),
          ["Código", "Nome", "Tipo", "Frente (cm)", "Profundidade (cm)", "Altura (cm)", "Níveis", "Vão útil (cm)",
           "Carga por nível (kg)", "Custo por módulo (R$)", "Modo", "Qtd fixa", "Mín.", "Máx."], rows, {2: 30})
    sheet(wb.create_sheet("Bins"),
          ["Código", "Nome", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso máx. (kg)", "Volume (L)"],
          [(bt.code, bt.name, *(float(v) if v is not None else None for v in
                                (bt.height, bt.width, bt.length, bt.max_weight, bt.max_volume)))
           for bt in db.scalars(select(BinType).where(BinType.parent_id.is_(None)).order_by(BinType.code))], {2: 30})
    sheet(wb.create_sheet("SKUs"),
          ["SKU", "Produto", "Material", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso (kg)", "Saldo",
           "Min level", "Max level"],
          [(it["sku"], it.get("name"), it.get("material"), it.get("h"), it.get("w"), it.get("l"), it.get("wt"),
            it.get("stock"), it.get("min"), it.get("max")) for it in cfg.get("items") or []], {2: 36})
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
