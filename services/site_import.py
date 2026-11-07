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

import re
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
    "cost": ["custo", "custor", "custobin", "custoporbin", "preco", "valor"],
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
    "end_caps": ["cabeceiras", "usarcabeceiras", "modulosdecabeceira", "cabeceirasnaspontas", "cabeceirassimnao"],
    "cap_align": ["posicaodacabeceira", "posicaodacabeceiraencostadaborda", "alinhamentodacabeceira", "cabeceiraposicao"],
}
ZONE_COLS = {
    "name": ["nome", "zona", "nomedazona"],
    "length": ["comprimento", "comprimentom", "tamanho", "extensao"],
    "orientation": ["orientacao", "orientacaodasfileiras", "sentido", "direcao"],
    "aisle": ["corredor", "corredorm", "larguradocorredor", "larguradocorredorm", "larguracorredor"],
    "cross": ["transversais", "corredorestransversais", "qtdtransversais"],
    "structures": ["estruturas", "estruturaspermitidas", "codigosdasestruturas"],
}
OBSTACLE_COLS = {
    "name": ["nome", "descricao", "area", "obstaculo"],
    "x": ["x", "xm", "posicaox", "inicioxm", "distanciadafrente"],
    "y": ["y", "ym", "posicaoy", "inicioym", "distancialateral"],
    "length": ["comprimento", "comprimentoemx", "comprimentoemxm", "comprimentom", "extensaox"],
    "width": ["largura", "larguraemy", "larguraemym", "larguram", "extensaoy"],
}
MODE_WORDS = {"auto": ["otimizar", "otimizarquantidade", "auto", "automatico", "sim"],
              "fixed": ["fixa", "fixo", "quantidadefixa", "fixed"], "off": ["naousar", "nao", "off", "desligado"]}
SHEETS = {"building": ["imovel", "predio", "galpao"], "structures": ["estruturas", "estrutura"],
          "bins": ["bins", "tiposdebin", "tipos de bin"], "skus": ["skus", "sku", "produtos", "demanda", "itens"],
          "zones": ["zonas", "zona", "zonasdaplanta"],
          "obstacles": ["obstaculos", "obstaculo", "areasbloqueadas", "bloqueios", "areas bloqueadas"]}


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


BUILDING_COLS = {"field": ["campo", "parametro", "item", "descricao"], "value": ["valor", "value", "medida"]}
SPECS = {"building": BUILDING_COLS, "structures": STRUCT_COLS, "bins": BIN_COLS, "skus": SKU_COLS,
         "zones": ZONE_COLS, "obstacles": OBSTACLE_COLS}
KIND_LABELS = {"building": "Imóvel", "structures": "Estruturas", "bins": "Bins", "skus": "SKUs", "zones": "Zonas",
               "obstacles": "Obstáculos"}
FIELD_LABELS = {
    "building": {"field": "Campo (nome do parâmetro)", "value": "Valor"},
    "structures": {"code": "Código", "name": "Nome", "kind": "Tipo", "width": "Frente (cm)", "depth": "Profundidade (cm)",
                   "height": "Altura total (cm)", "levels": "Nº de níveis", "level_height": "Vão útil (cm)",
                   "max_load": "Carga por nível (kg)", "cost": "Custo por módulo (R$)", "mode": "Modo",
                   "qty": "Qtd fixa", "min_qty": "Mínimo", "max_qty": "Máximo"},
    "bins": {"code": "Código", "name": "Nome", "height": "Altura (cm)", "width": "Largura (cm)",
             "length": "Comprimento (cm)", "max_weight": "Peso máx. (kg)", "max_volume": "Volume (L)",
             "cost": "Custo por bin (R$)"},
    "skus": {"sku": "SKU", "name": "Produto", "material": "Tipo de material", "h": "Altura (cm)", "w": "Largura (cm)",
             "l": "Comprimento (cm)", "wt": "Peso unitário (kg)", "stock": "Saldo", "min": "Min level",
             "max": "Max level"},
    "zones": {"name": "Nome", "length": "Comprimento (m)", "orientation": "Orientação das fileiras",
              "aisle": "Corredor (m)", "cross": "Corredores transversais", "structures": "Estruturas permitidas (códigos)"},
    "obstacles": {"name": "Nome", "x": "X (m)", "y": "Y (m)", "length": "Comprimento em X (m)",
                  "width": "Largura em Y (m)"},
}
REQUIRED = {"building": ["field", "value"], "structures": ["code"], "bins": ["code"], "skus": ["sku"],
            "zones": ["orientation"], "obstacles": ["x", "y", "length", "width"]}


def read_sheets(path: Path, limit: Optional[int] = None) -> dict[str, list[list[str]]]:
    """Todas as abas (nome -> linhas não vazias, cabeçalho incluso). CSV = uma aba "CSV".
    `limit` = máximo de linhas lidas por aba (prévia)."""
    from itertools import islice

    if path.suffix.lower() in {".csv", ".txt"}:
        return {"CSV": [r for _, r in islice(iter_rows(path, has_header=False), limit)]}
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out: dict[str, list[list[str]]] = {}
    try:
        for ws in wb.worksheets:
            rows = (r for r in ws.iter_rows(values_only=True) if any(c not in (None, "") for c in r))
            out[ws.title] = [[cell_to_str(c) for c in r] for r in islice(rows, limit)]
    finally:
        wb.close()
    return out


def guess_sheets(sheets: dict[str, list[list[str]]], remembered: Optional[dict] = None) -> dict[str, Optional[str]]:
    """Aba de cada tipo: 1º a aba usada na última importação do projeto, 2º pelo nome da aba, 3º pela aba cujos
    cabeçalhos mais se parecem com os campos do tipo (precisa achar os obrigatórios)."""
    names = list(sheets)
    out: dict[str, Optional[str]] = {}
    for kind, syns in SHEETS.items():
        last = ((remembered or {}).get(kind) or {}).get("__sheet__")
        cand = {normalize_header(n) for n in syns}
        out[kind] = last if last in sheets else next((n for n in names if normalize_header(n) in cand), None)
    taken = {v for v in out.values() if v}
    for kind in ("skus", "structures", "bins"):
        if out[kind]:
            continue
        best, score = None, 0
        for n in names:
            if n in taken or not sheets[n]:
                continue
            cols = _map(sheets[n][0], SPECS[kind])
            if any(cols[f] is None for f in REQUIRED[kind]):
                continue
            sc = sum(v is not None for v in cols.values())
            if sc > score:
                best, score = n, sc
        if best and score >= 3:
            out[kind] = best
            taken.add(best)
    if not any(out.values()) and len(names) == 1:
        out["skus"] = names[0]
    return out


def guess_columns(kind: str, headers: list[str], remembered: Optional[dict] = None) -> dict[str, Optional[int]]:
    """Coluna de cada campo: 1º o cabeçalho usado na última importação do projeto, depois os sinônimos."""
    cols = _map(headers, SPECS[kind])
    if kind == "building":
        cols["field"] = 0 if cols["field"] is None and headers else cols["field"]
        cols["value"] = 1 if cols["value"] is None and len(headers) > 1 else cols["value"]
    for field, header in (remembered or {}).items():
        if field in cols and header in headers and field != "__sheet__":
            idx = headers.index(header)
            for f, i in cols.items():  # a coluna lembrada sai de outro campo que a tivesse pegado
                if i == idx and f != field:
                    cols[f] = None
            cols[field] = idx
    return cols


def auto_mapping(sheets: dict[str, list[list[str]]], remembered: Optional[dict] = None) -> dict:
    out = {}
    for kind, name in guess_sheets(sheets, remembered).items():
        if name is None:
            continue
        headers = sheets[name][0] if sheets[name] else []
        out[kind] = {"sheet": name, "cols": guess_columns(kind, headers, (remembered or {}).get(kind))}
    return out


def remember(mapping: dict, sheets: dict[str, list[list[str]]]) -> dict:
    """Cabeçalhos escolhidos por campo (para pré-selecionar na próxima importação)."""
    out = {}
    for kind, m in mapping.items():
        headers = sheets.get(m["sheet"], [[]])[0] if sheets.get(m["sheet"]) else []
        out[kind] = {f: headers[i] for f, i in m["cols"].items() if i is not None and i < len(headers)}
        out[kind]["__sheet__"] = m["sheet"]
    return out


def import_file(db: Session, path: Path, cfg: dict, mapping: Optional[dict] = None) -> tuple[dict, list[str], list[str]]:
    """Lê a planilha e atualiza `cfg` (imóvel, modos das estruturas, SKUs) e o cadastro de estruturas/bins.
    `mapping` = {tipo: {"sheet": aba, "cols": {campo: índice da coluna | None}}}; None = automático.
    Retorna (cfg, mensagens, erros)."""
    all_sheets = read_sheets(path)
    mapping = mapping if mapping is not None else auto_mapping(all_sheets, cfg.get("import_mapping"))
    msgs, errs = [], []
    sheets, cms = {}, {}
    for kind, m in mapping.items():
        if m.get("sheet") not in all_sheets:
            continue
        cols = {f: None for f in SPECS[kind]} | {f: i for f, i in (m.get("cols") or {}).items() if f in SPECS[kind]}
        missing = [FIELD_LABELS[kind][f] for f in REQUIRED[kind] if cols.get(f) is None]
        if missing:
            errs.append(f"{KIND_LABELS[kind]}: escolha a coluna de {', '.join(missing)}.")
            continue
        sheets[kind], cms[kind] = all_sheets[m["sheet"]], cols
    if not sheets:
        return cfg, msgs, errs or ["Nenhuma aba para importar (use Imóvel, Estruturas, Bins e SKUs — baixe o modelo)."]
    cfg["import_mapping"] = {**(cfg.get("import_mapping") or {}), **remember(
        {k: m for k, m in mapping.items() if k in sheets}, all_sheets)}

    # ---- Imóvel ----
    if "building" in sheets:
        n = 0
        from services.site_service import BUILDING_FIELDS

        lookup = {normalize_header(s): k for k, syns in BUILDING_KEYS.items() for s in syns}
        lookup.update({normalize_header(label): k for k, label, _, _ in BUILDING_FIELDS})
        lookup[normalize_header("Orientação das fileiras")] = "orientation"
        fi, vi = cms["building"]["field"], cms["building"]["value"]
        for row in sheets["building"]:
            key = lookup.get(normalize_header(_cell(row, fi)))
            if key is None:
                continue
            val = _cell(row, vi)
            if key == "orientation":
                v = normalize_header(val)
                cfg["building"][key] = ("length" if "compr" in v else "width" if "larg" in v else "auto")
            elif key == "end_caps":
                cfg["building"][key] = "" if normalize_header(val) in {"nao", "n", "0", "false", "naousar"} else "1"
            elif key == "cap_align":
                cfg["building"][key] = "edge" if any(w in normalize_header(val) for w in ("borda", "edge", "alinh")) else "row"
            else:
                try:
                    parse_decimal(val)
                except ValueError:
                    errs.append(f"Imóvel: valor inválido para {_cell(row, fi)} ({val!r}).")
                    continue
                cfg["building"][key] = val.replace(".", ",") if val.count(".") == 1 and "," not in val else val
            n += 1
        msgs.append(f"Imóvel: {n} campo(s) atualizado(s).")

    # ---- Bins ----
    if "bins" in sheets and len(sheets["bins"]) > 1:
        hdr, *rows = sheets["bins"]
        cm = cms["bins"]
        if cm["code"] is None:
            errs.append("Bins: coluna Código não encontrada.")
        else:
            created = kept = priced = 0
            by_code = {bt.code: bt for bt in db.scalars(select(BinType))}
            existing = set(by_code)
            for i, row in enumerate(rows, start=2):
                code = normalize_code(_cell(row, cm["code"]))
                if not code:
                    continue
                e: list[str] = []
                cost = _dec(row, cm["cost"], "custo", e, i)
                if code in existing:  # tipo já cadastrado: dimensões mantidas; só o custo pode ser atualizado
                    kept += 1
                    bt = by_code.get(code)
                    if bt is not None and cost is not None and not e and bt.cost != Decimal(str(cost)):
                        bt.cost = Decimal(str(cost))
                        priced += 1
                    continue
                vals = {k: _dec(row, cm[k], k, e, i) for k in ("height", "width", "length", "max_weight", "max_volume")}
                vals["cost"] = cost
                if e or not all(vals[k] for k in ("height", "width", "length")):
                    errs.append(f"Bins linha {i} ({code}): informe altura, largura e comprimento válidos.")
                    continue
                db.add(BinType(code=code[:30], name=(_cell(row, cm["name"]) or code)[:150],
                               **{k: Decimal(str(v)) if v is not None else None for k, v in vals.items()}))
                existing.add(code)
                created += 1
            db.flush()
            msgs.append(f"Bins: {created} tipo(s) cadastrado(s), {kept} já existente(s) mantido(s)"
                        + (f" ({priced} com custo atualizado)." if priced else "."))

    # ---- Estruturas ----
    if "structures" in sheets and len(sheets["structures"]) > 1:
        hdr, *rows = sheets["structures"]
        cm = cms["structures"]
        if cm["code"] is None:
            errs.append("Estruturas: coluna Código não encontrada.")
        else:
            created = updated = 0
            by_code = {s.code: s for s in db.scalars(select(StructureType))}
            in_sheet: set[str] = set()
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
                in_sheet.add(str(st.id))
            # a aba lista as estruturas do projeto: as que não estão nela ficam fora ("Não usar")
            off = [sid for sid, sc in cfg["structures"].items() if sid not in in_sheet and sc.get("mode") != "off"]
            for sid in off:
                cfg["structures"][sid] = {"mode": "off", "qty": "", "min_qty": "", "max_qty": ""}
            for st in by_code.values():
                if st.id and str(st.id) not in cfg["structures"]:
                    cfg["structures"][str(st.id)] = {"mode": "off", "qty": "", "min_qty": "", "max_qty": ""}
            msgs.append(f"Estruturas: {created} cadastrada(s), {updated} atualizada(s)"
                        + (f", {len(off)} fora da planilha marcada(s) como \"Não usar\"." if off else "."))

    # ---- Zonas (depois das estruturas: as estruturas permitidas são códigos) ----
    if "zones" in sheets:
        hdr, *rows = sheets["zones"] if sheets["zones"] else [[]]
        cm = cms["zones"]
        code_id = {s.code: s.id for s in db.scalars(select(StructureType))}
        zones, bad = [], []
        for i, row in enumerate(rows, start=2):
            ori_txt = normalize_header(_cell(row, cm["orientation"]))
            if not ori_txt and not _cell(row, cm["name"]):
                continue
            ori = "across" if any(w in ori_txt for w in ("perp", "larg", "across", "transv", "vertical")) else "along"
            e: list[str] = []
            ln, al, cr = (_dec(row, cm[k], k, e, i) for k in ("length", "aisle", "cross"))
            if e:
                errs.extend(f"Zonas {x}" for x in e)
                continue
            codes = [normalize_code(c) for c in re.split(r"[,;/ ]+", _cell(row, cm["structures"])) if c.strip()]
            unknown = [c for c in codes if c not in code_id]
            if unknown:
                bad.append(f"linha {i}: estrutura(s) {', '.join(unknown)} não cadastrada(s)")
            fmt = lambda v: (f"{v:g}".replace(".", ",") if v is not None else "")  # noqa: E731
            zones.append({"name": (_cell(row, cm["name"]) or f"Zona {len(zones) + 1}")[:40], "length_m": fmt(ln),
                          "orientation": ori, "aisle_m": fmt(al), "cross_aisles": str(int(cr)) if cr else "",
                          "structures": [str(code_id[c]) for c in codes if c in code_id]})
        if bad:
            errs.append("Zonas: " + "; ".join(bad) + ".")
        if len(zones) == 1:
            errs.append("Zonas: informe ao menos duas zonas (uma só equivale a não usar zonas); a zona foi ignorada.")
            zones = []
        cfg["building"]["zones"] = zones
        msgs.append(f"Zonas: {len(zones)} zona(s) definida(s)." if zones else "Zonas: nenhuma (planta de zona única).")

    # ---- Obstáculos ----
    if "obstacles" in sheets:
        hdr, *rows = sheets["obstacles"] if sheets["obstacles"] else [[]]
        cm = cms["obstacles"]
        obs = []
        for i, row in enumerate(rows, start=2):
            e: list[str] = []
            vals = {k: _dec(row, cm[k], k, e, i) for k in ("x", "y", "length", "width")}
            if all(v is None for v in vals.values()):
                continue
            if e or any(v is None for v in vals.values()) or not vals["length"] or not vals["width"]:
                errs.append(f"Obstáculos linha {i}: informe X, Y, comprimento e largura válidos.")
                continue
            fmt = lambda v: f"{v:g}".replace(".", ",")  # noqa: E731
            obs.append({"name": (_cell(row, cm["name"]) or f"Obstáculo {len(obs) + 1}")[:40], "x_m": fmt(vals["x"]),
                        "y_m": fmt(vals["y"]), "length_m": fmt(vals["length"]), "width_m": fmt(vals["width"])})
        cfg["building"]["obstacles"] = obs
        msgs.append(f"Obstáculos: {len(obs)} área(s) bloqueada(s).")

    # ---- SKUs ----
    if "skus" in sheets:
        if len(sheets["skus"]) < 2:
            errs.append("SKUs: a aba não tem linhas.")
        else:
            hdr, *rows = sheets["skus"]
            cm = cms["skus"]
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
        "Bins: medidas internas em cm, peso máx. em kg, volume em litros (vazio = calculado), custo em R$ por bin. "
        "Códigos já cadastrados mantêm as medidas; só o custo é atualizado.",
        "SKUs: dimensões em cm e peso em kg por unidade; Saldo, Min level e Max level em unidades da filial nova. "
        "Dimensões vazias são completadas pelo cadastro de produtos (se o SKU existir). SKU repetido soma as quantidades.",
        "Ao enviar a aba SKUs, o projeto passa a usar o arquivo como fonte da demanda (substitui a lista anterior).",
        "Zonas (opcional, ao menos duas): faixas ao longo do comprimento a partir da frente (docas), na ordem das linhas. "
        "Comprimento vazio = o que sobrar; Orientação = 'ao longo do comprimento' ou 'perpendicular'; Corredor vazio = o do "
        "imóvel; Estruturas permitidas = códigos separados por vírgula (vazio = todas). Entre zonas fica uma avenida "
        "(largura na aba Imóvel). Aba vazia = sem zonas.",
        "Obstáculos (opcional): áreas bloqueadas (escritório, pilar, sala…) em metros a partir do canto superior "
        "esquerdo da planta: X ao longo do comprimento, Y ao longo da largura. As fileiras que passariam por elas são cortadas.",
    ]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 130

    b = cfg["building"]
    orient = {"auto": "automática", "length": "comprimento", "width": "largura"}
    sheet(wb.create_sheet("Imóvel"), ["Campo", "Valor"],
          [(label, b.get(k, "")) for k, label, _, _ in BUILDING_FIELDS]
          + [("Orientação das fileiras", orient.get(b.get("orientation") or "auto", "automática")),
             ("Cabeceiras (sim/não)", "sim" if b.get("end_caps") else "não"),
             ("Posição da cabeceira (encostada/borda)", "borda" if b.get("cap_align") == "edge" else "encostada")],
          {1: 50, 2: 16})
    id_code = {str(s.id): s.code for s in db.scalars(select(StructureType))}
    ori_lbl = {"along": "ao longo do comprimento", "across": "perpendicular (ao longo da largura)"}
    sheet(wb.create_sheet("Zonas"),
          ["Nome", "Comprimento (m)", "Orientação das fileiras", "Corredor (m)", "Corredores transversais",
           "Estruturas permitidas (códigos)"],
          [(z.get("name"), z.get("length_m") or None, ori_lbl.get(z.get("orientation"), "ao longo do comprimento"),
            z.get("aisle_m") or None, z.get("cross_aisles") or None,
            ", ".join(id_code.get(x, "") for x in z.get("structures") or [] if x in id_code) or None)
           for z in b.get("zones") or []], {1: 22, 3: 36, 6: 34})
    sheet(wb.create_sheet("Obstáculos"),
          ["Nome", "X (m)", "Y (m)", "Comprimento em X (m)", "Largura em Y (m)"],
          [(o.get("name"), o.get("x_m"), o.get("y_m"), o.get("length_m"), o.get("width_m"))
           for o in b.get("obstacles") or []], {1: 24})
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
          ["Código", "Nome", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso máx. (kg)", "Volume (L)",
           "Custo por bin (R$)"],
          [(bt.code, bt.name, *(float(v) if v is not None else None for v in
                                (bt.height, bt.width, bt.length, bt.max_weight, bt.max_volume, bt.cost)))
           for bt in db.scalars(select(BinType).where(BinType.parent_id.is_(None)).order_by(BinType.code))], {2: 30})
    sheet(wb.create_sheet("SKUs"),
          ["SKU", "Produto", "Material", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso (kg)", "Saldo",
           "Min level", "Max level"],
          [(it["sku"], it.get("name"), it.get("material"), it.get("h"), it.get("w"), it.get("l"), it.get("wt"),
            it.get("stock"), it.get("min"), it.get("max")) for it in cfg.get("items") or []], {2: 36})
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
