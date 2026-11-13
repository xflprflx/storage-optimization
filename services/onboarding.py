"""Onboarding integrado: trilhas do guia passo a passo (/onboarding) e tours de cada tela (static/onboarding.js).

Tour: id -> {"p": regex do caminho da tela, "u": tela de exemplo (testes), "t": título, "s": etapas}. Cada etapa
destaca o 1º elemento visível entre os seletores (`#id`, `[name="x"]` ou `[data-tour="x"]`; sem seletor = cartão no
meio da tela) e mostra título e explicação. Etapas cujo elemento não está na tela são puladas.

Trilha: etapas com a tela a abrir (href, com {b} = filial de trabalho e {prj} = último projeto), o tour que roda ao
chegar lá e como saber se já foi feita ("done": verificação no banco; "tour": basta ter visto o tour).
tests/test_onboarding.py confere que todo seletor existe na tela de exemplo do tour.
"""
from __future__ import annotations

from typing import Optional

from services.i18n import N_, lookup


def S(sel, title: str, desc: str, cond: bool = False) -> dict:
    """Etapa do tour. cond=True: o elemento só aparece em certas situações (ex.: depois de um resultado)."""
    return {"s": [sel] if isinstance(sel, str) else list(sel or []), "t": title, "d": desc, "cond": cond}


SAVE = '[data-tour="form-save"]'

TOURS: dict[str, dict] = {
    "intro": {"p": r"^/$", "u": "/", "t": N_("Conhecendo o Neokioski"), "s": [
        S(None, N_("Bem-vindo ao Neokioski"),
          N_("Este tour mostra onde fica cada parte do sistema. Avance pelos botões ou pelas setas do teclado; Esc fecha.")),
        S('[data-tour="nav-cadastros"]', N_("Cadastros"),
          N_("Filiais, armazéns, endereços, tipos de bin, estruturas e produtos: a base de tudo. Comece por aqui.")),
        S('[data-tour="nav-imports"]', N_("Importações"),
          N_("Traga cadastros e saldos de planilhas (CSV ou Excel) em vez de digitar um por um.")),
        S('[data-tour="nav-map"]', N_("Mapa do estoque"),
          N_("Veja os corredores e a ocupação, desenhe a planta da filial e abra a vista 3D.")),
        S('[data-tour="nav-opt"]', N_("Otimização de bins"),
          N_("Propõe as melhores bins por endereço, o pool de bins e o projeto de filial nova; os resultados ficam guardados.")),
        S('[data-tour="nav-stock"]', N_("Saldos"), N_("Quanto de cada produto há em cada endereço.")),
        S('[data-tour="dash-cards"]', N_("Painel"), N_("Os números gerais do cadastro. Clique num cartão para abrir a lista.")),
        S("#nkLangMenu", N_("Idioma e unidades"),
          N_("Troque o idioma e o sistema de medidas (métrico ou imperial) quando quiser.")),
        S("#nkHelpToggle", N_("Ajuda nos campos"),
          N_("Ligada, mostra uma explicação com desenho ao passar o mouse sobre qualquer campo.")),
        S("#nkGuideMenu", N_("Guia"),
          N_("Peça o onboarding a qualquer momento: o tour da tela em que você está ou o guia passo a passo completo.")),
    ]},
    "branch": {"p": r"^/branches/(new|\d+/edit)$", "u": "/branches/new", "t": N_("Cadastro de filial"), "s": [
        S('[name="code"]', N_("Código da filial"), N_("Um código curto e único, usado nos endereços e nas importações (ex.: CD01).")),
        S('[name="name"]', N_("Nome"), N_("Como a filial aparece nas telas e nos relatórios.")),
        S('[name="warehouse_ids"]', N_("Armazéns"),
          N_("Opcional: os armazéns (depósitos) desta filial. Sem armazém, os endereços ficam direto na filial.")),
        S(SAVE, N_("Salvar"), N_("Grave a filial. Depois volte ao guia para a próxima etapa.")),
    ]},
    "warehouse": {"p": r"^/warehouses/(new|\d+/edit)$", "u": "/warehouses/new", "t": N_("Cadastro de armazém"), "s": [
        S('[name="code"]', N_("Código do armazém"), N_("Identifica o depósito dentro da filial (ex.: ARM1).")),
        S('[name="branch_ids"]', N_("Filiais"), N_("Marque as filiais que usam este armazém.")),
        S(SAVE, N_("Salvar"), N_("Grave o armazém. Ele passa a aparecer nos filtros de escopo.")),
    ]},
    "bin_type": {"p": r"^/bin-types/(new|\d+/edit)$", "u": "/bin-types/new", "t": N_("Cadastro de tipo de bin"), "s": [
        S('[name="code"]', N_("Código do tipo de bin"), N_("A caixa, gaveta ou palete onde os produtos ficam (ex.: CX-P).")),
        S('[name="height"]', N_("Medidas internas"),
          N_("Altura, largura e comprimento úteis: é com elas que o sistema calcula quanto cabe em cada bin.")),
        S("#gridPrev", N_("Divisões"), N_("Opcional: divida a bin em compartimentos; o desenho mostra como fica.")),
        S(SAVE, N_("Salvar"), N_("Grave o tipo. Cadastre todos os tipos que você usa ou poderia usar.")),
    ]},
    "structure": {"p": r"^/structures/(new|\d+/edit)$", "u": "/structures/new", "t": N_("Cadastro de estrutura"), "s": [
        S('[name="code"]', N_("Estrutura de armazenagem"),
          N_("O módulo físico (estante, porta-palete, flow rack) com frente, profundidade, altura e níveis.")),
        S('[name="levels"]', N_("Níveis"), N_("Quantos níveis o módulo tem e o vão útil de cada um.")),
        S("#gzEditor", N_("Zona de ouro"),
          N_("A faixa de altura mais confortável para pegar itens: os produtos que mais saem vão para lá.")),
        S(SAVE, N_("Salvar"), N_("Grave e depois associe a estrutura às estantes em Capacidade dos níveis.")),
    ]},
    "batch": {"p": r"^/locations/batch$", "u": "/locations/batch", "t": N_("Gerar endereços em lote"), "s": [
        S('[data-tour="batch-scope"]', N_("Onde criar"), N_("Escolha a filial, o armazém e, se quiser, a estrutura das estantes.")),
        S('[data-tour="batch-pattern"]', N_("Faixas do endereço"),
          N_("Informe corredores, estantes, níveis e posições (ex.: A a D, 01 a 20): o sistema monta todas as combinações.")),
        S('[data-tour="batch-exc"]', N_("Exceções"), N_("Opcional: retire ou ajuste partes que não seguem o padrão.")),
        S('[data-tour="batch-run"]', N_("Pré-visualizar e gerar"),
          N_("Confira a prévia antes; depois clique em Gerar agora para gravar os endereços.")),
    ]},
    "locations": {"p": r"^/locations$", "u": "/locations", "t": N_("Endereços"), "s": [
        S('[name="q"]', N_("Buscar"), N_("Filtre por endereço, filial, armazém, SKU ou situação.")),
        S("#binBulk", N_("Bins em massa"),
          N_("Selecione endereços na lista e defina o tipo e a quantidade de bins de todos de uma vez.")),
        S('[data-tour="loc-actions"]', N_("Criar ou importar"),
          N_("Cadastre um endereço, gere em lote ou importe de uma planilha.")),
    ]},
    "imports": {"p": r"^/imports$", "u": "/imports", "t": N_("Importações"), "s": [
        S('[data-tour="imp-cards"]', N_("O que importar"),
          N_("Cada cartão é um tipo de cadastro. Baixe o modelo da planilha, preencha e envie.")),
        S('[data-tour="imp-order"]', N_("Ordem recomendada"),
          N_("Filiais e armazéns primeiro, depois tipos de bin, endereços, produtos e por fim os saldos.")),
    ]},
    "import_upload": {"p": r"^/imports/(?!history)[a-z_]+$", "u": "/imports/products", "t": N_("Enviar planilha"), "s": [
        S('[name="file"]', N_("Arquivo"), N_("CSV ou Excel. A planilha pode ter qualquer nome de coluna e estar em qualquer idioma.")),
        S("#has_header", N_("Cabeçalho"), N_("Marque se a 1ª linha tem os nomes das colunas.")),
        S('[data-tour="imp-send"]', N_("Enviar e mapear"),
          N_("Na próxima tela você liga cada coluna da planilha ao campo certo e confere a prévia antes de gravar.")),
    ]},
    "history": {"p": r"^/imports/history$", "u": "/imports/history", "t": N_("Histórico de saídas"), "s": [
        S('[name="branch_id"]', N_("Filial"), N_("O histórico é de uma filial: os pedidos ou saídas dela.")),
        S('[name="file"]', N_("Planilha de pedidos"),
          N_("Pedido, SKU, quantidade e data (opcional), ou uma lista de SKUs com a saída. Serve para a posição pela saída e o simulador de trajeto.")),
        S('[data-tour="hist-send"]', N_("Enviar e conferir"), N_("Na próxima etapa você confere a aba e as colunas.")),
    ]},
    "levels": {"p": r"^/levels$", "u": "/levels?branch_id={b}", "t": N_("Capacidade dos níveis"), "s": [
        S("#branch_id", N_("Escopo"), N_("Escolha a filial (e o armazém) para ver os níveis das estantes.")),
        S('[name="structure_type_id"]', N_("Estrutura das estantes"),
          N_("Associe a estrutura cadastrada às estantes: vãos, zona de ouro e frente passam a valer nelas.")),
        S('[data-tour="lv-bulk"]', N_("Capacidade em massa"),
          N_("Defina, por condições, quantas bins de cada tipo cabem em cada nível.")),
    ]},
    "layout": {"p": r"^/layout$", "u": "/layout?branch_id={b}", "t": N_("Mapa do estoque"), "s": [
        S("#branch_id", N_("Filial"), N_("Escolha a filial e o armazém para ver o mapa.")),
        S('[name="mode"]', N_("O que colorir"), N_("Ocupação, bins ou preenchimento de cada endereço.")),
        S("#map", N_("Mapa"), N_("Cada corredor com as estantes e níveis. Passe o mouse para ver o endereço.")),
        S(["#planLink", "#plan3dLink"], N_("Planta e 3D"),
          N_("Desenhe a planta da filial para medir a caminhada pelos corredores e ver tudo em 3D.")),
    ]},
    "plan": {"p": r"^/layout/plan$", "u": "/layout/plan?branch_id={b}", "t": N_("Planta da filial"), "s": [
        S("#btnAuto", N_("Gerar automaticamente"),
          N_("O jeito mais rápido: o sistema monta a planta a partir dos endereços; depois você ajusta.")),
        S("#addRow", N_("Fileiras e obstáculos"), N_("Acrescente fileiras de estantes, pilares e áreas que bloqueiam a passagem.")),
        S("#refMode", N_("Ponto de referência"),
          N_("Marque a expedição: a caminhada de cada endereço é medida a partir dele.")),
        S("#svg", N_("Desenho"), N_("Arraste para mover, use a roda do mouse para o zoom e clique numa fileira para editar.")),
        S("#btnSave", N_("Salvar planta"), N_("Grave a planta. A otimização e o 3D passam a usá-la.")),
    ]},
    "view3d": {"p": r"^/layout/plan/3d$|^/optimization/site/\d+/3d$", "u": "/layout/plan/3d?branch_id={b}",
               "t": N_("Vista 3D"), "s": [
        S(None, N_("Vista 3D"), N_("Arraste para girar, botão direito para mover e a roda do mouse para o zoom.")),
        S("#nkColor", N_("Cor das bins"), N_("Escolha o que a cor mostra: tipo, ocupação, estrutura, zonas de ouro ou curva ABC.")),
        S("#nkFind", N_("Buscar"), N_("Digite um SKU ou endereço para destacá-lo e levar a câmera até lá.")),
        S(["#nkWalkBtn", "#nkPlay"], N_("Caminhada"),
          N_("Simule a separação de um pedido ou de um dia inteiro andando pelos corredores, com tempos e vídeo.")),
    ]},
    "optimization": {"p": r"^/optimization(/pool)?$", "u": "/optimization?branch_id={b}", "t": N_("Otimização de bins"), "s": [
        S('[data-tour="opt-modes"]', N_("Dois modos"),
          N_("Por endereço troca as bins de cada endereço; Pool mantém as bins e muda os produtos de lugar.")),
        S('[data-tour="opt-scope"]', N_("Escopo"), N_("Filial, armazém e, se quiser, corredores, SKUs ou materiais a otimizar.")),
        S('[data-tour="opt-what"]', N_("O que otimizar"), N_("O objetivo (menos bins, menos volume, menor tempo…) e os desempates.")),
        S('[data-tour="opt-slot"]', N_("Posição pela saída"),
          N_("Com o histórico de saídas, os produtos que mais saem ficam perto da expedição e na zona de ouro.")),
        S('[data-tour="opt-rules"]', N_("Restrições e análise"),
          N_("Tipos de bin permitidos e limites. Clique em Analisar: o resultado mostra a proposta e o que muda.")),
    ]},
    "site_list": {"p": r"^/optimization/site$", "u": "/optimization/site", "t": N_("Projeto de filial nova"), "s": [
        S('[data-tour="site-new"]', N_("Novo projeto"),
          N_("Um projeto parte do imóvel e da demanda e calcula zonas, estruturas e endereços do zero.")),
        S("#exemplos", N_("Exemplos prontos"),
          N_("Crie um projeto a partir de um exemplo para ver tudo funcionando sem preparar planilhas.")),
    ]},
    "site_project": {"p": r"^/optimization/site/\d+$", "u": "/optimization/site/{prj}", "t": N_("Projeto de filial nova"), "s": [
        S("#siteStepper", N_("Etapas do projeto"),
          N_("Imóvel, zonas, estruturas, demanda, bins e objetivo. Avance etapa por etapa; os exemplos já vêm preenchidos.")),
        S('[data-tour="site-run"]', N_("Salvar e otimizar"), N_("Roda a otimização do projeto (pode levar alguns segundos).")),
        S('[data-tour="site-branch"]', N_("Gerar a filial"),
          N_("Com o plano pronto, gere o cadastro da filial: endereços, estruturas, produtos e histórico."), cond=True),
    ]},
    "runs": {"p": r"^/optimization/runs$", "u": "/optimization/runs", "t": N_("Resultados salvos"), "s": [
        S('[data-tour="runs-list"]', N_("Histórico de resultados"),
          N_("Toda análise fica guardada com os parâmetros e indicadores. Abra uma para ver os detalhes.")),
        S("#cmpBtn", N_("Comparar"), N_("Marque dois ou mais resultados e compare os indicadores lado a lado.")),
    ]},
    "routes": {"p": r"^/optimization/slotting/routes$", "u": "/optimization/slotting/routes?branch_id={b}",
               "t": N_("Simulador de trajeto"), "s": [
        S('[data-tour="rt-kpis"]', N_("Indicadores"), N_("Caminhada e tempo médio por pedido na amostra do histórico.")),
        S("#t_v", N_("Tempos"), N_("Velocidade, parada e tempo por peça: ajuste para a sua operação e recalcule.")),
        S("#dias", N_("Por dia"), N_("Simule um dia inteiro de separação e veja o total de horas."), cond=True),
    ]},
    "products": {"p": r"^/products$", "u": "/products", "t": N_("Produtos"), "s": [
        S('[data-tour="prod-actions"]', N_("Cadastrar ou importar"),
          N_("Importe a lista de produtos com as medidas; o cálculo de quanto cabe em cada bin depende delas.")),
        S('[name="q"]', N_("Buscar"), N_("Encontre por SKU, descrição ou material.")),
        S("#bulkDims", N_("Medidas em massa"), N_("Copie as medidas de um produto para vários de uma vez.")),
    ]},
    "stock": {"p": r"^/stock$", "u": "/stock", "t": N_("Saldos"), "s": [
        S('[data-tour="stock-actions"]', N_("Lançar ou importar"),
          N_("O saldo diz onde cada produto está hoje: é a situação atual que a otimização compara com a proposta.")),
        S('[name="sku"]', N_("Filtros"), N_("Filtre por SKU, endereço, filial e armazém.")),
    ]},
    "settings": {"p": r"^/settings$", "u": "/settings", "t": N_("Configurações"), "s": [
        S('[name="addr_sort"]', N_("Endereços"), N_("Como os endereços são ordenados e se 07 e 7 são o mesmo código.")),
        S("#sim_video", N_("Simulação"), N_("Tempos padrão da separação e opções da caminhada 3D.")),
    ]},
}

# --------------------------------------------------------------------------- trilhas do guia
TRACKS: list[dict] = [
    {"id": "explore", "t": N_("Explorar com dados de exemplo"), "icon": "bi-rocket-takeoff",
     "d": N_("O caminho mais rápido para ver o sistema funcionando: cria uma filial completa a partir de um exemplo, sem preparar planilhas."),
     "steps": [
         {"id": "intro", "t": N_("Conheça as telas"), "d": N_("Um tour rápido pela barra de navegação e pelo painel."),
          "href": "/", "tour": "intro", "done": "tour"},
         {"id": "example", "t": N_("Crie um projeto a partir de um exemplo"),
          "d": N_("Em Projeto de filial nova, escolha um dos exemplos prontos e clique em Criar projeto."),
          "href": "/optimization/site", "tour": "site_list", "done": "projects"},
         {"id": "gen_branch", "t": N_("Otimize o projeto e gere a filial"),
          "d": N_("Abra o projeto, clique em Salvar e otimizar e depois em Gerar cadastro da filial: endereços, produtos e histórico são criados."),
          "href": "/optimization/site/{prj}", "tour": "site_project", "done": "locations"},
     ]},
    {"id": "base", "t": N_("Cadastrar a sua operação"), "icon": "bi-database",
     "d": N_("Monte o cadastro da sua filial real. Cada etapa pode ser digitada ou importada de planilha."),
     "steps": [
         {"id": "branch", "t": N_("Filial"), "d": N_("O local da operação. Tudo o mais pertence a uma filial."),
          "href": "/branches/new", "tour": "branch", "done": "branches"},
         {"id": "warehouse", "t": N_("Armazéns"), "d": N_("Os depósitos dentro da filial, se houver mais de um."),
          "href": "/warehouses/new", "tour": "warehouse", "done": "warehouses", "optional": True},
         {"id": "bin_types", "t": N_("Tipos de bin"), "d": N_("As caixas, gavetas ou paletes, com as medidas internas."),
          "href": "/bin-types/new", "tour": "bin_type", "done": "bin_types"},
         {"id": "structures", "t": N_("Estruturas de armazenagem"),
          "d": N_("Os módulos (estantes, porta-paletes) com níveis e zona de ouro. Deixam a análise mais precisa."),
          "href": "/structures/new", "tour": "structure", "done": "structures", "optional": True},
         {"id": "locations", "t": N_("Endereços"), "d": N_("Gere em lote a partir das faixas (corredor, estante, nível, posição) ou importe."),
          "href": "/locations/batch", "tour": "batch", "done": "locations"},
         {"id": "bins", "t": N_("Bins nos endereços"), "d": N_("Quais bins (tipo e quantidade) cada endereço tem hoje."),
          "href": "/locations", "tour": "locations", "done": "location_bins"},
         {"id": "levels", "t": N_("Capacidade dos níveis"),
          "d": N_("Associe as estruturas às estantes e informe quantas bins cabem em cada nível."),
          "href": "/levels?branch_id={b}", "tour": "levels", "done": "shelf_levels", "optional": True},
         {"id": "products", "t": N_("Produtos"), "d": N_("Importe os produtos com as medidas da unidade."),
          "href": "/imports/products", "tour": "import_upload", "done": "products"},
         {"id": "stock", "t": N_("Saldos"), "d": N_("Importe onde cada produto está e quanto há."),
          "href": "/imports/stock", "tour": "import_upload", "done": "stock"},
     ]},
    {"id": "optimize", "t": N_("Otimizar as bins"), "icon": "bi-sliders",
     "d": N_("Com o cadastro pronto, veja quantas bins e quanto espaço você economiza."),
     "steps": [
         {"id": "address", "t": N_("Otimização por endereço"),
          "d": N_("Propõe a melhor combinação de bins para os produtos de cada endereço."),
          "href": "/optimization?branch_id={b}", "tour": "optimization", "done": "run_address"},
         {"id": "pool", "t": N_("Pool de bins"), "d": N_("Mantém as bins e realoca os produtos entre endereços para liberar espaço."),
          "href": "/optimization/pool?branch_id={b}", "tour": "optimization", "done": "run_pool"},
         {"id": "runs", "t": N_("Resultados salvos"), "d": N_("Compare as análises e exporte o plano de implantação."),
          "href": "/optimization/runs", "tour": "runs", "done": "tour"},
     ]},
    {"id": "slotting", "t": N_("Posição pela saída e trajeto"), "icon": "bi-signpost-2",
     "d": N_("Use o histórico de pedidos para pôr os produtos que mais saem nos melhores lugares e medir a caminhada."),
     "steps": [
         {"id": "history", "t": N_("Histórico de saídas"), "d": N_("Envie a planilha de pedidos da filial."),
          "href": "/imports/history", "tour": "history", "done": "history"},
         {"id": "slot_pool", "t": N_("Pool com posição pela saída"),
          "d": N_("Ligue Posição pela saída e analise: a proposta compara hoje × amanhã em caminhada e tempo."),
          "href": "/optimization/pool?branch_id={b}&slot=1", "tour": "optimization", "done": "run_slot"},
         {"id": "routes", "t": N_("Simulador de trajeto"), "d": N_("Simule os pedidos e dias inteiros de separação."),
          "href": "/optimization/slotting/routes?branch_id={b}", "tour": "routes", "done": "tour"},
     ]},
    {"id": "plan", "t": N_("Planta e 3D"), "icon": "bi-bricks",
     "d": N_("Desenhe a planta para medir a caminhada pelos corredores e ver o estoque em 3D."),
     "steps": [
         {"id": "map", "t": N_("Mapa do estoque"), "d": N_("Ocupação de cada corredor, estante e nível."),
          "href": "/layout?branch_id={b}", "tour": "layout", "done": "tour"},
         {"id": "plan", "t": N_("Planta da filial"), "d": N_("Gere a planta automaticamente e ajuste fileiras, obstáculos e expedição."),
          "href": "/layout/plan?branch_id={b}", "tour": "plan", "done": "layouts"},
         {"id": "view3d", "t": N_("Vista 3D e caminhada"), "d": N_("Navegue pelo estoque e simule a separação em 3D."),
          "href": "/layout/plan/3d?branch_id={b}", "tour": "view3d", "done": "tour", "optional": True},
     ]},
]


def _checks(db) -> dict[str, bool]:
    """Situação do cadastro para marcar as etapas feitas."""
    from sqlalchemy import func, select

    from models import (BayStructure, BinType, Branch, BranchLayout, LocationBin, OptimizationRun, PickHistory, Product,
                        ShelfLevel, SiteProject, StockItem, StorageLocation, StructureType, Warehouse)

    def has(model, *where) -> bool:
        return db.scalar(select(func.count()).select_from(model).where(*where)) > 0

    runs = {k for (k,) in db.execute(select(OptimizationRun.kind).distinct())}
    slot = any(str((p or {}).get("slot") or "").lower() in {"1", "on", "true"}
               for (p,) in db.execute(select(OptimizationRun.params).where(OptimizationRun.kind == "pool")))
    return {"branches": has(Branch), "warehouses": has(Warehouse), "bin_types": has(BinType),
            "structures": has(StructureType) or has(BayStructure), "locations": has(StorageLocation),
            "location_bins": has(LocationBin), "shelf_levels": has(ShelfLevel), "products": has(Product),
            "stock": has(StockItem), "history": has(PickHistory), "layouts": has(BranchLayout), "projects": has(SiteProject),
            "run_address": "address" in runs, "run_pool": "pool" in runs, "run_slot": slot}


def work_ids(db) -> dict:
    """Filial de trabalho (a 1ª com endereços, senão a 1ª) e o último projeto de filial nova."""
    from sqlalchemy import func, select

    from models import Branch, SiteProject, StorageLocation

    b = db.scalar(select(StorageLocation.branch_id).group_by(StorageLocation.branch_id)
                  .order_by(func.count().desc()).limit(1)) or db.scalar(select(Branch.id).order_by(Branch.id).limit(1))
    prj = db.scalar(select(SiteProject.id).order_by(SiteProject.id.desc()).limit(1))
    return {"b": b, "prj": prj}


def _href(href: str, ids: dict) -> str:
    if "{prj}" in href and not ids.get("prj"):
        return "/optimization/site"
    if "{b}" in href and not ids.get("b"):
        href = href.replace("branch_id={b}&", "").replace("?branch_id={b}", "").replace("&branch_id={b}", "")
    return href.replace("{b}", str(ids.get("b") or "")).replace("{prj}", str(ids.get("prj") or ""))


def progress(db) -> dict:
    """Trilhas com o estado de cada etapa ("done": True/False; None = só o navegador sabe, pelo tour visto)."""
    checks, ids = _checks(db), work_ids(db)
    tracks, nxt = [], None
    total = done_n = 0
    for tr in TRACKS:
        steps = []
        for st in tr["steps"]:
            done = None if st["done"] == "tour" else checks.get(st["done"], False)
            href = _href(st["href"], ids)
            steps.append({**st, "href": href, "go": f"{href}#nk-tour={st['tour']}", "state": done})
            if not st.get("optional") and done is not None:
                total += 1
                done_n += bool(done)
        tracks.append({**tr, "steps": steps,
                       "complete": all(s["state"] is not False for s in steps if not s.get("optional"))})
    for tr in tracks[1:] + tracks[:1]:  # próxima etapa: o cadastro real vem antes do caminho de exemplo
        for s in tr["steps"]:
            if s["state"] is False and not s.get("optional"):
                nxt = nxt or (tr, s)
    return {"tracks": tracks, "next": nxt, "total": total, "done": done_n,
            "pct": round(done_n * 100 / total) if total else 0, "empty": not checks["branches"]}


def tours_payload(lang: Optional[str] = None) -> list[dict]:
    """Tours traduzidos para o navegador."""
    from services.i18n import units_in_text

    tr = lambda s: units_in_text(lookup(s, lang))  # noqa: E731
    return [{"id": k, "p": v["p"], "t": tr(v["t"]),
             "s": [{"s": st["s"], "t": tr(st["t"]), "d": tr(st["d"])} for st in v["s"]]} for k, v in TOURS.items()]
