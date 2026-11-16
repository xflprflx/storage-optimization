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


def S(sel, title: str, desc: str, concept: Optional[str] = None, cond: bool = False) -> dict:
    """Etapa do tour: o que fazer (desc, com exemplo) e, opcional, o conceito por trás (concept, em linguagem
    simples). cond=True: o elemento só aparece em certas situações (ex.: depois de um resultado)."""
    return {"s": [sel] if isinstance(sel, str) else list(sel or []), "t": title, "d": desc, "c": concept, "cond": cond}


SAVE = '[data-tour="form-save"]'

TOURS: dict[str, dict] = {
    "intro": {"p": r"^/$", "u": "/", "t": N_("Conhecendo o Neokioski"), "s": [
        S(None, N_("Bem-vindo ao Neokioski"),
          N_("O sistema responde a três perguntas do estoque: quais bins usar em cada endereço, onde guardar cada produto e quanto o separador anda para montar os pedidos. Avance pelos botões ou pelas setas do teclado; Esc fecha."),
          N_("Otimizar é escolher, entre milhares de combinações possíveis, a que dá o melhor resultado numa meta (ex.: menos bins) sem quebrar as regras (ex.: o saldo tem de caber). O sistema faz essa busca; você só descreve o estoque e escolhe a meta.")),
        S('[data-tour="nav-cadastros"]', N_("Cadastros"),
          N_("Aqui fica a descrição do estoque: filiais, endereços, tipos de bin (com medidas internas), estruturas e produtos (com medidas da unidade)."),
          N_("A otimização só é tão boa quanto o cadastro: sem as medidas da unidade e da bin o sistema não consegue calcular quanto cabe, e esses itens aparecem como \"Faltam dimensões\".")),
        S('[data-tour="nav-imports"]', N_("Importações"),
          N_("Para volumes grandes, importe planilhas (CSV ou Excel) em vez de digitar. Siga a ordem: filiais → armazéns → tipos de bin → endereços → capacidade dos níveis → produtos → saldos.")),
        S('[data-tour="nav-map"]', N_("Mapa do estoque"),
          N_("Mostra a ocupação de cada corredor, estante e nível. Dali você desenha a planta da filial, que permite medir a caminhada real pelos corredores, e abre a vista 3D.")),
        S('[data-tour="nav-opt"]', N_("Otimização de bins"),
          N_("Quatro ferramentas: Por endereço (troca as bins de cada endereço), Pool (mantém as bins e muda os produtos de lugar), Projeto de filial nova (dimensiona um armazém do zero) e Resultados salvos.")),
        S('[data-tour="nav-stock"]', N_("Saldos"),
          N_("Quanto de cada produto há em cada endereço hoje. É o ponto de partida que a otimização compara com a proposta (antes × depois).")),
        S('[data-tour="dash-cards"]', N_("Painel"), N_("Os números gerais do cadastro. Clique num cartão para abrir a lista; \"Pendências de cadastro\" mostra o que ainda falta.")),
        S("#nkLangMenu", N_("Idioma e unidades"),
          N_("Troque o idioma e o sistema de medidas (métrico ou imperial). Os valores são guardados sempre em métrico; só a exibição muda.")),
        S("#nkHelpToggle", N_("Ajuda nos campos"),
          N_("Ligada, cada campo mostra ao passar o mouse uma explicação com desenho. Use junto com os tours quando tiver dúvida sobre um valor.")),
        S("#nkGuideMenu", N_("Guia"),
          N_("Peça o onboarding a qualquer momento: o tour da tela em que você está, o guia passo a passo ou o glossário de conceitos.")),
    ]},
    "branch": {"p": r"^/branches/(new|\d+/edit)$", "u": "/branches/new", "t": N_("Cadastro de filial"), "s": [
        S('[name="code"]', N_("Código da filial"), N_("Um código curto e único, ex.: CD01. É por ele que as planilhas de importação ligam endereços e saldos à filial, então use o mesmo código do seu ERP/WMS.")),
        S('[name="name"]', N_("Nome"), N_("Como a filial aparece nas telas e nos relatórios, ex.: Centro de distribuição Campinas.")),
        S('[name="warehouse_ids"]', N_("Armazéns"),
          N_("Marque só se a filial tem mais de um depósito físico separado (ex.: seco e refrigerado). Sem armazém, os endereços ficam direto na filial."),
          N_("O par filial + armazém é o \"escopo\": a otimização nunca troca bins nem produtos entre escopos diferentes.")),
        S(SAVE, N_("Salvar"), N_("Grave a filial. Depois volte ao guia para a próxima etapa.")),
    ]},
    "warehouse": {"p": r"^/warehouses/(new|\d+/edit)$", "u": "/warehouses/new", "t": N_("Cadastro de armazém"), "s": [
        S('[name="code"]', N_("Código do armazém"), N_("Identifica o depósito dentro da filial, ex.: SECO, FRIO ou ARM1.")),
        S('[name="branch_ids"]', N_("Filiais"), N_("Marque as filiais que usam este armazém. Ele passa a aparecer nos filtros de escopo dessas filiais.")),
        S(SAVE, N_("Salvar"), N_("Grave o armazém.")),
    ]},
    "bin_type": {"p": r"^/bin-types/(new|\d+/edit)$", "u": "/bin-types/new", "t": N_("Cadastro de tipo de bin"), "s": [
        S('[name="code"]', N_("Código do tipo de bin"), N_("A caixa, gaveta ou palete onde os produtos ficam, ex.: CX-P (caixa pequena), CX-G, PAL. Cadastre também os tipos que você poderia comprar: o sistema só propõe tipos cadastrados.")),
        S('[name="height"]', N_("Medidas internas"),
          N_("Meça por dentro: altura, largura (a frente, virada para o corredor) e comprimento (a profundidade). Ex.: 15 × 20 × 40 cm."),
          N_("Quantas unidades cabem numa bin é o MENOR de três limites: o encaixe das unidades em grade (com rotação), o volume da bin × enchimento máximo ÷ volume da unidade e o peso máximo ÷ peso da unidade. Medidas externas superestimam a capacidade.")),
        S('[name="max_weight"]', N_("Peso máximo"), N_("Opcional, em kg. Com \"Respeitar peso máximo da bin\" ligado na otimização, produtos pesados param de encher a bin antes de faltar espaço.")),
        S("#gridPrev", N_("Divisões"), N_("Opcional, para frações: escolha a bin pai (a bin inteira) e em quantas partes ela é dividida na largura e no comprimento, ex.: 2 × 3. As medidas da fração saem da divisão; o desenho mostra como fica.")),
        S(SAVE, N_("Salvar"), N_("Grave o tipo.")),
    ]},
    "structure": {"p": r"^/structures/(new|\d+/edit)$", "u": "/structures/new", "t": N_("Cadastro de estrutura"), "s": [
        S('[name="code"]', N_("Estrutura de armazenagem"),
          N_("O módulo físico que se repete no armazém, ex.: estante de 100 cm de frente, 60 cm de profundidade, 200 cm de altura e 5 níveis."),
          N_("Sem estrutura, o sistema estima tudo pelo endereço. Com ela, sabe a altura real de cada nível (para a zona de ouro), a frente real de cada estante (para a caminhada) e o espaço disponível para as bins.")),
        S('[name="levels"]', N_("Níveis e vãos"), N_("Quantos níveis o módulo tem e o vão útil de cada um (altura livre entre uma prateleira e a de cima). Se os vãos forem diferentes, informe um por nível, de baixo para cima.")),
        S("#gzEditor", N_("Zona de ouro"),
          N_("A faixa de altura confortável para pegar itens, contada do piso. Um bom começo é 75 a 150 cm (botão Sugerir)."),
          N_("Pegar acima do ombro ou abaixo do joelho é mais lento e cansativo. Os níveis cujo meio cai nessa faixa são \"de ouro\": a otimização põe ali os produtos que mais saem.")),
        S(SAVE, N_("Salvar"), N_("Grave e depois associe a estrutura às estantes em Capacidade dos níveis (ou já ao gerar os endereços em lote).")),
    ]},
    "batch": {"p": r"^/locations/batch$", "u": "/locations/batch", "t": N_("Gerar endereços em lote"), "s": [
        S('[data-tour="batch-scope"]', N_("Onde criar"), N_("Escolha a filial, o armazém e, se já cadastrou, a estrutura das estantes: ela é associada às estantes geradas e preenche o vão e a capacidade dos níveis."),
          N_("Endereço = Corredor-Estante-Nível-Posição, ex.: A-01-2-1 é o corredor A, estante 01, nível 2 (de baixo para cima), posição 1. Cada endereço recebe bins; cada bin guarda um SKU.")),
        S('[data-tour="batch-pattern"]', N_("Regra padrão por nível"),
          N_("Para cada parte do endereço escolha faixa de números, faixa de letras, valor fixo ou não utilizar. Ex.: corredor A a D, estante 1 a 20 com 2 dígitos (01…20), nível 1 a 5, posição 1 a 4. Só o corredor é obrigatório; \"Não utilizar\" encerra o endereço naquela parte."),
          N_("Estantes ímpares e pares costumam ficar em lados opostos do corredor; a numeração importa para estimar a caminhada quando não há planta desenhada.")),
        S('[data-tour="batch-exc"]', N_("Exceções"), N_("Para o que foge do padrão, ex.: no corredor D, estantes 19 e 20, o nível vai só de 1 a 3. A exceção vale só para os códigos indicados.")),
        S('[data-tour="batch-run"]', N_("Pré-visualizar e gerar"),
          N_("Pré-visualize primeiro: a lista mostra o que será criado e os conflitos com endereços existentes. Endereços ocupados nunca são sobrescritos. Depois, Gerar agora.")),
    ]},
    "locations": {"p": r"^/locations$", "u": "/locations", "t": N_("Endereços"), "s": [
        S('[name="q"]', N_("Buscar"), N_("Filtre por endereço (ex.: A-01 traz a estante 01 do corredor A), filial, armazém, SKU ou situação.")),
        S("#binBulk", N_("Bins em massa"),
          N_("Marque endereços na lista e informe o tipo e a quantidade de bins que eles têm hoje, ex.: 4 × CX-P. Faça isso para todo o armazém antes de otimizar."),
          N_("As bins de hoje são o \"antes\". Sem elas o sistema não sabe o que trocar e todo endereço aparece como \"Bins atuais não comportam o saldo\".")),
        S('[data-tour="loc-actions"]', N_("Criar ou importar"), N_("Gere em lote para criar muitos de uma vez, ou importe uma planilha com endereço, tipo de bin e quantidade.")),
    ]},
    "imports": {"p": r"^/imports$", "u": "/imports", "t": N_("Importações"), "s": [
        S('[data-tour="imp-cards"]', N_("O que importar"),
          N_("Cada cartão é um tipo de cadastro. Clique para baixar o modelo (CSV), preencha e envie. A planilha pode ter qualquer nome de coluna: você liga cada coluna ao campo certo na tela seguinte.")),
        S('[data-tour="imp-order"]', N_("Ordem recomendada"),
          N_("Filiais → armazéns → tipos de bin → endereços → capacidade dos níveis → produtos → saldos. Cada um depende dos anteriores: um saldo só entra se o produto e o endereço já existirem."),
          N_("As medidas são sempre métricas nas planilhas (cm, kg, L), em qualquer idioma.")),
    ]},
    "import_upload": {"p": r"^/imports/(?!history)[a-z_]+$", "u": "/imports/products", "t": N_("Enviar planilha"), "s": [
        S('[name="file"]', N_("Arquivo"), N_("CSV ou Excel exportado do seu ERP/WMS. Para produtos, as colunas essenciais são SKU, descrição e as medidas da unidade (altura, largura, comprimento em cm; peso em kg).")),
        S("#has_header", N_("Cabeçalho"), N_("Marque se a 1ª linha tem os nomes das colunas.")),
        S('[data-tour="imp-send"]', N_("Enviar e mapear"),
          N_("Na próxima tela você liga cada coluna ao campo certo e vê uma prévia com os erros linha a linha antes de gravar. Nada é gravado antes de você confirmar.")),
    ]},
    "history": {"p": r"^/imports/history$", "u": "/imports/history", "t": N_("Histórico de saídas"), "s": [
        S('[name="branch_id"]', N_("Filial"), N_("O histórico é de uma filial: os pedidos (ou saídas) dela, de preferência dos últimos 3 a 6 meses.")),
        S('[name="file"]', N_("Planilha de pedidos"),
          N_("Uma linha por item de pedido: nº do pedido, SKU, quantidade e, se tiver, a data e o horário de liberação para separação (para simular o dia com o horário). Também serve uma lista de SKUs com o total de saída, mas sem pedidos não há afinidade nem simulação de trajeto."),
          N_("Com o histórico o sistema calcula a popularidade de cada SKU (curva ABC: A = os que somam 80% das saídas, B até 95%, C o resto) e quais saem juntos no mesmo pedido (afinidade).")),
        S("#abs", N_("Quantidades negativas"), N_("Deixe marcado se o relatório lança a saída com sinal negativo (ex.: -3): o sistema usa o valor absoluto.")),
        S('[data-tour="hist-send"]', N_("Enviar e conferir"), N_("Na próxima etapa você confere a aba e as colunas. Só o resumo (popularidade, pares e uma amostra de pedidos) fica salvo.")),
    ]},
    "levels": {"p": r"^/levels$", "u": "/levels?branch_id={b}", "t": N_("Capacidade dos níveis"), "s": [
        S("#branch_id", N_("Escopo"), N_("Escolha a filial (e o armazém) para listar os níveis de cada estante.")),
        S('[name="structure_type_id"]', N_("Estrutura das estantes"),
          N_("Associe a estrutura às estantes por condição, ex.: corredores A a C, estantes 01 a 20. Os vãos, a zona de ouro e a frente da estrutura passam a valer nelas.")),
        S('[data-tour="lv-bulk"]', N_("Capacidade em massa"),
          N_("Informe quantas bins de cada tipo cabem num nível, ex.: 6 × CX-P ou 3 × CX-G, e o vão em cm. Use condições para aplicar a muitos níveis de uma vez."),
          N_("A capacidade do nível é uma restrição: a otimização por endereço nunca propõe mais bins do que o nível comporta. Sem capacidade cadastrada, ela é estimada pela largura do nível.")),
    ]},
    "layout": {"p": r"^/layout$", "u": "/layout?branch_id={b}", "t": N_("Mapa do estoque"), "s": [
        S("#branch_id", N_("Filial"), N_("Escolha a filial e o armazém para ver o mapa.")),
        S('[name="mode"]', N_("O que colorir"), N_("Ocupação (tem saldo ou não), % enchimento (volume do saldo ÷ volume das bins) ou tipo de bin. Enchimento baixo indica bins grandes demais para o saldo.")),
        S("#map", N_("Mapa"), N_("Cada corredor com as estantes e os níveis. Passe o mouse para ver o endereço, as bins e o saldo.")),
        S(["#planLink", "#plan3dLink"], N_("Planta e 3D"),
          N_("Desenhe a planta para que a caminhada seja medida pelos corredores de verdade, e não estimada pelo endereço.")),
    ]},
    "plan": {"p": r"^/layout/plan$", "u": "/layout/plan?branch_id={b}", "t": N_("Planta da filial"), "s": [
        S("#btnAuto", N_("Gerar automaticamente"),
          N_("Monta a planta a partir dos endereços: uma fileira por lado de corredor, com a largura de corredor informada. É o jeito mais rápido; depois ajuste o que for diferente.")),
        S("#addRow", N_("Fileiras e obstáculos"), N_("Uma fileira é uma linha de estantes de um lado do corredor. Obstáculos (pilares, docas, escritórios) bloqueiam a passagem e desviam a caminhada.")),
        S("#refMode", N_("Ponto de referência"),
          N_("Clique onde o separador começa e termina o pedido, normalmente a expedição."),
          N_("Toda distância é medida a partir desse ponto pelo caminho mais curto entre os corredores. A \"zona de ouro da caminhada\" são os endereços mais perto dele.")),
        S("#svg", N_("Desenho"), N_("Arraste para mover, roda do mouse para o zoom; clique numa fileira para girar, trocar o lado do corredor ou inverter a numeração.")),
        S("#btnSave", N_("Salvar planta"), N_("Grave. A posição pela saída, o simulador de trajeto e o 3D passam a usar esta planta.")),
    ]},
    "view3d": {"p": r"^/layout/plan/3d$|^/optimization/site/\d+/3d$", "u": "/layout/plan/3d?branch_id={b}",
               "t": N_("Vista 3D"), "s": [
        S(None, N_("Vista 3D"), N_("Arraste para girar, botão direito para mover e a roda do mouse para o zoom.")),
        S("#nkColor", N_("Cor das bins"), N_("Pinte por zona de ouro ou curva ABC para ver se os produtos A estão nos níveis de ouro e perto da expedição. Se estiverem espalhados, a posição pela saída tem ganho a oferecer.")),
        S("#nkFind", N_("Buscar"), N_("Digite um SKU ou endereço para destacá-lo e levar a câmera até lá.")),
        S(["#nkWalkBtn", "#nkPlay"], N_("Caminhada"),
          N_("Simula o separador montando um pedido ou um dia inteiro, com a rota e o tempo de cada parada. Dá para gravar em vídeo."),
          N_("Tempo do pedido = caminhada ÷ velocidade + paradas × tempo por parada + peças × tempo por peça + itens fora da zona de ouro × tempo extra. Os valores padrão ficam em Configurações.")),
    ]},
    "optimization": {"p": r"^/optimization(/pool)?$", "u": "/optimization?branch_id={b}", "t": N_("Otimização de bins"), "s": [
        S('[data-tour="opt-modes"]', N_("Dois modos"),
          N_("Por endereço: cada endereço guarda os mesmos produtos e o sistema escolhe as melhores bins para eles. Pool: as bins ficam onde estão e os produtos mudam de endereço."),
          N_("Comece pelo Por endereço: é o de menor impacto na operação (só troca bins). O Pool libera mais espaço, mas mexe no endereço dos produtos.")),
        S('[data-tour="opt-scope"]', N_("Escopo"), N_("Escolha a filial (obrigatória) e, para um teste rápido, restrinja a um corredor (Endereço contém: A-) ou a alguns SKUs. Escopos menores rodam mais rápido.")),
        S('[data-tour="opt-bins"]', N_("Bins existentes ou infinitas"),
          N_("Existentes: o plano só usa as bins que você já tem (não precisa comprar). Recurso infinito: o sistema diz quantas de cada tipo seriam necessárias, útil para planejar compras."),
          N_("Isso é uma restrição de recurso. Com bins existentes a solução pode ser um pouco pior, porque o sistema fica limitado ao que há no armazém.")),
        S('[data-tour="opt-basis"]', N_("Quantidade por SKU"),
          N_("Saldo atual dimensiona para o estoque de hoje. Min/Max level dimensiona para o estoque mínimo ou máximo planejado do produto, evitando que a bin fique pequena quando o estoque subir.")),
        S('[data-tour="opt-objective"]', N_("Objetivo"),
          N_("A meta a otimizar. Menos volume de bins (menos ar) é um bom começo: tira bins grandes demais. Menos bins reduz a quantidade; Mínimo de mudanças só corrige o que não comporta o saldo."),
          N_("Objetivo é o número que o sistema tenta deixar o menor (ou maior) possível. Restrições são regras que nunca podem ser quebradas, como o saldo caber na bin e a bin caber no nível.")),
        S('[data-tour="opt-prio"]', N_("Desempate"),
          N_("Se várias soluções empatam no objetivo, o 2º critério decide, depois o 3º e o 4º. Ex.: objetivo Menos bins e desempate por Mínimo de mudanças: entre as soluções com menos bins, a que mexe em menos coisa."),
          N_("Isso se chama otimização lexicográfica: o 2º critério nunca piora o 1º, só escolhe entre empates. Por isso a ordem importa.")),
        S('[data-tour="opt-engine"]', N_("Motor e tempo limite"),
          N_("Pesquisa operacional (padrão) busca a melhor solução para o escopo inteiro; 15 s costumam bastar. Heurística é instantânea e serve para escopos muito grandes ou para conferir."),
          N_("A heurística decide um item por vez com uma regra simples (rápida, mas pode deixar ganho na mesa). A pesquisa operacional (OR-Tools CP-SAT) monta um modelo matemático e prova quando achou o ótimo. Ela parte da solução da heurística, então nunca fica pior.")),
        S('[data-tour="opt-slot"]', N_("Posição pela saída"),
          N_("Ligue para considerar a popularidade dos produtos (precisa do histórico de saídas). Informe o corredor mais perto da expedição; com planta desenhada, a caminhada vem dela."),
          N_("Slotting: os produtos que mais saem (curva A) vão para perto da expedição (zona de ouro da caminhada) e na altura confortável (zona de ouro da pega). Menos caminhada e menos esforço por pedido.")),
        S('[data-tour="opt-fill"]', N_("Enchimento e margem"),
          N_("Enchimento máximo (padrão 85%) deixa folga para pegar e repor sem forçar. Margem (ex.: 20%) dimensiona para 20% a mais que a quantidade, prevendo crescimento."),
          N_("Com 100% de enchimento a bin \"cabe no papel\" mas não na prática: as unidades não se encaixam perfeitamente e o separador precisa de espaço para a mão.")),
        S('[data-tour="opt-units"]', N_("Unidades na bin"),
          N_("Encaixe geométrico confere se as unidades cabem fisicamente em grade, não só pelo volume. A rotação diz se o produto pode deitar (qualquer orientação) ou só girar com a altura para cima.")),
        S('[data-tour="opt-run"]', N_("Analisar"), N_("Roda a otimização. Nada muda no cadastro: o resultado é uma proposta que você confere antes de aplicar.")),
        S('[data-tour="opt-solver"]', N_("Qualidade da solução"),
          N_("\"Ótimo comprovado\": não existe solução melhor. \"Melhor no tempo\": a melhor encontrada no tempo limite (aumente o tempo se quiser). \"Heurística\": o solver não melhorou a regra simples a tempo."),
          N_("A tabela compara, critério a critério, a heurística com a pesquisa operacional. Valores em verde são o ganho do modelo matemático."), cond=True),
        S('[data-tour="opt-kpis"]', N_("Indicadores"),
          N_("Antes → depois de cada indicador: bins, volume de bins, largura de frente e ocupação. Leia primeiro estes cartões para saber o tamanho do ganho."), cond=True),
        S('[data-tour="opt-table"]', N_("Resultado por endereço"),
          N_("Cada linha diz o que fazer: \"Pode melhorar\" (troca sugerida), \"Bins atuais não comportam o saldo\" (corrigir), \"Sem saldo: liberar bins\", \"Já está ótimo\", \"Inviável com as restrições\" (afrouxe alguma regra) ou \"Faltam dimensões\" (complete o cadastro)."),
          cond=True),
        S("#applyForm", N_("Aplicar"),
          N_("Exporte o plano de implantação (Excel/PDF) para a equipe executar. Depois de executado, aplique os selecionados ou todos para atualizar o cadastro de bins."), cond=True),
    ]},
    "site_list": {"p": r"^/optimization/site$", "u": "/optimization/site", "t": N_("Projeto de filial nova"), "s": [
        S('[data-tour="site-new"]', N_("Novo projeto"),
          N_("Para um armazém que ainda não existe (ou uma reforma): você informa o imóvel e a demanda, e o sistema calcula quantas estruturas de cada tipo, onde ficam e quais endereços e bins criar.")),
        S("#exemplos", N_("Exemplos prontos"),
          N_("Crie um projeto a partir de um exemplo para ver o fluxo completo sem preparar planilhas; depois gere a filial e use as outras telas com ela.")),
    ]},
    "site_project": {"p": r"^/optimization/site/\d+$", "u": "/optimization/site/{prj}", "t": N_("Projeto de filial nova"), "s": [
        S('[data-tour="site-building"]', N_("Imóvel"), N_("Comprimento × largura × pé-direito útil (altura livre até vigas e sprinklers), largura dos corredores e afastamento das paredes. Ex.: 40 × 25 m, 8 m, corredores de 1,2 m.")),
        S('[data-tour="site-zones"]', N_("Zonas e circulação"), N_("Áreas que não recebem estrutura: docas, expedição, escritório, pilares. A expedição também é o ponto de referência da caminhada.")),
        S('[data-tour="site-structs"]', N_("Estruturas"),
          N_("As estruturas candidatas. Para cada uma: deixar o sistema escolher a quantidade (com mínimo e máximo opcionais), fixar uma quantidade ou não usar.")),
        S('[data-tour="site-demand"]', N_("Demanda"),
          N_("O que precisa caber: SKUs com medidas e a quantidade a guardar (de uma planilha ou do cadastro de uma filial existente, com um fator de crescimento).")),
        S('[data-tour="site-bins"]', N_("Bins e regras"), N_("Os tipos de bin permitidos e as mesmas regras da otimização (enchimento máximo, rotação, peso).")),
        S('[data-tour="site-slot"]', N_("Posição dos SKUs"), N_("Com histórico de pedidos, os SKUs mais populares ficam perto da expedição e nos níveis de ouro.")),
        S('[data-tour="site-objective"]', N_("Objetivo"),
          N_("Menos módulos de estrutura, menor custo total ou menos metros de fileira."),
          N_("O projeto é resolvido em ordem: 1º guardar toda a demanda (falta é muito penalizada), 2º o objetivo escolhido, 3º o desempate. Se faltar espaço, o resultado diz quanto não coube.")),
        S('[data-tour="site-run"]', N_("Salvar e otimizar"), N_("Roda o modelo (alguns segundos a poucos minutos). O resultado traz planta, quantidades, custo e o plano em PDF/Excel.")),
        S('[data-tour="site-branch"]', N_("Gerar a filial"),
          N_("Cria o cadastro da filial a partir do plano: endereços, estruturas, bins, produtos e, se houver, o histórico de pedidos."), cond=True),
    ]},
    "runs": {"p": r"^/optimization/runs$", "u": "/optimization/runs", "t": N_("Resultados salvos"), "s": [
        S('[data-tour="runs-list"]', N_("Histórico de resultados"),
          N_("Toda análise fica guardada com os parâmetros e os indicadores. Use para testar cenários: rode com objetivos ou restrições diferentes e compare.")),
        S("#cmpBtn", N_("Comparar"), N_("Marque dois ou mais resultados e compare os indicadores lado a lado, ex.: enchimento 85% × 90%, ou Menos bins × Menos volume.")),
    ]},
    "routes": {"p": r"^/optimization/slotting/routes$", "u": "/optimization/slotting/routes?branch_id={b}",
               "t": N_("Simulador de trajeto"), "s": [
        S('[data-tour="rt-kpis"]', N_("Indicadores"), N_("Caminhada e tempo médio por pedido numa amostra do histórico, com os produtos onde estão hoje.")),
        S("#t_v", N_("Tempos"),
          N_("Ajuste para a sua operação. Padrões: 1 m/s de caminhada, 10 s por parada, 2 s por peça e 5 s a mais por item fora da zona de ouro."),
          N_("Meça alguns pedidos reais com um cronômetro e calibre estes valores: com eles certos, o tempo simulado vira uma previsão confiável de horas de separação.")),
        S("#dias", N_("Por dia"), N_("Simule um dia inteiro de pedidos e veja o total de horas, para dimensionar a equipe."), cond=True),
    ]},
    "products": {"p": r"^/products$", "u": "/products", "t": N_("Produtos"), "s": [
        S('[data-tour="prod-actions"]', N_("Cadastrar ou importar"),
          N_("Importe os produtos com as medidas da UNIDADE que fica na bin (a peça ou a caixa de venda, não o fardo)."),
          N_("É com essas medidas que o sistema calcula quantas unidades cabem em cada tipo de bin. Produto sem medida aparece como \"Faltam dimensões\" e não é otimizado.")),
        S('[name="q"]', N_("Buscar"), N_("Encontre por SKU, descrição ou material.")),
        S("#bulkDims", N_("Medidas em massa"), N_("Copie as medidas de um produto para vários de uma vez, útil para variações de cor ou tamanho com a mesma embalagem.")),
    ]},
    "stock": {"p": r"^/stock$", "u": "/stock", "t": N_("Saldos"), "s": [
        S('[data-tour="stock-actions"]', N_("Lançar ou importar"),
          N_("Importe o saldo por endereço do seu ERP/WMS: endereço, SKU e quantidade."),
          N_("O saldo é o \"antes\": a otimização compara as bins de hoje com a proposta para essas quantidades.")),
        S('[name="sku"]', N_("Filtros"), N_("Filtre por SKU, endereço, filial e armazém.")),
    ]},
    "settings": {"p": r"^/settings$", "u": "/settings", "t": N_("Configurações"), "s": [
        S('[name="addr_sort"]', N_("Endereços"), N_("Ordem natural (2 antes de 10) ou alfabética, e se 07 e 7 são o mesmo código. Afeta a ordem das listas de separação.")),
        S("#sim_video", N_("Simulação"), N_("Os tempos padrão da separação (velocidade, parada, peça, fora do ouro) e as opções da caminhada 3D. Valem para todas as telas de simulação.")),
    ]},
}

# --------------------------------------------------------------------------- conceitos (glossário do guia)
CONCEPTS: list[dict] = [
    {"id": "address", "t": N_("Endereço"), "tour": "batch", "href": "/locations/batch",
     "d": N_("Uma posição do armazém identificada por Corredor-Estante-Nível-Posição, ex.: A-01-2-1. O nível conta de baixo para cima. Cada endereço tem bins, e cada bin guarda um SKU.")},
    {"id": "bin", "at": "height", "t": N_("Bin e capacidade"), "tour": "bin_type", "href": "/bin-types/new",
     "d": N_("A caixa, gaveta ou palete dentro do endereço. Quantas unidades cabem nela é o menor de três limites: encaixe em grade, volume × enchimento máximo e peso máximo.")},
    {"id": "fill", "at": "opt-fill", "t": N_("Enchimento máximo"), "tour": "optimization", "href": "/optimization?branch_id={b}",
     "d": N_("Quanto do volume da bin pode ser ocupado, ex.: 85%. A folga existe porque as unidades não se encaixam perfeitamente e é preciso espaço para a mão.")},
    {"id": "objective", "at": "opt-objective", "t": N_("Objetivo e restrições"), "tour": "optimization", "href": "/optimization?branch_id={b}",
     "d": N_("Objetivo é o que se quer minimizar ou maximizar (ex.: volume de bins). Restrições são regras que nunca podem ser quebradas (o saldo cabe, a bin cabe no nível). Otimizar é achar a melhor solução que respeita todas as regras.")},
    {"id": "lexi", "at": "opt-prio", "t": N_("Desempate (prioridade)"), "tour": "optimization", "href": "/optimization?branch_id={b}",
     "d": N_("Quando várias soluções empatam no objetivo, o 2º critério escolhe entre elas, depois o 3º. O critério de baixo nunca piora o de cima.")},
    {"id": "engine", "at": "opt-engine", "t": N_("Pesquisa operacional × heurística"), "tour": "optimization", "href": "/optimization?branch_id={b}",
     "d": N_("Heurística: regra rápida que decide um item por vez; boa, mas sem garantia. Pesquisa operacional: modelo matemático do escopo inteiro resolvido por um solver (OR-Tools); diz se a solução é ótima comprovada ou a melhor achada no tempo limite.")},
    {"id": "modes", "at": "opt-modes", "t": N_("Por endereço × Pool"), "tour": "optimization", "href": "/optimization?branch_id={b}",
     "d": N_("Por endereço: produtos ficam, bins trocam. Pool: bins ficam, produtos mudam de endereço. O primeiro é mais simples de executar; o segundo costuma liberar mais espaço.")},
    {"id": "abc", "at": "file", "t": N_("Curva ABC"), "tour": "history", "href": "/imports/history",
     "d": N_("Classificação por popularidade no histórico: A são os SKUs que somam 80% das saídas (poucos itens, muito movimento), B os que levam até 95% e C o resto.")},
    {"id": "gold", "at": "gzEditor", "t": N_("Zonas de ouro"), "tour": "structure", "href": "/structures/new",
     "d": N_("Da pega: a faixa de altura confortável (ex.: 75–150 cm). Da caminhada: os endereços mais perto da expedição (padrão: os 20% mais perto). Os produtos A devem estar nas duas.")},
    {"id": "slotting", "at": "opt-slot", "t": N_("Posição pela saída (slotting)"), "tour": "optimization", "href": "/optimization/pool?branch_id={b}&slot=1",
     "d": N_("Decidir o endereço de cada produto pela popularidade: os mais procurados nos melhores lugares, para reduzir caminhada e esforço na separação.")},
    {"id": "affinity", "at": "file", "t": N_("Afinidade"), "tour": "history", "href": "/imports/history",
     "d": N_("Produtos que saem juntos no mesmo pedido. Deixá-los perto um do outro encurta a rota. O sistema considera um par quando ele aparece em pelo menos 20% dos pedidos do menos vendido dos dois.")},
    {"id": "sim", "at": "t_v", "t": N_("Tempo de separação"), "tour": "routes", "href": "/optimization/slotting/routes?branch_id={b}",
     "d": N_("Caminhada ÷ velocidade + paradas × tempo por parada + peças × tempo por peça + itens fora da zona de ouro × tempo extra. Endereços do mesmo módulo contam como uma parada.")},
]


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
         {"id": "branch", "t": N_("Filial"), "d": N_("O local da operação, com um código curto (ex.: CD01) igual ao do seu ERP/WMS. Tudo o mais pertence a uma filial."),
          "href": "/branches/new", "tour": "branch", "done": "branches"},
         {"id": "warehouse", "t": N_("Armazéns"), "d": N_("Só se a filial tem depósitos fisicamente separados (ex.: seco e refrigerado): a otimização nunca mistura armazéns."),
          "href": "/warehouses/new", "tour": "warehouse", "done": "warehouses", "optional": True},
         {"id": "bin_types", "t": N_("Tipos de bin"), "d": N_("As caixas, gavetas ou paletes com as medidas INTERNAS. Inclua os tipos que você poderia comprar: o sistema só propõe tipos cadastrados."),
          "href": "/bin-types/new", "tour": "bin_type", "done": "bin_types"},
         {"id": "structures", "t": N_("Estruturas de armazenagem"),
          "d": N_("Os módulos (estantes, porta-paletes) com frente, níveis e zona de ouro. Com eles, a zona de ouro e a caminhada usam medidas reais em vez de estimativas."),
          "href": "/structures/new", "tour": "structure", "done": "structures", "optional": True},
         {"id": "locations", "t": N_("Endereços"), "d": N_("Corredor-Estante-Nível-Posição (ex.: A-01-2-1). Gere em lote a partir das faixas (ex.: corredores A a D, estantes 01 a 20) ou importe."),
          "href": "/locations/batch", "tour": "batch", "done": "locations"},
         {"id": "bins", "t": N_("Bins nos endereços"), "d": N_("Quais bins (tipo e quantidade) cada endereço tem hoje, ex.: 4 × CX-P. É o \"antes\" que a otimização compara com a proposta."),
          "href": "/locations", "tour": "locations", "done": "location_bins"},
         {"id": "levels", "t": N_("Capacidade dos níveis"),
          "d": N_("Quantas bins de cada tipo cabem em cada nível (ex.: 6 × CX-P). É o limite que a otimização por endereço respeita."),
          "href": "/levels?branch_id={b}", "tour": "levels", "done": "shelf_levels", "optional": True},
         {"id": "products", "t": N_("Produtos"), "d": N_("Importe os SKUs com as medidas da unidade que fica na bin (cm e kg). Sem medidas, o produto aparece como \"Faltam dimensões\"."),
          "href": "/imports/products", "tour": "import_upload", "done": "products"},
         {"id": "stock", "t": N_("Saldos"), "d": N_("Importe endereço, SKU e quantidade. É a quantidade que precisa caber nas bins propostas."),
          "href": "/imports/stock", "tour": "import_upload", "done": "stock"},
     ]},
    {"id": "optimize", "t": N_("Otimizar as bins"), "icon": "bi-sliders",
     "d": N_("Com o cadastro pronto, veja quantas bins e quanto espaço você economiza."),
     "steps": [
         {"id": "address", "t": N_("Otimização por endereço"),
          "d": N_("Os produtos ficam onde estão e o sistema troca as bins. Comece com o objetivo \"Menos volume de bins\" e o motor de pesquisa operacional."),
          "href": "/optimization?branch_id={b}", "tour": "optimization", "done": "run_address"},
         {"id": "pool", "t": N_("Pool de bins"), "d": N_("As bins ficam e os produtos mudam de endereço, agrupando-se em menos bins. Libera mais espaço, mas exige movimentar produtos."),
          "href": "/optimization/pool?branch_id={b}", "tour": "optimization", "done": "run_pool"},
         {"id": "runs", "t": N_("Resultados salvos"), "d": N_("Compare cenários (ex.: enchimento 85% × 90%) e exporte o plano de implantação para a equipe executar."),
          "href": "/optimization/runs", "tour": "runs", "done": "tour"},
     ]},
    {"id": "slotting", "t": N_("Posição pela saída e trajeto"), "icon": "bi-signpost-2",
     "d": N_("Use o histórico de pedidos para pôr os produtos que mais saem nos melhores lugares e medir a caminhada."),
     "steps": [
         {"id": "history", "t": N_("Histórico de saídas"), "d": N_("Pedido, SKU, quantidade e data dos últimos 3 a 6 meses. Dá a curva ABC e os pares que saem juntos."),
          "href": "/imports/history", "tour": "history", "done": "history"},
         {"id": "slot_pool", "t": N_("Pool com posição pela saída"),
          "d": N_("Escolha o objetivo \"Menor tempo de separação\": os SKUs da curva A vão para perto da expedição e para a zona de ouro. O resultado compara hoje × proposta em caminhada e horas."),
          "href": "/optimization/pool?branch_id={b}&slot=1", "tour": "optimization", "done": "run_slot"},
         {"id": "routes", "t": N_("Simulador de trajeto"), "d": N_("Calibre os tempos (velocidade, parada, peça) com a sua operação e simule pedidos e dias inteiros para estimar horas de separação."),
          "href": "/optimization/slotting/routes?branch_id={b}", "tour": "routes", "done": "tour"},
     ]},
    {"id": "plan", "t": N_("Planta e 3D"), "icon": "bi-bricks",
     "d": N_("Desenhe a planta para medir a caminhada pelos corredores e ver o estoque em 3D."),
     "steps": [
         {"id": "map", "t": N_("Mapa do estoque"), "d": N_("Ocupação e preenchimento de cada corredor, estante e nível: mostra onde há bins grandes demais para o saldo."),
          "href": "/layout?branch_id={b}", "tour": "layout", "done": "tour"},
         {"id": "plan", "t": N_("Planta da filial"), "d": N_("Gere automaticamente, ajuste fileiras e obstáculos e marque a expedição: a caminhada passa a ser medida pelos corredores reais."),
          "href": "/layout/plan?branch_id={b}", "tour": "plan", "done": "layouts"},
         {"id": "view3d", "t": N_("Vista 3D e caminhada"), "d": N_("Pinte por curva ABC ou zona de ouro para ver onde estão os produtos mais procurados e simule a separação andando pelos corredores."),
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
    """Trilhas com o estado de cada etapa (True/False): pelo cadastro ou, nas etapas "tour", pelo tour já visto pelo
    usuário logado. Só as etapas do cadastro contam no progresso e na sugestão da próxima etapa."""
    from services.auth import current_user

    checks, ids = _checks(db), work_ids(db)
    seen = user_seen(db, current_user())
    tracks, nxt = [], None
    total = done_n = 0
    for tr in TRACKS:
        steps = []
        for st in tr["steps"]:
            done = bool(seen.get(st["tour"])) if st["done"] == "tour" else checks.get(st["done"], False)
            href = _href(st["href"], ids)
            steps.append({**st, "href": href, "go": f"{href}#nk-tour={st['tour']}", "state": done})
            if not st.get("optional") and st["done"] != "tour":
                total += 1
                done_n += bool(done)
        tracks.append({**tr, "steps": steps,
                       "complete": all(s["state"] for s in steps if not s.get("optional"))})
    for tr in tracks[1:] + tracks[:1]:  # próxima etapa: o cadastro real vem antes do caminho de exemplo
        for s in tr["steps"]:
            if s["state"] is False and not s.get("optional") and s["done"] != "tour":
                nxt = nxt or (tr, s)
    # "Ver onde aparece": abre a tela e começa o tour na etapa do conceito (#nk-tour=<id>@<trecho do seletor>)
    concepts = [{**c, "go": f"{_href(c['href'], ids)}#nk-tour={c['tour']}" + (f"@{c['at']}" if c.get("at") else "")}
                for c in CONCEPTS]
    return {"tracks": tracks, "next": nxt, "concepts": concepts, "total": total, "done": done_n,
            "pct": round(done_n * 100 / total) if total else 0, "empty": not checks["branches"]}


def tours_payload(lang: Optional[str] = None) -> list[dict]:
    """Tours traduzidos para o navegador."""
    from services.i18n import units_in_text

    tr = lambda s: units_in_text(lookup(s, lang))  # noqa: E731
    return [{"id": k, "p": v["p"], "t": tr(v["t"]),
             "s": [{"s": st["s"], "t": tr(st["t"]), "d": tr(st["d"]), "c": tr(st["c"]) if st["c"] else ""}
                   for st in v["s"]]} for k, v in TOURS.items()]


# --------------------------------------------------------------------------- progresso por usuário
def user_seen(db, me: Optional[dict]) -> dict:
    """Tours já vistos pelo usuário ({id: "done" | "seen"})."""
    if not me:
        return {}
    from models import User

    user = db.get(User, me["id"])
    return dict(user.tour_seen or {}) if user else {}


def mark_seen(user, tour_id: str, finished: bool) -> None:
    """Marca o tour como visto pelo usuário ("done" = chegou ao fim; um "done" não volta para "seen")."""
    if tour_id not in TOURS:
        return
    seen = dict(user.tour_seen or {})
    if seen.get(tour_id) != "done":
        seen[tour_id] = "done" if finished else "seen"
    user.tour_seen = seen


def reset_progress(user) -> None:
    """Zera o onboarding do usuário: todos os tours como não vistos e o convite do guia de volta ao Painel."""
    user.tour_seen = {}
    prefs = dict(user.prefs or {})
    prefs.pop("welcome_hidden", None)
    user.prefs = prefs
