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
    "out": ["saida", "saidas", "saidaperiodo", "giro", "vendas", "demandadesaida", "consumo", "linhasdepedido",
            "popularidade"],
}
ORDER_COLS = {
    "order": ["pedido", "numeropedido", "npedido", "idpedido", "codigopedido", "ordem", "documento", "nf",
              "notafiscal", "order", "orderid"],
    "sku": ["sku", "codigo", "codigosku", "codproduto", "codigodoproduto", "codigoproduto", "item", "produto"],
    "qty": ["quantidade", "qtd", "qtde", "quantity", "saida", "unidades"],
    "date": ["data", "datapedido", "emissao", "dataemissao", "date"],
}
MAX_PAIR_LINES = 40  # pedidos com mais SKUs que isso não entram na contagem de pares (combinações demais)
MAX_PAIRS = 5000  # pares guardados (os que mais saem juntos)
MAX_SAMPLE = 3000  # pedidos guardados (amostra) para o simulador de trajeto
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
    "golden_from": ["zonadeouroinicio", "zonadeouroiniciocm", "inicioouro", "inicio da zona de ouro"],
    "golden_to": ["zonadeourofim", "zonadeourofimcm", "fimouro", "fim da zona de ouro"],
    "first_level": ["alturado1nivel", "alturado1onivel", "alturaprimeironivel", "primeironivel", "1nivelcm", "base"],
    "level_heights": ["vaospornivel", "vaosporniveis", "vaodecadanivel", "vaosporniveiscm", "vaos"],
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
    "address_mode": ["enderecamento", "enderecamentocorredorrua", "letradoendereco", "letrasdosenderecos"],
    "serpentine": ["serpentina", "ruasemserpentina", "ruasemserpentinasimnao", "numeracaoemserpentina"],
}
ZONE_COLS = {
    "name": ["nome", "zona", "nomedazona"],
    "length": ["comprimento", "comprimentom", "tamanho", "extensao"],
    "orientation": ["orientacao", "orientacaodasfileiras", "sentido", "direcao"],
    "aisle": ["corredor", "corredorm", "larguradocorredor", "larguradocorredorm", "larguracorredor"],
    "cross": ["transversais", "corredorestransversais", "qtdtransversais"],
    "structures": ["estruturas", "estruturaspermitidas", "codigosdasestruturas"],
    "x": ["x", "xm", "posicaox"],
    "y": ["y", "ym", "posicaoy"],
    "width": ["largura", "larguraemy", "larguraemym", "larguram", "larguradazona"],
}
OBSTACLE_COLS = {
    "name": ["nome", "descricao", "area", "obstaculo"],
    "x": ["x", "xm", "posicaox", "inicioxm", "distanciadafrente"],
    "y": ["y", "ym", "posicaoy", "inicioym", "distancialateral"],
    "length": ["comprimento", "comprimentoemx", "comprimentoemxm", "comprimentom", "extensaox"],
    "width": ["largura", "larguraemy", "larguraemym", "larguram", "extensaoy"],
}
SECTOR_COLS = {
    "name": ["nome", "setor", "descricao"],
    "area": ["aream2", "areanecessaria", "aream2necessaria", "area m2", "m2", "espaco", "espaconecessario"],
    "min_side": ["ladominimo", "ladominimom", "ladomin", "menorlado"],
    "place": ["onde", "posicao", "local", "preferencia", "lugar"],
    "x": ["x", "xm", "posicaox"],
    "y": ["y", "ym", "posicaoy"],
    "length": ["comprimento", "comprimentoemx", "comprimentoemxm", "comprimentom"],
    "width": ["largura", "larguraemy", "larguraemym", "larguram"],
}
PLACE_WORDS = {"auto": ["auto", "automatico", "otimizar", "livre", "qualquer"], "front": ["docas", "frente", "juntoasdocas"],
               "back": ["fundo", "fundos"], "side": ["lateral", "laterais", "parede", "paredes"],
               "fixed": ["fixa", "fixo", "posicaofixa", "xy"]}
MODE_WORDS = {"auto": ["otimizar", "otimizarquantidade", "auto", "automatico", "sim"],
              "fixed": ["fixa", "fixo", "quantidadefixa", "fixed"], "off": ["naousar", "nao", "off", "desligado"]}
SHEETS = {"building": ["imovel", "predio", "galpao"], "structures": ["estruturas", "estrutura"],
          "bins": ["bins", "tiposdebin", "tipos de bin"], "skus": ["skus", "sku", "produtos", "demanda", "itens"],
          "zones": ["zonas", "zona", "zonasdaplanta"],
          "obstacles": ["obstaculos", "obstaculo", "areasbloqueadas", "bloqueios", "areas bloqueadas"],
          "sectors": ["setores", "setor", "areasoperacionais", "recebimentoeexpedicao"],
          "orders": ["pedidos", "historicodepedidos", "historico", "pedidosdoperiodo", "vendas", "saidas"]}


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
         "zones": ZONE_COLS, "obstacles": OBSTACLE_COLS, "sectors": SECTOR_COLS, "orders": ORDER_COLS}
KIND_LABELS = {"building": "Imóvel", "structures": "Estruturas", "bins": "Bins", "skus": "SKUs", "zones": "Zonas",
               "obstacles": "Obstáculos", "sectors": "Setores", "orders": "Pedidos (histórico de saídas)"}
FIELD_LABELS = {
    "building": {"field": "Campo (nome do parâmetro)", "value": "Valor"},
    "structures": {"code": "Código", "name": "Nome", "kind": "Tipo", "width": "Frente (cm)", "depth": "Profundidade (cm)",
                   "height": "Altura total (cm)", "levels": "Nº de níveis", "level_height": "Vão útil (cm)",
                   "max_load": "Carga por nível (kg)", "cost": "Custo por módulo (R$)", "mode": "Modo",
                   "qty": "Qtd fixa", "min_qty": "Mínimo", "max_qty": "Máximo",
                   "golden_from": "Zona de ouro início (cm)", "golden_to": "Zona de ouro fim (cm)",
                   "first_level": "Altura do 1º nível (cm)", "level_heights": "Vãos por nível (cm, ex.: 45; 40; 35)"},
    "bins": {"code": "Código", "name": "Nome", "height": "Altura (cm)", "width": "Largura (cm)",
             "length": "Comprimento (cm)", "max_weight": "Peso máx. (kg)", "max_volume": "Volume (L)",
             "cost": "Custo por bin (R$)"},
    "skus": {"sku": "SKU", "name": "Produto", "material": "Tipo de material", "h": "Altura (cm)", "w": "Largura (cm)",
             "l": "Comprimento (cm)", "wt": "Peso unitário (kg)", "stock": "Saldo", "min": "Min level",
             "max": "Max level", "out": "Saída (linhas ou unid./período)"},
    "zones": {"name": "Nome", "length": "Comprimento em X (m)", "orientation": "Orientação das fileiras",
              "aisle": "Corredor (m)", "cross": "Corredores transversais", "structures": "Estruturas permitidas (códigos)",
              "x": "X (m)", "y": "Y (m)", "width": "Largura em Y (m)"},
    "obstacles": {"name": "Nome", "x": "X (m)", "y": "Y (m)", "length": "Comprimento em X (m)",
                  "width": "Largura em Y (m)"},
    "sectors": {"name": "Nome", "area": "Área necessária (m²)", "min_side": "Lado mínimo (m)",
                "place": "Onde (automático, docas, fundo, lateral, fixa)", "x": "X (m)", "y": "Y (m)",
                "length": "Comprimento em X (m)", "width": "Largura em Y (m)"},
    "orders": {"order": "Pedido (identificador)", "sku": "SKU", "qty": "Quantidade", "date": "Data"},
}
REQUIRED = {"building": ["field", "value"], "structures": ["code"], "bins": ["code"], "skus": ["sku"],
            "zones": ["orientation"], "obstacles": ["x", "y", "length", "width"],
            "sectors": ["name"], "orders": ["order", "sku", "qty"]}


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
            elif key == "serpentine":
                cfg["building"][key] = "1" if normalize_header(val) in {"sim", "s", "1", "true", "yes"} else ""
            elif key == "address_mode":
                cfg["building"][key] = "row" if any(w in normalize_header(val) for w in ("rua", "fileira", "row")) else "aisle"
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
                                                            "max_load", "cost", "qty", "min_qty", "max_qty",
                                                            "golden_from", "golden_to")}
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
                if v["golden_from"] is not None and v["golden_to"] is not None and v["golden_to"] > v["golden_from"]:
                    st.golden_from, st.golden_to = Decimal(str(v["golden_from"])), Decimal(str(v["golden_to"]))
                elif v["golden_from"] is not None or v["golden_to"] is not None:
                    errs.append(f"Estruturas linha {i} ({code}): zona de ouro precisa de início e fim (fim maior).")
                if cm.get("first_level") is not None and _cell(row, cm["first_level"]):
                    fl = _dec(row, cm["first_level"], "1º nível", errs, i)
                    st.first_level_cm = Decimal(str(fl)) if fl is not None else st.first_level_cm
                if cm.get("level_heights") is not None and _cell(row, cm["level_heights"]):
                    try:
                        hs = [float(x.replace(",", ".")) for x in re.split(r"[;/|\s]+", _cell(row, cm["level_heights"])) if x]
                        st.level_heights = [h for h in hs if h > 0][:st.levels or 50] or None
                    except ValueError:
                        errs.append(f"Estruturas linha {i} ({code}): vãos por nível inválidos (use 45; 40; 35).")
                total = float(st.first_level_cm or 0) + sum(st.vaos())
                if st.height and total > float(st.height) + 1e-6:
                    errs.append(f"Estruturas linha {i} ({code}): 1º nível + vãos ({total:g} cm) passam da altura total "
                                f"({float(st.height):g} cm) — altura do 1º nível e vãos por nível não foram gravados.")
                    st.first_level_cm, st.level_heights = None, None
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
            ln, al, cr, zx, zy, zw = (_dec(row, cm[k], k, e, i) for k in ("length", "aisle", "cross", "x", "y", "width"))
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
                          "x_m": fmt(zx), "y_m": fmt(zy), "width_m": fmt(zw),
                          "structures": [str(code_id[c]) for c in codes if c in code_id]})
        if bad:
            errs.append("Zonas: " + "; ".join(bad) + ".")
        if len(zones) == 1 and not (zones[0]["x_m"] and zones[0]["y_m"]):
            errs.append("Zonas: com zonas empilhadas (sem X/Y), informe ao menos duas; a zona foi ignorada.")
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

    # ---- Setores ----
    if "sectors" in sheets:
        hdr, *rows = sheets["sectors"] if sheets["sectors"] else [[]]
        cm = cms["sectors"]
        secs = []
        fmt = lambda v: f"{v:g}".replace(".", ",") if v is not None else ""  # noqa: E731
        for i, row in enumerate(rows, start=2):
            e: list[str] = []
            name = _cell(row, cm["name"])[:40]
            vals = {k: _dec(row, cm.get(k), k, e, i) for k in ("area", "min_side", "x", "y", "length", "width")}
            if not name and all(v is None for v in vals.values()):
                continue
            pw = normalize_header(_cell(row, cm.get("place")))
            place = next((k for k, ws in PLACE_WORDS.items() if pw in ws), None)
            if place is None:  # vazio ou desconhecido: fixa se veio a posição, senão automático
                place = "fixed" if all(vals[k] is not None for k in ("x", "y", "length", "width")) else "auto"
            if e or (place == "fixed" and any(vals[k] is None for k in ("x", "y", "length", "width"))) \
                    or (place != "fixed" and not vals["area"]):
                errs.append(f"Setores linha {i}: informe a área (ou X, Y, comprimento e largura para posição fixa).")
                continue
            secs.append({"name": name or f"Setor {len(secs) + 1}", "area_m2": fmt(vals["area"]),
                         "min_side_m": fmt(vals["min_side"]), "place": place, "x_m": fmt(vals["x"]), "y_m": fmt(vals["y"]),
                         "length_m": fmt(vals["length"]), "width_m": fmt(vals["width"])})
        cfg["building"]["sectors"] = secs
        msgs.append(f"Setores: {len(secs)} setor(es).")

    # ---- Pedidos (histórico de saídas) ----
    if "orders" in sheets:
        res = _orders(sheets["orders"], cms["orders"], bool((mapping.get("orders") or {}).get("abs")))
        if res.get("error"):
            errs.append(res["error"])
        else:
            cfg["orders"] = res
            neg = (f" {res['neg']} quantidade(s) negativa(s) "
                   + ("convertida(s) para valor absoluto." if res["abs"] else "ignorada(s) (sem a conversão).")) if res["neg"] else ""
            msgs.append(f"Pedidos: {res['orders']} pedido(s), {res['lines']} linha(s), {len(res['skus'])} SKU(s), "
                        f"{len(res['pairs'])} par(es) que saem juntos.{neg}")

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
                    out = _dec(row, cm.get("out"), "saída", e, i)
                    if out is not None:
                        it["out"] = out
                    if e:
                        bad += 1
                        if bad <= 10:
                            errs.extend(f"SKUs {x}" for x in e)
                        continue
                    if sku in items:  # SKU repetido: soma as quantidades
                        prev = items[sku]
                        for k in ("stock", "min", "max", "out"):
                            if it.get(k) is not None:
                                prev[k] = (prev.get(k) or 0) + it[k]
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


def _iso_date(raw: str) -> str:
    """Data como AAAA-MM-DD (aceita ISO e DD/MM/AAAA); vazio se não reconhecer."""
    import re

    raw = (raw or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if m:
        d, mo, y = m.groups()
        y = ("20" + y) if len(y) == 2 else y
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def _orders(rows_all: list[list[str]], cm: dict, to_abs: bool) -> dict:
    """Resumo do histórico de pedidos (o arquivo bruto não fica no projeto): por SKU, linhas de pedido (em quantos
    pedidos aparece) e unidades; pares de SKUs que aparecem no mesmo pedido (os MAX_PAIRS mais frequentes).
    Quantidade negativa = saída em alguns relatórios: com `to_abs` vira positiva; sem, a linha é ignorada."""
    from collections import Counter
    from itertools import combinations

    if len(rows_all) < 2:
        return {"error": "Pedidos: a aba não tem linhas."}
    _hdr, *rows = rows_all
    lines: dict[tuple, float] = {}
    neg = ignored = bad = 0
    dates = []
    for i, row in enumerate(rows, start=2):
        order, sku = _cell(row, cm["order"]), _cell(row, cm["sku"])
        if not order or not sku:
            continue
        try:
            q = parse_decimal(_cell(row, cm["qty"]))
        except ValueError:
            bad += 1
            continue
        q = float(q) if q is not None else 1.0  # quantidade vazia: conta a linha (1 unidade)
        if q < 0:
            neg += 1
            if not to_abs:
                ignored += 1
                continue
            q = -q
        if q == 0:
            continue
        key = (order[:60], sku[:60])
        lines[key] = lines.get(key, 0.0) + q
        if cm.get("date") is not None:
            d = _iso_date(_cell(row, cm["date"]))
            if d:
                dates.append(d)
    if not lines:
        return {"error": "Pedidos: nenhuma linha válida (pedido, SKU e quantidade)"
                         + (f"; {neg} quantidade(s) negativa(s) ignorada(s) — marque a conversão para valor absoluto."
                            if ignored else ".")}
    skus: dict[str, list] = {}
    orders: dict[str, list] = {}
    for (o, s), q in lines.items():
        e = skus.setdefault(s, [0, 0.0])
        e[0] += 1
        e[1] += q
        orders.setdefault(o, []).append(s)
    pairs: Counter = Counter()
    big = 0
    for o, ss in orders.items():
        if len(ss) > MAX_PAIR_LINES:
            big += 1
            continue
        for a, b in combinations(sorted(ss), 2):
            pairs[(a, b)] += 1
    top = [[a, b, c] for (a, b), c in pairs.most_common(MAX_PAIRS) if c >= 2]
    # amostra de pedidos (SKUs de cada um) para o simulador de trajeto
    import random

    ids = sorted(orders)
    pick = random.Random(1).sample(ids, min(MAX_SAMPLE, len(ids)))
    # [pedido, SKUs, unidades de cada SKU] — as unidades entram no tempo de pega (tempo por peça) do simulador
    sample = []
    for o in sorted(pick):
        ss = sorted(set(orders[o]))
        sample.append([o, ss, [round(lines[(o, x)], 3) for x in ss]])
    return {"orders": len(orders), "lines": len(lines), "units": round(sum(lines.values()), 3), "sample": sample,
            "skus": {s: [n, round(u, 3)] for s, (n, u) in skus.items()}, "pairs": top, "neg": neg, "abs": to_abs,
            "ignored": ignored, "bad": bad, "big_orders": big,
            "dates": [min(dates), max(dates)] if dates else None}


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
        "Zonas (opcional): com X e Y, cada zona é um retângulo em qualquer lugar do imóvel (X, Y, comprimento em X e "
        "largura em Y; largura vazia = até a parede) e o espaço entre elas é a circulação. Sem X e Y, as zonas (ao menos "
        "duas) são faixas empilhadas a partir da frente, na ordem das linhas, separadas por avenidas (largura na aba "
        "Imóvel); comprimento vazio = o que sobrar. Orientação = 'ao longo do comprimento' ou 'perpendicular'; Corredor "
        "vazio = o do imóvel; Estruturas permitidas = códigos separados por vírgula (vazio = todas). Aba vazia = sem zonas.",
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
             ("Posição da cabeceira (encostada/borda)", "borda" if b.get("cap_align") == "edge" else "encostada"),
             ("Endereçamento (corredor/rua)", "rua" if b.get("address_mode") == "row" else "corredor"),
             ("Ruas em serpentina (sim/não)", "sim" if b.get("serpentine") else "não")],
          {1: 50, 2: 16})
    id_code = {str(s.id): s.code for s in db.scalars(select(StructureType))}
    ori_lbl = {"along": "ao longo do comprimento", "across": "perpendicular (ao longo da largura)"}
    sheet(wb.create_sheet("Zonas"),
          ["Nome", "X (m)", "Y (m)", "Comprimento em X (m)", "Largura em Y (m)", "Orientação das fileiras",
           "Corredor (m)", "Corredores transversais", "Estruturas permitidas (códigos)"],
          [(z.get("name"), z.get("x_m") or None, z.get("y_m") or None, z.get("length_m") or None,
            z.get("width_m") or None, ori_lbl.get(z.get("orientation"), "ao longo do comprimento"),
            z.get("aisle_m") or None, z.get("cross_aisles") or None,
            ", ".join(id_code.get(x, "") for x in z.get("structures") or [] if x in id_code) or None)
           for z in b.get("zones") or []], {1: 22, 6: 36, 9: 34})
    sheet(wb.create_sheet("Obstáculos"),
          ["Nome", "X (m)", "Y (m)", "Comprimento em X (m)", "Largura em Y (m)"],
          [(o.get("name"), o.get("x_m"), o.get("y_m"), o.get("length_m"), o.get("width_m"))
           for o in b.get("obstacles") or []], {1: 24})
    place_lbl = {"auto": "automático", "front": "docas", "back": "fundo", "side": "lateral", "fixed": "fixa"}
    sheet(wb.create_sheet("Setores"),
          ["Nome", "Área necessária (m²)", "Lado mínimo (m)", "Onde (automático, docas, fundo, lateral, fixa)",
           "X (m)", "Y (m)", "Comprimento em X (m)", "Largura em Y (m)"],
          [(sc.get("name"), sc.get("area_m2") or None, sc.get("min_side_m") or None, place_lbl.get(sc.get("place"), "automático"),
            sc.get("x_m") or None, sc.get("y_m") or None, sc.get("length_m") or None, sc.get("width_m") or None)
           for sc in b.get("sectors") or []], {1: 22, 4: 40})
    mode_lbl = {"auto": "Otimizar", "fixed": "Fixa", "off": "Não usar"}
    rows = []
    for st in db.scalars(select(StructureType).order_by(StructureType.code)):
        sc = cfg["structures"].get(str(st.id), {})
        f = lambda v: float(v) if v is not None else None  # noqa: E731
        rows.append((st.code, st.name, st.kind, f(st.width), f(st.depth), f(st.height), st.levels, f(st.level_height),
                     f(st.max_load), f(st.cost), mode_lbl.get(sc.get("mode") or "off"), sc.get("qty") or None,
                     sc.get("min_qty") or None, sc.get("max_qty") or None, f(st.golden_from), f(st.golden_to),
                     f(st.first_level_cm), "; ".join(f"{h:g}" for h in (st.level_heights or [])) or None))
    sheet(wb.create_sheet("Estruturas"),
          ["Código", "Nome", "Tipo", "Frente (cm)", "Profundidade (cm)", "Altura (cm)", "Níveis", "Vão útil (cm)",
           "Carga por nível (kg)", "Custo por módulo (R$)", "Modo", "Qtd fixa", "Mín.", "Máx.",
           "Zona de ouro início (cm)", "Zona de ouro fim (cm)", "Altura do 1º nível (cm)", "Vãos por nível (cm)"],
          rows, {2: 30})
    sheet(wb.create_sheet("Bins"),
          ["Código", "Nome", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso máx. (kg)", "Volume (L)",
           "Custo por bin (R$)"],
          [(bt.code, bt.name, *(float(v) if v is not None else None for v in
                                (bt.height, bt.width, bt.length, bt.max_weight, bt.max_volume, bt.cost)))
           for bt in db.scalars(select(BinType).where(BinType.parent_id.is_(None)).order_by(BinType.code))], {2: 30})
    sheet(wb.create_sheet("SKUs"),
          ["SKU", "Produto", "Material", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso (kg)", "Saldo",
           "Min level", "Max level", "Saída (linhas ou unid./período)"],
          [(it["sku"], it.get("name"), it.get("material"), it.get("h"), it.get("w"), it.get("l"), it.get("wt"),
            it.get("stock"), it.get("min"), it.get("max"), it.get("out")) for it in cfg.get("items") or []], {2: 36, 11: 18})
    # o projeto guarda só o resumo dos pedidos: a aba vem vazia, no formato esperado
    sheet(wb.create_sheet("Pedidos"), ["Pedido", "Data", "SKU", "Quantidade"], [], {1: 14, 2: 12})
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
