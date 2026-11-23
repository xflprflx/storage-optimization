"""Importação dos dados de uma filial nova para o projeto (planilha modelo).

Um Excel com até 4 abas (todas opcionais, identificadas pelo nome):
  - Imóvel      : Campo | Valor (comprimento, largura, pé-direito, corredores...)
  - Estruturas  : modelos de estrutura (cadastrados/atualizados pelo código) e o modo de cada uma no projeto
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

from models import BinType, Product, StructureType
from services.addressing import normalize_code
from services.file_reader import cell_to_str, iter_rows, normalize_header, parse_decimal
from services.i18n import N_, _

MAX_ITEMS = 200_000

def condition_code(text: str) -> str:
    """Texto da planilha -> condição de armazenagem ("" = ambiente)."""
    import unicodedata

    t = "".join(c for c in unicodedata.normalize("NFD", (text or "").strip().lower()) if not unicodedata.combining(c))
    if not t:
        return ""
    if t.startswith(("congel", "frozen", "freez")):
        return "congelado"
    if t.startswith(("refrig", "resfri", "frio", "chill", "cold")):
        return "refrigerado"
    if t.startswith(("control", "segur", "secur")):
        return "controlado"
    return ""


def door_code(text: str) -> Optional[str]:
    """Texto da planilha -> tipo de porta ("" = sem porta; None = não informado)."""
    import unicodedata

    t = "".join(c for c in unicodedata.normalize("NFD", (text or "").strip().lower()) if not unicodedata.combining(c))
    if not t:
        return None
    if "correr" in t or "slid" in t:
        return "sliding"
    if "giro" in t or "swing" in t or "abrir" in t or t in ("sim", "yes", "1"):
        return "swing"
    return ""


def _kind_extras(db, st, row, cm) -> None:
    """Colunas opcionais do tipo de estrutura na aba Estruturas: condição de armazenagem e porta. Só o que vier
    preenchido muda o tipo."""
    from decimal import Decimal, InvalidOperation

    from services.structure_kinds import kind_of, refresh_floor

    k = kind_of(db, st)
    if k is None:
        return
    cond = _cell(row, cm.get("condition"))
    if cond:
        k.condition = condition_code(cond)
    dt = door_code(_cell(row, cm.get("door")))
    if dt is not None:
        k.door_type = dt
    lv = _cell(row, cm.get("door_leaves"))
    if lv.strip() in ("1", "2"):
        k.door_leaves = int(lv.strip())
    ang = _cell(row, cm.get("door_angle")).replace("°", "").strip()
    if ang in ("90", "180"):
        k.door_angle = int(ang)
    sh = normalize_header(_cell(row, cm.get("shared")))
    if sh:  # módulos vizinhos compartilham a lateral (colunas / quadro de montantes)
        k.shared_sides = sh in {"sim", "s", "1", "x", "true", "yes", "y", "si", "oui", "ja"}
    tm = _cell(row, cm.get("door_time")).replace(",", ".").strip()
    if tm:
        try:
            k.door_time_s = Decimal(tm)
        except InvalidOperation:
            pass
    refresh_floor(db)


def _cond_label(code) -> str:
    from services.structure_kinds import CONDITIONS

    return _(CONDITIONS[code]) if code in CONDITIONS and code else ""


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
    "condition": ["condicao", "condicaodearmazenagem", "condicaoarmazenagem", "armazenagem", "temperatura", "condition"],
    "out": ["saida", "saidas", "saidaperiodo", "giro", "vendas", "demandadesaida", "consumo", "linhasdepedido",
            "popularidade"],
    # embalagem (opcionais): caixa e palete do produto
    "case_qty": ["unidadesporcaixa", "unporcaixa", "qtdporcaixa", "qtdcaixa", "unitspercase"],
    "case_model": ["modelodeembalagem", "modeloembalagem", "embalagem", "caixapadrao", "packagingmodel"],
    "case_h": ["alturadacaixa", "alturadacaixacm", "alturacaixa", "caseheight"],
    "case_w": ["larguradacaixa", "larguradacaixacm", "larguracaixa", "casewidth"],
    "case_l": ["comprimentodacaixa", "comprimentodacaixacm", "comprimentocaixa", "caselength"],
    "case_wt": ["pesodacaixacheia", "pesodacaixacheiakg", "pesodacaixa", "pesocaixa", "caseweight"],
    "pallet": ["palete", "pallet", "tipodepalete", "paletetipodebin"],
    "pallet_layer": ["caixasporcamada", "caixasporcamadalastro", "lastro", "casesperlayer"],
    "pallet_layers": ["camadas", "camadasnopalete", "layers"],
    "store_pref": ["guardarem", "guardaremautomaticounidadescaixa", "guardar", "storepref"],
    "unitization": ["unitizacaodosku", "unitizacaodoskuconformeafilialnunca", "unitizacao", "unitization"],
    "mixed_packs": ["embalagensdiferentesporbin", "embalagensdiferentesporbinsimnao", "mixedpacks"],
}
SKU_PACK_COLS = {
    "sku": ["sku", "codigo", "codproduto", "produto", "item"],
    "model": ["modelodeembalagem", "modelo", "embalagem", "model"],
    "inner": ["feitade", "feitademodelovaziounidades", "dentro", "inner"],
    "qty": ["quantidade", "qtd", "qtde", "qty"],
    "wt": ["pesocheia", "pesocheiakg", "peso", "pesokg"],
    "enabled": ["habilitada", "habilitadasimnao", "enabled"],
    "sort": ["ordem", "order"],
    "pallet": ["palete", "paletetipodebin", "pallet"],
    "layer": ["caixasporcamada", "caixasporcamadalastro", "lastro"],
    "layers": ["camadas", "layers"],
}
PACK_ORDER = ("case_qty", "case_model", "case_h", "case_w", "case_l", "case_wt", "pallet", "pallet_layer",
              "pallet_layers", "store_pref")
PACKAGING_COLS = {
    "code": ["codigo", "cod", "code", "modelo"],
    "name": ["nome", "descricao"],
    "height": ["altura", "alturacm"],
    "width": ["largura", "larguracm"],
    "length": ["comprimento", "comprimentocm"],
    "tare": ["tara", "tarakg", "pesovazio", "peso", "pesokg"],
    "inner": ["contem", "contemmodelo", "feitade", "inner"],
    "inner_qty": ["quantidade", "qtd", "qtde", "qty"],
}
ORDER_COLS = {
    "order": ["pedido", "numeropedido", "npedido", "idpedido", "codigopedido", "ordem", "documento", "nf",
              "notafiscal", "order", "orderid"],
    "sku": ["sku", "codigo", "codigosku", "codproduto", "codigodoproduto", "codigoproduto", "item", "produto"],
    "qty": ["quantidade", "qtd", "qtde", "quantity", "saida", "unidades"],
    "date": ["data", "datapedido", "emissao", "dataemissao", "date"],
    # opcional: horário em que o pedido foi liberado para separação (hora ou data e hora)
    "release": ["liberacao", "horaliberacao", "horariodeliberacao", "horarioliberacao", "datahoraliberacao",
                "dataliberacao", "liberadoem", "liberacaoseparacao", "liberacaoparaseparacao", "release", "releasetime",
                "released", "releasedat"],
}
MAX_PAIR_LINES = 40  # pedidos com mais SKUs que isso não entram na contagem de pares (combinações demais)
MAX_PAIRS = 5000  # pares guardados (os que mais saem juntos)
MAX_SAMPLE = 3000  # pedidos guardados (amostra) para o simulador de trajeto
MAX_DAY_ORDERS = 5000  # com data: dias com mais pedidos que isso não entram na amostra (simulação pesada demais)
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
    # do tipo de estrutura (opcionais): condição de armazenagem e porta
    "condition": ["condicao", "condicaodearmazenagem", "condicaoarmazenagem", "armazenagem"],
    "door": ["porta", "tipodeporta", "door"],
    "door_leaves": ["folhas", "folhasdaporta", "nfolhas"],
    "door_angle": ["abertura", "aberturadaporta", "aberturagraus", "aberturamaxima"],
    "door_time": ["tempodaporta", "tempodaportas", "tempoporta", "tempodaportasegundos"],
    "shared": ["vizinhoscompartilhamalateral", "vizinhoscompartilhamalateralsimnao", "compartilhaalateral",
               "lateralcompartilhada", "sharedsides"],
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
    "open_top": ["abertaemcima", "abertaemcimasimnao", "aberta", "semtampa", "opentop"],
    "over_cm": ["maximoacimadaborda", "maximoacimadabordacm", "acimadaborda", "maxacima", "overhang"],
    "store_as": ["armazenaem", "armazenaemunidadescaixasambospalete", "armazena", "storeas", "unitizacao"],
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
    "unitization": ["unitizacao", "unitizacaodesligadasocaixascaixasepaletes", "unitization"],
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
          "orders": ["pedidos", "historicodepedidos", "historico", "pedidosdoperiodo", "vendas", "saidas"],
          "sku_packs": ["embalagensdosskus", "embalagensdossku", "embalagensporsku", "skupackagings"],
          "packaging": ["embalagens", "modelosdeembalagem", "modelosdeembalagens", "caixaspadrao", "packaging",
                        "embalajes", "emballages", "verpackungen", "包装"]}


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
        errs.append(_("linha {line}: {label} inválido ({raw}).", line=line, label=label, raw=f"{raw!r}"))
        return None
    if v is not None and v < 0:
        errs.append(_("linha {line}: {label} negativo.", line=line, label=label))
        return None
    return float(v) if v is not None else None


BUILDING_COLS = {"field": ["campo", "parametro", "item", "descricao"], "value": ["valor", "value", "medida"]}
SPECS = {"building": BUILDING_COLS, "structures": STRUCT_COLS, "bins": BIN_COLS, "skus": SKU_COLS,
         "zones": ZONE_COLS, "obstacles": OBSTACLE_COLS, "sectors": SECTOR_COLS, "orders": ORDER_COLS,
         "packaging": PACKAGING_COLS, "sku_packs": SKU_PACK_COLS}
KIND_LABELS = {"building": N_("Imóvel"), "structures": N_("Estruturas"), "bins": N_("Bins"), "skus": "SKUs", "zones": N_("Zonas"),
               "obstacles": N_("Obstáculos"), "sectors": N_("Setores"), "orders": N_("Pedidos (histórico de saídas)"),
               "packaging": N_("Embalagens (modelos de caixa)"), "sku_packs": N_("Embalagens dos SKUs")}
FIELD_LABELS = {
    "building": {"field": N_("Campo (nome do parâmetro)"), "value": N_("Valor")},
    "structures": {"code": N_("Código"), "name": N_("Nome"), "kind": N_("Tipo"), "width": N_("Frente (cm)"), "depth": N_("Profundidade (cm)"),
                   "height": N_("Altura total (cm)"), "levels": N_("Nº de níveis"), "level_height": N_("Vão útil (cm)"),
                   "max_load": N_("Carga por nível (kg)"), "cost": N_("Custo por módulo (R$)"), "mode": N_("Modo"),
                   "qty": N_("Qtd fixa"), "min_qty": N_("Mínimo"), "max_qty": N_("Máximo"),
                   "golden_from": N_("Zona de ouro início (cm)"), "golden_to": N_("Zona de ouro fim (cm)"),
                   "first_level": N_("Altura do 1º nível (cm)"), "level_heights": N_("Vãos por nível (cm, ex.: 45; 40; 35)"),
                   "condition": N_("Condição de armazenagem"), "door": N_("Porta (sem, de giro, de correr)"),
                   "door_leaves": N_("Folhas da porta"), "door_angle": N_("Abertura da porta (°)"),
                   "door_time": N_("Tempo da porta (s)"), "shared": N_("Vizinhos compartilham a lateral (sim/não)")},
    "bins": {"code": N_("Código"), "name": N_("Nome"), "height": N_("Altura (cm)"), "width": N_("Largura (cm)"),
             "length": N_("Comprimento (cm)"), "max_weight": N_("Peso máx. (kg)"), "max_volume": "Volume (L)",
             "cost": N_("Custo por bin (R$)"), "open_top": N_("Aberta em cima (sim/não)"),
             "over_cm": N_("Máximo acima da borda (cm)"),
             "store_as": N_("Armazena em (unidades/caixas/ambos/palete)")},
    "skus": {"sku": "SKU", "name": N_("Produto"), "material": N_("Tipo de material"), "h": N_("Altura (cm)"), "w": N_("Largura (cm)"),
             "l": N_("Comprimento (cm)"), "wt": N_("Peso unitário (kg)"), "stock": N_("Saldo"), "min": N_("Min level"),
             "max": N_("Max level"), "out": N_("Saída (linhas ou unid./período)"),
             "condition": N_("Condição de armazenagem"), "case_qty": N_("Unidades por caixa"),
             "case_model": N_("Modelo de embalagem"), "case_h": N_("Altura da caixa (cm)"),
             "case_w": N_("Largura da caixa (cm)"), "case_l": N_("Comprimento da caixa (cm)"),
             "case_wt": N_("Peso da caixa cheia (kg)"), "pallet": N_("Palete (tipo de bin)"),
             "pallet_layer": N_("Caixas por camada (lastro)"), "pallet_layers": N_("Camadas"),
             "store_pref": N_("Guardar em (automático/unidades/caixa)"),
             "unitization": N_("Unitização do SKU (conforme a filial/nunca)"),
             "mixed_packs": N_("Embalagens diferentes por bin (sim/não)")},
    "sku_packs": {"sku": "SKU", "model": N_("Modelo de embalagem"), "inner": N_("Feita de (modelo; vazio = unidades)"),
                  "qty": N_("Quantidade"), "wt": N_("Peso cheia (kg)"),
                  "enabled": N_("Habilitada (sim/não)"), "sort": N_("Ordem"), "pallet": N_("Palete (tipo de bin)"),
                  "layer": N_("Caixas por camada"), "layers": N_("Camadas")},
    "packaging": {"code": N_("Código"), "name": N_("Nome"), "height": N_("Altura (cm)"), "width": N_("Largura (cm)"),
                  "length": N_("Comprimento (cm)"), "tare": N_("Tara (kg)"), "inner": N_("Contém (modelo)"),
                  "inner_qty": N_("Quantidade")},
    "zones": {"name": N_("Nome"), "length": N_("Comprimento em X (m)"), "orientation": N_("Orientação das fileiras"),
              "aisle": "Corredor (m)", "cross": N_("Corredores transversais"), "structures": N_("Estruturas permitidas (códigos)"),
              "x": "X (m)", "y": "Y (m)", "width": N_("Largura em Y (m)")},
    "obstacles": {"name": N_("Nome"), "x": "X (m)", "y": "Y (m)", "length": N_("Comprimento em X (m)"),
                  "width": N_("Largura em Y (m)")},
    "sectors": {"name": N_("Nome"), "area": N_("Área necessária (m²)"), "min_side": N_("Lado mínimo (m)"),
                "place": N_("Onde (automático, docas, fundo, lateral, fixa)"), "x": "X (m)", "y": "Y (m)",
                "length": N_("Comprimento em X (m)"), "width": N_("Largura em Y (m)")},
    "orders": {"order": N_("Pedido (identificador)"), "sku": "SKU", "qty": N_("Quantidade"), "date": N_("Data"),
               "release": N_("Liberação (horário, opcional)")},
}
REQUIRED = {"building": ["field", "value"], "structures": ["code"], "bins": ["code"], "skus": ["sku"],
            "zones": ["orientation"], "obstacles": ["x", "y", "length", "width"],
            "sectors": ["name"], "orders": ["order", "sku", "qty"], "packaging": ["code"],
            "sku_packs": ["sku", "model"]}


YES_WORDS = {"sim", "s", "1", "x", "true", "yes", "y", "si", "oui", "ja", "是"}


def _sku_packs(rows_all: list[list[str]], cm: dict, errs: list) -> dict:
    """Aba Embalagens dos SKUs: {sku: [embalagem]} — uma linha por SKU × modelo de embalagem (× palete); repetir a
    linha com outro palete acrescenta a paletização."""
    hdr, *rows = rows_all
    out: dict[str, list] = {}
    for i, row in enumerate(rows, start=2):
        sku, model = _cell(row, cm["sku"])[:60], normalize_code(_cell(row, cm["model"]))
        if not sku or not model:
            continue
        e: list[str] = []
        v = {k: _dec(row, cm.get(k), FIELD_LABELS["sku_packs"][k], e, i) for k in ("qty", "wt", "sort", "layer", "layers")}
        if e:
            errs.extend(f"{_('Embalagens dos SKUs')} {x}" for x in e[:3])
            continue
        lst = out.setdefault(sku, [])
        r = next((x for x in lst if x["model"] == model), None)
        if r is None:
            en = normalize_header(_cell(row, cm.get("enabled")))
            inner = normalize_code(_cell(row, cm.get("inner")))
            r = {"model": model, "inner": "" if inner.casefold() in ("unidade", "unidades", "un", "units", "unit") else inner,
                 "qty": int(v["qty"]) if v["qty"] else None, "wt": v["wt"],
                 "enabled": (en in YES_WORDS) if en else True,
                 "sort": int(v["sort"]) if v["sort"] else len(lst) + 1, "pallets": []}
            lst.append(r)
        pal = normalize_code(_cell(row, cm.get("pallet")))
        if pal:
            r["pallets"].append([pal, int(v["layer"]) if v["layer"] else None, int(v["layers"]) if v["layers"] else None])
    return out


def _pack_cells(row: list[str], cm: dict, it: dict, errs: list, line: int) -> None:
    """Embalagem da linha da aba SKUs (colunas opcionais) para o item do projeto."""
    from services.product_importer import PREF_WORDS

    for k in ("case_qty", "pallet_layer", "pallet_layers"):
        if cm.get(k) is None or not _cell(row, cm[k]):
            continue
        v = _dec(row, cm[k], FIELD_LABELS["skus"][k], errs, line)
        if v is not None and (v <= 0 or v != int(v)):
            errs.append(_("linha {line}: {label}: número inteiro maior que zero.", line=line,
                          label=_(FIELD_LABELS["skus"][k])))
        elif v is not None:
            it[k] = int(v)
    for k in ("case_h", "case_w", "case_l", "case_wt"):
        if cm.get(k) is not None and _cell(row, cm[k]):
            v = _dec(row, cm[k], FIELD_LABELS["skus"][k], errs, line)
            if v:
                it[k] = v
    for k in ("case_model", "pallet"):
        if cm.get(k) is not None and _cell(row, cm[k]):
            it[k] = normalize_code(_cell(row, cm[k]))
    un = normalize_header(_cell(row, cm.get("unitization"))) if cm.get("unitization") is not None else ""
    if un:
        it["unitization"] = "never" if un.startswith(("nunca", "never", "nao", "no")) else "branch"
    mx = normalize_header(_cell(row, cm.get("mixed_packs"))) if cm.get("mixed_packs") is not None else ""
    if mx:
        it["mixed_packs"] = mx in YES_WORDS
    pref = normalize_header(_cell(row, cm.get("store_pref"))) if cm.get("store_pref") is not None else ""  # aceita o texto traduzido
    if pref:
        v = next((k for k, ws in PREF_WORDS.items() if any(pref.startswith(w) for w in ws)), None)
        if v is None:
            errs.append(_("linha {line}: Guardar em inválido: {raw} (use automático, unidades ou caixa)", line=line, raw=pref))
        else:
            it["store_pref"] = v


def _pack_notes(db: Session, items: list[dict]) -> list[str]:
    """Avisos da embalagem dos SKUs do projeto: códigos de modelo/palete não cadastrados, unidades que não cabem na
    caixa, lastro que não cabe na base; e quantos tiveram unidades por caixa ou o palete sugeridos pelas medidas."""
    from services.packaging import catalogs, check, item_has_pack, item_packaging, load_products

    with_pack = [it for it in items if item_has_pack(it)]
    if not with_pack:
        return []
    models, pallets = catalogs(db)
    ids = [p.id for p in db.scalars(select(Product).where(Product.sku.in_([it["sku"] for it in with_pack][:900])))]
    cat = {p.sku: p for p in load_products(db, ids).values()}
    out, warns, unknown_m, unknown_p, sug_q, sug_p = [], [], set(), set(), 0, 0
    for it in with_pack:
        if it.get("case_model") and it["case_model"] not in models:
            unknown_m.add(it["case_model"])
        if it.get("pallet") and it["pallet"] not in pallets:
            unknown_p.add(it["pallet"])
        k, prod, sug = item_packaging(it, cat.get(it["sku"]), models, pallets)
        sug_q += "case_qty" in sug
        sug_p += "pallet" in sug
        for w in check(k, prod):
            warns.append(f"{it['sku']}: {w}")
    out.append(_("Embalagem: {n} SKU(s) com caixa ou palete na planilha.", n=len(with_pack)))
    if sug_q:
        out.append(_("Embalagem: {n} SKU(s) com unidades por caixa calculadas pelas medidas.", n=sug_q))
    if sug_p:
        out.append(_("Embalagem: {n} SKU(s) com caixas por camada e camadas do palete calculadas pelas medidas.", n=sug_p))
    if unknown_m:
        out.append(_("Embalagem: modelo(s) não cadastrado(s), ignorado(s): {codes}. Cadastre em Modelos de embalagem ou na aba Embalagens.",
                     codes=", ".join(sorted(unknown_m)[:10])))
    if unknown_p:
        out.append(_("Embalagem: palete(s) não cadastrado(s) como tipo de bin com Armazena em = palete fechado, ignorado(s): {codes}.",
                     codes=", ".join(sorted(unknown_p)[:10])))
    out.extend(warns[:10])
    if len(warns) > 10:
        out.append(_("Embalagem: mais {v} aviso(s).", v=len(warns) - 10))
    return out


def _sheet_pack_notes(db: Session, items: list[dict], sku_packs: dict) -> list[str]:
    """Avisos da aba Embalagens dos SKUs: SKUs fora da aba SKUs, modelos/paletes não cadastrados, conteúdo ou lastro
    que não cabe; e o que foi calculado pelas medidas."""
    from types import SimpleNamespace

    from services.packaging import catalogs, check, fill, sheet_packagings

    if not sku_packs:
        return []
    models, pallets = catalogs(db)
    by = {it["sku"]: it for it in items}
    out, warns, calc = [], [], 0
    unknown = sorted({r["model"] for rows in sku_packs.values() for r in rows if r["model"] not in models}
                     | {p[0] for rows in sku_packs.values() for r in rows for p in r["pallets"] if p[0] not in pallets})
    missing = [sku for sku in sku_packs if sku not in by]
    for sku, rows in sku_packs.items():
        it = by.get(sku) or {}
        prod = SimpleNamespace(height=it.get("h"), width=it.get("w"), length=it.get("l"), weight=it.get("wt"))
        for k in sheet_packagings(rows, models, pallets):
            calc += len(fill(k, prod))
            warns.extend(f"{sku}: {w}" for w in check(k, prod))
    if calc:
        out.append(_("Embalagens dos SKUs: {n} valor(es) calculado(s) pelas medidas (quantidade, caixas por camada, camadas).", n=calc))
    if unknown:
        out.append(_("Embalagens dos SKUs: códigos não cadastrados (modelo ou palete fechado), ignorados: {codes}.",
                     codes=", ".join(unknown[:10])))
    if missing:
        out.append(_("Embalagens dos SKUs: {n} SKU(s) que não estão na aba SKUs: {skus}.", n=len(missing),
                     skus=", ".join(missing[:10])))
    out.extend(warns[:10])
    if len(warns) > 10:
        out.append(_("Embalagem: mais {v} aviso(s).", v=len(warns) - 10))
    return out


def read_sheets(path: Path, limit: Optional[int] = None) -> dict[str, list[list[str]]]:
    """Todas as abas (nome -> linhas não vazias, cabeçalho incluso). CSV = uma aba "CSV".
    `limit` = máximo de linhas lidas por aba (prévia)."""
    from itertools import islice

    if path.suffix.lower() in {".csv", ".txt"}:
        return {"CSV": [r for _x, r in islice(iter_rows(path, has_header=False), limit)]}
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
        return cfg, msgs, errs or [_("Nenhuma aba para importar (use Imóvel, Estruturas, Bins e SKUs — baixe o modelo).")]
    cfg["import_mapping"] = {**(cfg.get("import_mapping") or {}), **remember(
        {k: m for k, m in mapping.items() if k in sheets}, all_sheets)}

    # ---- Imóvel ----
    if "building" in sheets:
        n = 0
        from services.site_service import BUILDING_FIELDS

        lookup = {normalize_header(s): k for k, syns in BUILDING_KEYS.items() for s in syns}
        lookup.update({normalize_header(label): k for k, label, _x, _x in BUILDING_FIELDS})
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
            elif key == "unitization":  # vai para a demanda do projeto
                v = normalize_header(val)
                cfg["demand"]["unitization"] = ("full" if "palete" in v or "pallet" in v or v.startswith(("full", "ligada"))
                                                else "cases" if "caixa" in v or "case" in v else "off")
            elif key == "serpentine":
                cfg["building"][key] = "1" if normalize_header(val) in {"sim", "s", "1", "true", "yes"} else ""
            elif key == "address_mode":
                cfg["building"][key] = "row" if any(w in normalize_header(val) for w in ("rua", "fileira", "row")) else "aisle"
            else:
                try:
                    parse_decimal(val)
                except ValueError:
                    errs.append(_("Imóvel: valor inválido para {cell} ({val}).", cell=_cell(row, fi), val=f"{val!r}"))
                    continue
                cfg["building"][key] = val.replace(".", ",") if val.count(".") == 1 and "," not in val else val
            n += 1
        msgs.append(_("Imóvel: {n} campo(s) atualizado(s).", n=n))

    # ---- Bins ----
    if "bins" in sheets and len(sheets["bins"]) > 1:
        hdr, *rows = sheets["bins"]
        cm = cms["bins"]
        if cm["code"] is None:
            errs.append(_("Bins: coluna Código não encontrada."))
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
                # aberta em cima (a carga pode passar da borda) e o máximo acima da borda: colunas opcionais
                o_raw = _cell(row, cm.get("open_top")) if cm.get("open_top") is not None else ""
                over = _dec(row, cm.get("over_cm"), "over_cm", e, i) if cm.get("over_cm") is not None else None
                opened = (normalize_header(o_raw) in {"sim", "s", "1", "x", "true", "yes", "y", "si", "oui", "ja", "是"}
                          if str(o_raw).strip() else (True if over else None))
                store = None  # Armazena em (unitização): coluna opcional
                s_raw = normalize_header(_cell(row, cm.get("store_as"))) if cm.get("store_as") is not None else ""
                if s_raw:
                    from services.master_importer import STORE_WORDS

                    store = next((m for m, ws in STORE_WORDS.items() if any(s_raw.startswith(re.sub(r"[^a-z]", "", w)) for w in ws)), None)
                    if store is None:
                        errs.append(_("Bins linha {i} ({code}): Armazena em inválido: {v} (use unidades, caixas, ambos ou palete)",
                                      i=i, code=code, v=s_raw))
                if code in existing:  # tipo já cadastrado: dimensões e abertura mantidas; custo e Armazena em atualizados
                    kept += 1
                    bt = by_code.get(code)
                    if bt is not None and cost is not None and not e and bt.cost != Decimal(str(cost)):
                        bt.cost = Decimal(str(cost))
                        priced += 1
                    if bt is not None and store and bt.store_as != store:
                        bt.store_as = store
                    continue
                vals = {k: _dec(row, cm[k], k, e, i) for k in ("height", "width", "length", "max_weight", "max_volume")}
                vals["cost"] = cost
                if e or not all(vals[k] for k in ("height", "width", "length")):
                    errs.append(_("Bins linha {i} ({code}): informe altura, largura e comprimento válidos.", i=i, code=code))
                    continue
                db.add(BinType(code=code[:30], name=(_cell(row, cm["name"]) or code)[:150],
                               **{k: Decimal(str(v)) if v is not None else None for k, v in vals.items()},
                               open_top=bool(opened), over_cm=Decimal(str(over)) if (opened and over) else None,
                               store_as=store or "units"))
                existing.add(code)
                created += 1
            db.flush()
            msgs.append(_("Bins: {created} tipo(s) cadastrado(s), {kept} já existente(s) mantido(s)", created=created, kept=kept)
                        + (f" ({priced} com custo atualizado)." if priced else "."))

    # ---- Embalagens (modelos de caixa) ----
    if "packaging" in sheets and len(sheets["packaging"]) > 1:
        from models import PackagingModel

        hdr, *rows = sheets["packaging"]
        cm = cms["packaging"]
        by_code = {m.code: m for m in db.scalars(select(PackagingModel))}
        created = updated = 0
        for i, row in enumerate(rows, start=2):
            code = normalize_code(_cell(row, cm["code"]))
            if not code:
                continue
            e: list[str] = []
            vals = {k: _dec(row, cm.get(k), k, e, i) for k in ("height", "width", "length", "tare")}
            m = by_code.get(code)
            if e or (m is None and not all(vals[k] for k in ("height", "width", "length"))):
                errs.append(_("Embalagens linha {i} ({code}): informe altura, largura e comprimento válidos.", i=i, code=code))
                continue
            if m is None:
                m = PackagingModel(code=code[:30], name="")
                db.add(m)
                by_code[code] = m
                created += 1
            else:
                updated += 1
            m.name = (_cell(row, cm.get("name")) or m.name or code)[:150]
            for k, v in vals.items():
                if v is not None:
                    setattr(m, k, Decimal(str(v)))
            inner = normalize_code(_cell(row, cm.get("inner")))
            if inner:  # hierarquia: este modelo contém N de outro (linha anterior ou já cadastrado)
                if inner in by_code and inner != code:
                    q = _dec(row, cm.get("inner_qty"), "inner_qty", errs, i)
                    m.inner_model = by_code[inner]
                    m.inner_qty = int(q) if q else None
                    if not m.inner_qty and all(getattr(m, a) for a in ("height", "width", "length")):
                        from services.packaging import fit_in_case

                        im = by_code[inner]
                        m.inner_qty = fit_in_case((float(im.height), float(im.width), float(im.length)), None,
                                                  (float(m.height), float(m.width), float(m.length))) or None
                else:
                    errs.append(_("Embalagens linha {i} ({code}): modelo {v} não cadastrado.", i=i, code=code, v=inner))
        db.flush()
        msgs.append(_("Embalagens: {created} modelo(s) cadastrado(s), {updated} atualizado(s).", created=created, updated=updated))

    # ---- Estruturas ----
    if "structures" in sheets and len(sheets["structures"]) > 1:
        hdr, *rows = sheets["structures"]
        cm = cms["structures"]
        if cm["code"] is None:
            errs.append(_("Estruturas: coluna Código não encontrada."))
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
                    errs.append(_("Estruturas linha {i} ({code}): informe frente, profundidade, altura, níveis e vão.", i=i, code=code))
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
                    errs.append(_("Estruturas linha {i} ({code}): zona de ouro precisa de início e fim (fim maior).", i=i, code=code))
                if cm.get("first_level") is not None and _cell(row, cm["first_level"]):
                    fl = _dec(row, cm["first_level"], _("1º nível"), errs, i)
                    st.first_level_cm = Decimal(str(fl)) if fl is not None else st.first_level_cm
                if cm.get("level_heights") is not None and _cell(row, cm["level_heights"]):
                    try:
                        hs = [float(x.replace(",", ".")) for x in re.split(r"[;/|\s]+", _cell(row, cm["level_heights"])) if x]
                        st.level_heights = [h for h in hs if h > 0][:st.levels or 50] or None
                    except ValueError:
                        errs.append(_("Estruturas linha {i} ({code}): vãos por nível inválidos (use 45; 40; 35).", i=i, code=code))
                from services.structure_kinds import attach_kind

                with db.no_autoflush:  # depois das medidas: a estrutura nova só vai ao banco completa
                    attach_kind(db, st)  # o tipo escrito na planilha vira (ou usa) o tipo de estrutura cadastrado
                    _kind_extras(db, st, row, cm)
                total = float(st.first_level_cm or 0) + sum(st.vaos())
                if st.height and total > float(st.height) + 1e-6:
                    errs.append(_("Estruturas linha {i} ({code}): 1º nível + vãos ({total} cm) passam da altura total ({float} cm) — altura do 1º nível e vãos por nível não foram gravados.", i=i, code=code, total=f"{total:g}", float=f"{float(st.height):g}"))
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
            msgs.append(_("Estruturas: {created} cadastrada(s), {updated} atualizada(s)", created=created, updated=updated)
                        + (_(", {len} fora da planilha marcada(s) como \"Não usar\".", len=len(off)) if off else "."))

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
                errs.extend(_("Zonas {x}", x=x) for x in e)
                continue
            codes = [normalize_code(c) for c in re.split(r"[,;/ ]+", _cell(row, cm["structures"])) if c.strip()]
            unknown = [c for c in codes if c not in code_id]
            if unknown:
                bad.append(_("linha {i}: estrutura(s) {v} não cadastrada(s)", i=i, v=', '.join(unknown)))
            fmt = lambda v: (f"{v:g}".replace(".", ",") if v is not None else "")  # noqa: E731
            zones.append({"name": (_cell(row, cm["name"]) or _("Zona {v}", v=len(zones) + 1))[:40], "length_m": fmt(ln),
                          "orientation": ori, "aisle_m": fmt(al), "cross_aisles": str(int(cr)) if cr else "",
                          "x_m": fmt(zx), "y_m": fmt(zy), "width_m": fmt(zw),
                          "structures": [str(code_id[c]) for c in codes if c in code_id]})
        if bad:
            errs.append("Zonas: " + "; ".join(bad) + ".")
        if len(zones) == 1 and not (zones[0]["x_m"] and zones[0]["y_m"]):
            errs.append(_("Zonas: com zonas empilhadas (sem X/Y), informe ao menos duas; a zona foi ignorada."))
            zones = []
        cfg["building"]["zones"] = zones
        msgs.append(_("Zonas: {len} zona(s) definida(s).", len=len(zones)) if zones else _("Zonas: nenhuma (planta de zona única)."))

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
                errs.append(_("Obstáculos linha {i}: informe X, Y, comprimento e largura válidos.", i=i))
                continue
            fmt = lambda v: f"{v:g}".replace(".", ",")  # noqa: E731
            obs.append({"name": (_cell(row, cm["name"]) or _("Obstáculo {v}", v=len(obs) + 1))[:40], "x_m": fmt(vals["x"]),
                        "y_m": fmt(vals["y"]), "length_m": fmt(vals["length"]), "width_m": fmt(vals["width"])})
        cfg["building"]["obstacles"] = obs
        msgs.append(_("Obstáculos: {len} área(s) bloqueada(s).", len=len(obs)))

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
                errs.append(_("Setores linha {i}: informe a área (ou X, Y, comprimento e largura para posição fixa).", i=i))
                continue
            secs.append({"name": name or _("Setor {v}", v=len(secs) + 1), "area_m2": fmt(vals["area"]),
                         "min_side_m": fmt(vals["min_side"]), "place": place, "x_m": fmt(vals["x"]), "y_m": fmt(vals["y"]),
                         "length_m": fmt(vals["length"]), "width_m": fmt(vals["width"])})
        cfg["building"]["sectors"] = secs
        msgs.append(_("Setores: {len} setor(es).", len=len(secs)))

    # ---- Pedidos (histórico de saídas) ----
    if "orders" in sheets:
        res = _orders(sheets["orders"], cms["orders"], bool((mapping.get("orders") or {}).get("abs")))
        if res.get("error"):
            errs.append(res["error"])
        else:
            cfg["orders"] = res
            neg = (f" {res['neg']} quantidade(s) negativa(s) "
                   + ("convertida(s) para valor absoluto." if res["abs"] else _("ignorada(s) (sem a conversão)."))) if res["neg"] else ""
            msgs.append(_("Pedidos: {orders} pedido(s), {lines} linha(s), {skus} SKU(s), {pairs} par(es) que saem juntos.{neg}", orders=res['orders'], lines=res['lines'], skus=len(res['skus']), pairs=len(res['pairs']), neg=neg))

    # ---- Embalagens dos SKUs (várias por SKU, em hierarquia) ----
    if "sku_packs" in sheets and len(sheets["sku_packs"]) > 1:
        cfg["sku_packs"] = _sku_packs(sheets["sku_packs"], cms["sku_packs"], errs)
        n = sum(len(v) for v in cfg["sku_packs"].values())
        msgs.append(_("Embalagens dos SKUs: {n} embalagem(ns) de {skus} SKU(s).", n=n, skus=len(cfg["sku_packs"])))

    # ---- SKUs ----
    if "skus" in sheets:
        if len(sheets["skus"]) < 2:
            errs.append(_("SKUs: a aba não tem linhas."))
        else:
            hdr, *rows = sheets["skus"]
            cm = cms["skus"]
            if cm["sku"] is None:
                errs.append(_("SKUs: coluna SKU não encontrada."))
            elif all(cm[k] is None for k in ("stock", "min", "max")):
                errs.append(_("SKUs: informe ao menos uma coluna de quantidade (Saldo, Min level ou Max level)."))
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
                    out = _dec(row, cm.get("out"), _("saída"), e, i)
                    if out is not None:
                        it["out"] = out
                    cond = condition_code(_cell(row, cm.get("condition")))
                    if cond:
                        it["condition"] = cond
                    _pack_cells(row, cm, it, e, i)
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
                        errs.append(_("SKUs: limite de {MAX_ITEMS} SKUs atingido; o resto foi ignorado.", MAX_ITEMS=MAX_ITEMS))
                        break
                if bad > 10:
                    errs.append(_("SKUs: mais {v} linha(s) com erro.", v=bad - 10))
                cfg["items"] = list(items.values())
                cfg["demand"]["source"] = "upload"
                msgs.extend(_pack_notes(db, cfg["items"]))
                msgs.extend(_sheet_pack_notes(db, cfg["items"], cfg.get("sku_packs") or {}))
                msgs.append(_("SKUs: {len} SKU(s) carregado(s) no projeto (fonte da demanda = arquivo).", len=len(items)))
    return cfg, msgs, errs


def _iso_date(raw: str) -> str:
    """Data como AAAA-MM-DD; vazio se não reconhecer. Aceita AAAA-MM-DD (com hora ou não), AAAA/MM/DD,
    DD/MM/AAAA, DD.MM.AAAA e DD-MM-AAAA; MM/DD/AAAA quando o 2º número passa de 12 ou o idioma é o inglês."""
    import re

    from services.i18n import get_lang

    raw = (raw or "").strip()
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}" if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31 else ""
    m = re.match(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", raw)
    if m:
        a, b, y = (int(x) for x in m.groups())
        y = 2000 + y if y < 100 else y
        d, mo = (b, a) if b > 12 or (get_lang() == "en" and a <= 12) else (a, b)
        return f"{y}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else ""
    return ""


def _time_of_day(raw: str) -> Optional[int]:
    """Horário (segundos desde 0h) de "08:30", "8:30:15", "2026-10-05 08:30", "2026-10-05T08:30:00",
    "05/10/2026 08:30" ou da fração de dia do Excel (0,354 = 08:30). None se não reconhecer."""
    import re

    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"(?:^|[\sT])(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s if h < 24 and mi < 60 and s < 60 else None
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        return None
    frac = v % 1 if v >= 1 else v  # número de série do Excel com hora: só a fração do dia
    return int(round(frac * 86400)) % 86400 if 0 <= v and (frac > 0 or v == 0) else None


def _orders(rows_all: list[list[str]], cm: dict, to_abs: bool) -> dict:
    """Resumo do histórico de pedidos (o arquivo bruto não fica no projeto): por SKU, linhas de pedido (em quantos
    pedidos aparece) e unidades; pares de SKUs que aparecem no mesmo pedido (os MAX_PAIRS mais frequentes).
    Quantidade negativa = saída em alguns relatórios: com `to_abs` vira positiva; sem, a linha é ignorada."""
    from collections import Counter
    from itertools import combinations

    if len(rows_all) < 2:
        return {"error": _("Pedidos: a aba não tem linhas.")}
    _hdr, *rows = rows_all
    lines: dict[tuple, float] = {}
    neg = ignored = bad = 0
    dates = []
    day_of: dict[str, str] = {}
    rel_of: dict[str, int] = {}  # pedido -> horário de liberação (segundos desde 0h)
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
                day_of.setdefault(order[:60], d)  # data do pedido (a da 1ª linha)
        if cm.get("release") is not None:  # liberação do pedido: o menor horário entre as linhas dele
            raw = _cell(row, cm["release"])
            t = _time_of_day(raw)
            if t is not None:
                rel_of[order[:60]] = min(t, rel_of.get(order[:60], t))
            d = _iso_date(raw)
            if d and not day_of.get(order[:60]):  # sem a coluna Data: a data que vier junto com a liberação
                dates.append(d)
                day_of[order[:60]] = d
    if not lines:
        return {"error": _("Pedidos: nenhuma linha válida (pedido, SKU e quantidade)")
                         + (_("; {neg} quantidade(s) negativa(s) ignorada(s) — marque a conversão para valor absoluto.", neg=neg)
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
    per_day: dict[str, int] = {}
    for o in ids:
        if day_of.get(o):
            per_day[day_of[o]] = per_day.get(day_of[o], 0) + 1
    pick: list = []
    if per_day:
        # com data: dias inteiros (o simulador roda um dia completo), dos mais recentes para trás, até MAX_SAMPLE
        # pedidos (pelo menos um dia, até MAX_DAY_ORDERS); pedidos sem data não entram
        keep_days, total = set(), 0
        for day in sorted(per_day, reverse=True):
            n = per_day[day]
            if keep_days and total + n > MAX_SAMPLE or n > MAX_DAY_ORDERS:
                continue
            keep_days.add(day)
            total += n
        pick = [o for o in ids if day_of.get(o) in keep_days]
    if not per_day or not pick:
        pick = random.Random(1).sample(ids, min(MAX_SAMPLE, len(ids)))
    # [pedido, SKUs, unidades de cada SKU, data(, liberação)] — as unidades entram no tempo de pega (tempo por peça) do
    # simulador; a data permite simular um dia inteiro; a liberação (segundos desde 0h; None = pedido sem horário, fica
    # disponível desde o início) só existe quando a coluna foi mapeada
    has_rel = cm.get("release") is not None
    sample = []
    for o in sorted(pick):
        ss = sorted(set(orders[o]))
        e = [o, ss, [round(lines[(o, x)], 3) for x in ss], day_of.get(o, "")]
        if has_rel:
            e.append(rel_of.get(o))
        sample.append(e)
    return {"orders": len(orders), "lines": len(lines), "units": round(sum(lines.values()), 3), "sample": sample,
            "skus": {s: [n, round(u, 3)] for s, (n, u) in skus.items()}, "pairs": top, "neg": neg, "abs": to_abs,
            "ignored": ignored, "bad": bad, "big_orders": big,
            "dates": [min(dates), max(dates)] if dates else None, "has_release": has_rel,
            "release_missing": sum(1 for o in orders if o not in rel_of) if has_rel else 0,
            "days": {d: n for d, n in sorted(per_day.items())}}


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
        for i, _x in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, 16)
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = _("Instruções")
    for line in [
        _("Modelo de importação — Projeto de filial nova"),
        "",
        _("Todas as abas são opcionais: envie só o que quiser atualizar. Não renomeie as abas."),
        _("Imóvel: medidas em metros. Orientação: automática, comprimento ou largura."),
        _("Estruturas: medidas em cm. Modo: Otimizar (o sistema decide a quantidade, com Mín./Máx. opcionais), "
        "Fixa (usa exatamente Qtd fixa) ou Não usar. Códigos existentes são atualizados."),
        _("Bins: medidas internas em cm, peso máx. em kg, volume em litros (vazio = calculado), custo em R$ por bin. "
        "Códigos já cadastrados mantêm as medidas; só o custo é atualizado."),
        _("SKUs: dimensões em cm e peso em kg por unidade; Saldo, Min level e Max level em unidades da filial nova. "
        "Dimensões vazias são completadas pelo cadastro de produtos (se o SKU existir). SKU repetido soma as quantidades."),
        _("Ao enviar a aba SKUs, o projeto passa a usar o arquivo como fonte da demanda (substitui a lista anterior)."),
        _("Embalagem (opcional, aba SKUs): unidades por caixa, modelo de embalagem (código da aba Embalagens ou do "
          "cadastro), medidas e peso da caixa cheia, palete (código de um tipo de bin com Armazena em = palete), caixas "
          "por camada, camadas e Guardar em (automático, unidades ou caixa). Vazios = o cadastro do produto; sem "
          "unidades por caixa ou sem o padrão do palete, o sistema calcula pelas medidas. Bins: Armazena em = unidades, "
          "caixas, ambos (caixas inteiras + o resto solto) ou palete (palete fechado)."),
        _("Zonas (opcional): com X e Y, cada zona é um retângulo em qualquer lugar do imóvel (X, Y, comprimento em X e "
        "largura em Y; largura vazia = até a parede) e o espaço entre elas é a circulação. Sem X e Y, as zonas (ao menos "
        "duas) são faixas empilhadas a partir da frente, na ordem das linhas, separadas por avenidas (largura na aba "
        "Imóvel); comprimento vazio = o que sobrar. Orientação = 'ao longo do comprimento' ou 'perpendicular'; Corredor "
        "vazio = o do imóvel; Estruturas permitidas = códigos separados por vírgula (vazio = todas). Aba vazia = sem zonas."),
        _("Obstáculos (opcional): áreas bloqueadas (escritório, pilar, sala…) em metros a partir do canto superior "
        "esquerdo da planta: X ao longo do comprimento, Y ao longo da largura. As fileiras que passariam por elas são cortadas."),
    ]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 130

    b = cfg["building"]
    orient = {"auto": _("automática"), "length": "comprimento", "width": "largura"}
    sheet(wb.create_sheet(_("Imóvel")), [_("Campo"), _("Valor")],
          [(label, b.get(k, "")) for k, label, _x, _x in BUILDING_FIELDS]
          + [(_("Orientação das fileiras"), orient.get(b.get("orientation") or "auto", _("automática"))),
             (_("Cabeceiras (sim/não)"), "sim" if b.get("end_caps") else _("não")),
             (_("Posição da cabeceira (encostada/borda)"), "borda" if b.get("cap_align") == "edge" else "encostada"),
             (_("Endereçamento (corredor/rua)"), "rua" if b.get("address_mode") == "row" else "corredor"),
             (_("Ruas em serpentina (sim/não)"), "sim" if b.get("serpentine") else _("não")),
             (_("Unitização (desligada/só caixas/caixas e paletes)"),
              {"cases": "só caixas", "full": "caixas e paletes"}.get(cfg["demand"].get("unitization"), "desligada"))],
          {1: 50, 2: 16})
    id_code = {str(s.id): s.code for s in db.scalars(select(StructureType))}
    ori_lbl = {"along": _("ao longo do comprimento"), "across": "perpendicular (ao longo da largura)"}
    sheet(wb.create_sheet(_("Zonas")),
          [_("Nome"), "X (m)", "Y (m)", _("Comprimento em X (m)"), _("Largura em Y (m)"), _("Orientação das fileiras"),
           "Corredor (m)", _("Corredores transversais"), _("Estruturas permitidas (códigos)")],
          [(z.get("name"), z.get("x_m") or None, z.get("y_m") or None, z.get("length_m") or None,
            z.get("width_m") or None, ori_lbl.get(z.get("orientation"), _("ao longo do comprimento")),
            z.get("aisle_m") or None, z.get("cross_aisles") or None,
            ", ".join(id_code.get(x, "") for x in z.get("structures") or [] if x in id_code) or None)
           for z in b.get("zones") or []], {1: 22, 6: 36, 9: 34})
    sheet(wb.create_sheet(_("Obstáculos")),
          [_("Nome"), "X (m)", "Y (m)", _("Comprimento em X (m)"), _("Largura em Y (m)")],
          [(o.get("name"), o.get("x_m"), o.get("y_m"), o.get("length_m"), o.get("width_m"))
           for o in b.get("obstacles") or []], {1: 24})
    place_lbl = {"auto": _("automático"), "front": "docas", "back": "fundo", "side": "lateral", "fixed": "fixa"}
    sheet(wb.create_sheet(_("Setores")),
          [_("Nome"), _("Área necessária (m²)"), _("Lado mínimo (m)"), _("Onde (automático, docas, fundo, lateral, fixa)"),
           "X (m)", "Y (m)", _("Comprimento em X (m)"), _("Largura em Y (m)")],
          [(sc.get("name"), sc.get("area_m2") or None, sc.get("min_side_m") or None, place_lbl.get(sc.get("place"), _("automático")),
            sc.get("x_m") or None, sc.get("y_m") or None, sc.get("length_m") or None, sc.get("width_m") or None)
           for sc in b.get("sectors") or []], {1: 22, 4: 40})
    mode_lbl = {"auto": _("Otimizar"), "fixed": _("Fixa"), "off": _("Não usar")}
    rows = []
    for st in db.scalars(select(StructureType).order_by(StructureType.code)):
        sc = cfg["structures"].get(str(st.id), {})
        f = lambda v: float(v) if v is not None else None  # noqa: E731
        rows.append((st.code, st.name, st.kind, f(st.width), f(st.depth), f(st.height), st.levels, f(st.level_height),
                     f(st.max_load), f(st.cost), mode_lbl.get(sc.get("mode") or "off"), sc.get("qty") or None,
                     sc.get("min_qty") or None, sc.get("max_qty") or None, f(st.golden_from), f(st.golden_to),
                     f(st.first_level_cm), "; ".join(f"{h:g}" for h in (st.level_heights or [])) or None))
    sheet(wb.create_sheet(_("Estruturas")),
          [_("Código"), _("Nome"), _("Tipo"), _("Frente (cm)"), _("Profundidade (cm)"), _("Altura (cm)"), N_("Níveis"), _("Vão útil (cm)"),
           _("Carga por nível (kg)"), _("Custo por módulo (R$)"), _("Modo"), _("Qtd fixa"), _("Mín."), _("Máx."),
           _("Zona de ouro início (cm)"), _("Zona de ouro fim (cm)"), _("Altura do 1º nível (cm)"), _("Vãos por nível (cm)")],
          rows, {2: 30})
    store_lbl = {"units": "unidades", "cases": "caixas", "both": "ambos", "pallet": "palete"}
    from models import PackagingModel

    sheet(wb.create_sheet(_("Embalagens")),
          [_("Código"), _("Nome"), _("Altura (cm)"), _("Largura (cm)"), _("Comprimento (cm)"), _("Tara (kg)"),
           _("Contém (modelo)"), _("Quantidade")],
          [(m.code, m.name, *(float(v) if v is not None else None for v in (m.height, m.width, m.length, m.tare)),
            m.inner_model.code if m.inner_model else None, m.inner_qty)
           for m in db.scalars(select(PackagingModel).order_by(PackagingModel.code))], {2: 30})
    sheet(wb.create_sheet(N_("Bins")),
          [_("Código"), _("Nome"), _("Altura (cm)"), _("Largura (cm)"), _("Comprimento (cm)"), _("Peso máx. (kg)"), "Volume (L)",
           _("Custo por bin (R$)"), _("Aberta em cima (sim/não)"), _("Máximo acima da borda (cm)"),
           _("Armazena em (unidades/caixas/ambos/palete)")],
          [(bt.code, bt.name, *(float(v) if v is not None else None for v in
                                (bt.height, bt.width, bt.length, bt.max_weight, bt.max_volume, bt.cost)),
            _("sim") if bt.open_top else _("não"), float(bt.over_cm) if bt.over_cm is not None else None,
            store_lbl.get(bt.store_as or "units"))
           for bt in db.scalars(select(BinType).where(BinType.parent_id.is_(None)).order_by(BinType.code))], {2: 30})
    pref_lbl = {"auto": _("automático"), "units": _("unidades"), "cases": _("caixa")}
    sheet(wb.create_sheet("SKUs"),
          ["SKU", N_("Produto"), _("Material"), _("Altura (cm)"), _("Largura (cm)"), _("Comprimento (cm)"), _("Peso (kg)"), _("Saldo"),
           _("Min level"), _("Max level"), _("Saída (linhas ou unid./período)"), _("Condição de armazenagem"),
           *(_(FIELD_LABELS["skus"][k]) for k in PACK_ORDER), _(FIELD_LABELS["skus"]["unitization"]),
           _(FIELD_LABELS["skus"]["mixed_packs"])],
          [(it["sku"], it.get("name"), it.get("material"), it.get("h"), it.get("w"), it.get("l"), it.get("wt"),
            it.get("stock"), it.get("min"), it.get("max"), it.get("out"), _cond_label(it.get("condition")),
            *(pref_lbl.get(it.get(k), it.get(k)) if k == "store_pref" else it.get(k) for k in PACK_ORDER),
            {"never": "nunca", "branch": "conforme a filial"}.get(it.get("unitization")),
            None if it.get("mixed_packs") is None else ("sim" if it["mixed_packs"] else "não"))
           for it in cfg.get("items") or []], {2: 36, 11: 18, 12: 22})
    sp = [_(FIELD_LABELS["sku_packs"][k]) for k in SKU_PACK_COLS]
    sheet(wb.create_sheet(_("Embalagens dos SKUs")), sp,
          [(sku, r["model"], r.get("inner") or None, r.get("qty"), r.get("wt"), "sim" if r.get("enabled", True) else "não",
            r.get("sort"), *(pal or (None, None, None)))
           for sku, rows in (cfg.get("sku_packs") or {}).items() for r in rows
           for pal in (r.get("pallets") or [None])], {1: 14, 2: 14, 3: 22})
    # o projeto guarda só o resumo dos pedidos: a aba vem vazia, no formato esperado
    sheet(wb.create_sheet(_("Pedidos")), [_("Pedido"), _("Data"), "SKU", N_("Quantidade"), _("Liberação")], [],
          {1: 14, 2: 12, 5: 16})
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
