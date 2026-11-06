# Neokioski — Gestão de Estoque (FastAPI + Jinja2 + SQLite)

Sistema web local para cadastro de depósitos, endereços, bins e produtos, importação de saldos,
mapa visual do estoque e otimização de bins. Sem login/autenticação nesta versão.

## Como rodar

Requisitos: Python 3.10 ou superior.

**Windows**
```
python -m pip install -r requirements.txt
start.bat
```
Use `python -m pip` (e não só `pip`) para instalar no mesmo Python que o `start.bat` usa — evita o erro
`No module named uvicorn`.

**Linux/macOS**
```
python -m pip install -r requirements.txt
python -m uvicorn main:app --port 8000
```

Acesse http://127.0.0.1:8000 . O banco `neokioski.db` é criado na primeira execução.
Variável opcional: `NEOKIOSKI_DB_URL` (padrão `sqlite:///./neokioski.db`).

## Atualizar para uma nova versão

Substitua os arquivos do projeto e **mantenha o `neokioski.db`**. Ao iniciar, o sistema ajusta o banco
sozinho: cria tabelas/colunas novas e reaplica as regras de consistência (ocupação dos endereços, endereço
padrão dos produtos, níveis dedicados e vão dos níveis). Faça uma cópia do `.db` antes, por segurança.

## Git

O `.gitignore` deixa fora do repositório o banco (`*.db`, `-wal`, `-shm`), a pasta `uploads/` (arquivos
temporários das importações), `__pycache__`, ambientes virtuais, `.env`, caches de ferramentas e arquivos de
editores/sistema. Se o banco ou o `__pycache__` já tinham sido versionados:
`git rm --cached neokioski.db` e `git rm -r --cached __pycache__`, depois commit.

## Estrutura
```
main.py                   entrypoint FastAPI (lifespan cria/ajusta o banco e limpa uploads antigos)
database.py               engine SQLite (FK ON, WAL), Base, DecimalType exato, migrações automáticas
models/entities.py        Branch, Warehouse (N:N), StorageLocation, Product (+Dimensions, tipo de material,
                          min/max level), StockItem, BinType, ShelfLevel (nível físico + vão), LevelCapacity,
                          LocationBin (+ nível onde a bin está)
services/
  addressing.py           montagem/parse de endereços, regra genérico x compartimento
  batch_generator.py      geração em lote (ALPHABETIC/NUMERIC/EXACT), exceções, conflitos
  file_reader.py          leitura CSV/XLSX em streaming, mapeamento por índice/nome, números BR
  master_importer.py      importação de filiais, armazéns, tipos de bin, endereços e capacidade dos níveis
  product_importer.py     importação de produtos em lotes (cria filial/armazém por código)
  stock_importer.py       importação de saldos (soma de várias colunas de quantidade)
  stock_service.py        ocupação, endereço padrão, cache de filiais/armazéns, associação de SKUs
  material_service.py     tipo de material: normalização, opções e filtro
  bin_service.py          bins por endereço (atribuição individual e em massa)
  level_service.py        níveis físicos: capacidade, uso, vão, níveis dedicados
  layout_service.py       árvore física do depósito para o mapa
  bin_optimizer.py        otimização de bins por endereço (algoritmo puro: capacidade, objetivos, restrições)
  pool_optimizer.py       otimização cega aos endereços (pool de bins, algoritmo puro)
  pool_service.py         carrega o pool do banco, executa e aplica o plano de movimentação
  bin_content_service.py  conteúdo de cada bin física (presumido/informado, validação, edição)
  packing.py              arranjo das unidades na bin (orientação, grade, posições, desenho)
  action_plan.py          plano de ação comum aos dois modos (passos, de-para, antes × depois)
  plan_export.py          exportação do plano em Excel (openpyxl) e PDF (reportlab)
  optimization_service.py carrega o escopo do banco, executa e aplica as sugestões
routers/                  dashboard, layout, optimization, packing (3D), levels, branches, warehouses, locations,
                          bin_types, products, stock, imports, api
templates/                Jinja2 + Bootstrap 5
uploads/                  arquivos temporários das importações (apagados após 24 h; não versionar)
```

## Cadastros

- **Filiais e armazéns**: um armazém pode pertencer a várias filiais.
- **Endereço**: `Corredor-Estante-Nível-Posição[-Compartimento]`, único por filial+armazém. Só o corredor é
  obrigatório. Uma posição é *genérica* (sem compartimento) **ou** possui compartimentos — nunca ambos.
- **Geração em lote**: faixas ALPHABETIC/NUMERIC/EXACT por nível da hierarquia, com **exceções** (ex.: na estante
  04 os níveis vão de 01 a 03). Pré-visualize antes de gravar. "Substituir posições desocupadas" remove as antigas
  conflitantes apenas se nenhuma estiver ocupada. Endereços idênticos e livres mantêm o mesmo ID. Limite: 50.000 por lote.
  **Pré-visualizar** mostra os números e, logo abaixo, o **próprio mapa do estoque** (mesmo desenho, zoom, cores,
  destaque e dicas) com o lote aplicado em simulação: endereços novos em azul tracejado, já com as bins do lote; os
  antigos que seriam substituídos somem; os existentes em conflito ficam com borda vermelha (a dica diz o motivo).
  Abre no corredor do lote; dá para trocar de corredor ou abrir em tela cheia. Nada é gravado até "Gerar agora".
- **Ocupação**: um endereço está OCUPADO sempre que tiver algum item atrelado, mesmo sem saldo — registro de
  saldo (inclusive zero) ou produto com o endereço como padrão. Calculada automaticamente. Endereços ocupados
  não podem ser excluídos nem substituídos na geração em lote.
- **Produtos**: SKU, nome, **tipo de material**, dimensões (cm), peso (kg), **min level / max level** (unidades no
  endereço padrão) e endereço padrão.
  - **Medidas em massa** (rodapé da lista): marque os produtos (ou "todos os filtrados", pela busca/tipo de material)
    e informe altura, largura, comprimento e/ou peso — campo vazio não muda — ou copie as medidas de um SKU modelo.
    A distribuição presumida nas bins é recalculada.
  - **Endereço padrão = endereço onde o produto está.** Com saldo lançado, é mantido automaticamente: se estiver
    vazio ou apontar para um endereço sem saldo do produto, passa a ser o endereço de maior saldo. O usuário pode
    escolher outro entre os que têm saldo. Produto sem saldo usa o endereço informado.
  - **Tipo de material**: texto livre com sugestões dos tipos já cadastrados (a grafia existente é reaproveitada,
    sem diferenciar maiúsculas).
  - Na lista, a coluna Endereço padrão mostra o padrão em destaque e os demais endereços como "também em".
- **Tipos de bin**: dimensões internas (cm), peso máximo (kg) e volume (L; vazio = calculado pelas dimensões).
  Um endereço pode ter várias bins, de tipos diferentes. Atribuição no cadastro do endereço, na geração em lote
  (endereços novos) ou em massa na lista de endereços (selecionados ou todos os filtrados): adicionar, definir,
  substituir, remover tipo ou limpar.

## Níveis das estantes (`/levels` — Capacidade dos níveis)

- **Capacidade**: "neste nível cabem X bins do tipo Y", por nível físico (Corredor-Estante-Nível), em tabela ou em
  lote por condições (ex.: estantes 01-04, níveis 01,03). Tipos diferentes são alternativas; o uso é
  Σ(bins do tipo ÷ capacidade do tipo), 100% = cheio.
- **Vão do nível** (distância até o nível de cima, cm): por nível ou em lote (ex.: corredor de porta-paletes = 150).
  **A estrutura acompanha a bin**: se uma bin é mais alta que o vão, o vão daquele nível sobe para a altura da bin;
  a bin nunca é trocada nem reduzida por causa do vão.
- **Níveis reservados**: níveis sem endereço, para receber bins de outros níveis da estante.
- **Excluir endereço** (um ou em massa): a confirmação tem a opção **Excluir também a posição física**. Com ela, o
  nível da estante (vão e capacidades) sai do mapa se ficar sem nenhum endereço e sem bins de outros endereços
  guardadas nele; senão, ele continua como nível reservado (vazio). A mensagem lista os níveis excluídos e os
  mantidos com o motivo. A escolha fica lembrada no navegador.
- **Níveis dedicados**: se as bins de um endereço passam da capacidade do nível dele (ex.: cabem 3× Y e são 6× Y),
  o excedente vai, um nível cheio por vez, para o nível logo acima **se ele suportar todo o excedente** (com o que
  já tem); senão, é criado um **nível dedicado** ao endereço logo acima (código `04D1`, `04D2`…, com a capacidade e
  o vão do nível do endereço). Níveis dedicados vazios são removidos automaticamente.
- **Outro nível, por escolha do usuário**: no cadastro do endereço, cada linha de bin tem a coluna "Nível" (níveis
  da mesma estante). A escolha é mantida desde que caiba — um nível pertence aos seus endereços; bins de fora que o
  deixam acima da capacidade voltam para o endereço dono e são realocadas pela regra acima.
- As regras de níveis são aplicadas ao salvar o endereço, na atribuição em massa, na geração em lote, ao mudar a
  capacidade/vão e ao iniciar o sistema (o vão também ao aplicar a otimização); as mensagens informam o que foi
  ajustado.

## Importações (CSV/XLSX)

Todas seguem o mesmo fluxo: enviar arquivo → mapear colunas (sugeridas pelo nome) → resultado com erros por linha.
Ordem recomendada: **1. Filiais → 2. Armazéns → 3. Tipos de bin → 4. Endereços → 5. Capacidade dos níveis →
6. Produtos → 7. Saldos**. Cada tela de cadastro tem o botão "Importar" e cada importação tem um modelo CSV.

- **Filiais**: código (obrigatório) e nome. Existentes têm o nome atualizado (opção).
- **Armazéns**: código, nome e filiais (lista "01, 02"); cria filiais inexistentes e acrescenta as associações.
- **Tipos de bin**: código, nome, altura/largura/comprimento (cm), peso máx. (kg), volume (L). Existentes são
  atualizados só nos campos preenchidos.
- **Endereços**: endereço completo (A-01-02-03) ou corredor/estante/nível/posição/compartimento (se os dois forem
  mapeados, valem os separados); filial/armazém (ou padrão), tipo de bin e quantidade (várias linhas do mesmo endereço
  somam). Endereços existentes não são duplicados; conflitos de hierarquia são recusados por linha. As bins do arquivo
  substituem as do endereço (ou somam, se a opção for desmarcada); depois valem as regras de níveis.
- **Capacidade dos níveis**: filial, armazém, corredor (obrigatório), estante e nível (valor, lista 01,03, faixa
  01-04 ou vazio/"(todas)"), tipo de bin + quantos cabem (0 remove) e/ou vão (cm).

- **Produtos**: só SKU obrigatório; colunas mapeáveis: nome, tipo de material, altura, largura, comprimento, peso,
  estoque mínimo/máximo (min/max level), filial, armazém e endereço. Cabeçalhos conhecidos são mapeados
  automaticamente. Filiais/armazéns inexistentes são criados só com o código. Campos vazios não apagam dados
  existentes. Lotes de 500 linhas com commit por lote. Modelo CSV disponível na tela.
- **Saldos**: filial, armazém, posição, SKU e uma ou mais colunas de quantidade (somadas). O saldo do endereço é
  sobrescrito; filial/armazém/posição criados se não existirem. Opções: somar duplicadas ou última prevalece,
  zerar ausentes, criar SKUs. Ao final, ocupação e endereço padrão dos produtos são atualizados.
- **Sincronizar** (opção em todas, desmarcada por padrão; em Saldos é o "zerar ausentes"): exclui o que **não está no
  arquivo** e não está em uso. Só roda se **nenhuma linha** der erro e houver ao menos uma linha válida; senão nada é
  excluído e o resultado avisa. O que fica por estar em uso é listado com o motivo.
  - Filiais: sai a filial sem endereços/níveis.
  - Armazéns: sai o armazém sem endereços/níveis; com a coluna Filiais, as associações ficam iguais às do arquivo
    (a que tem endereços/níveis fica).
  - Tipos de bin: sai o tipo não usado em endereço (com as capacidades de nível dele); bin pai de compartimento que
    fica é mantida.
  - Endereços: nas filiais/armazéns do arquivo, sai o endereço livre (sem ocupação nem item atrelado); opção de excluir
    também a posição física (nível) que ficar vazia.
  - Capacidade dos níveis: nas filiais/armazéns do arquivo, saem as capacidades (nível × tipo) não informadas e, com a
    coluna Vão mapeada, os vãos não informados (níveis dedicados ficam).
  - Produtos: sai o produto sem saldo.
  - Saldos: zera os saldos ausentes nas filiais/armazéns do arquivo.
- Números aceitos: `1.234,56`, `1234.56`, `10,5`. Códigos que o Excel converte em número (`01` → `1`) são
  associados aos cadastros existentes.

## Mapa do estoque (`/layout`)

- Elevação frontal por corredor, em escala: cada endereço tem a largura (bins lado a lado) e a altura (maior bin)
  das suas bins; endereços sem bin dimensionada usam 40×30 cm e aparecem tracejados.
- Cada nível mostra a área da capacidade cadastrada, o % de uso e o vão (↕). A altura do nível é o vão cadastrado
  (ou a bin mais alta).
- **Porta-paletes do mesmo corredor têm a mesma altura**: a estante mais baixa é esticada aumentando a distância
  entre os níveis (as bins mantêm o tamanho real).
- Bins de um endereço em outro nível (níveis dedicados, reservados ou escolhidos) aparecem no nível onde estão,
  como bins normais com contorno azul tracejado e o endereço dono (passe o mouse para ligar ao dono).
- Cores por ocupação, % de enchimento ou tipo de bin; destaque por endereço/SKU; zoom; clique abre o endereço.

## Otimização de bins (`/optimization`)

Dois modos, em abas, com **a mesma tela**: formulário à esquerda (1. Escopo, 2. O que otimizar, 3. Restrições) e, à
direita, os mesmos cartões de indicadores, o quadro "Bins por tipo", a barra de ações (plano de ação, Excel, PDF,
aplicar selecionados, aplicar todos), filtros por situação e a mesma tabela (Hoje × Plano com desenhos e link para o
3D, Δ bins, Δ volume, Δ largura, enchimento hoje × plano, colunas ordenáveis).

| | Por endereço | Pool de bins |
|---|---|---|
| O que muda | as bins de cada endereço (tipo e quantidade); os produtos ficam | os produtos mudam de endereço; as bins ficam |
| Bins: existentes / recurso infinito | existentes = o total de cada tipo não passa do que existe no escopo (as bins trocam de endereço); infinito = pode sugerir bins a mais (aplicável; "Bins por tipo" mostra quantas obter) | existentes = só as bins do escopo; infinito = quantas bins de cada tipo seriam necessárias (simulação: bins novas não têm endereço) |
| Quantidade: saldo / min / max level | sim | sim |
| Objetivos | menos bins, menos volume, menor largura de frente, mínimo de mudanças | menos bins, menos volume, menor largura de frente, mínimo de movimentações |
| Tipos permitidos, enchimento, margem, geometria/rotação, peso, SKUs misturados | sim | sim |
| Um único tipo de bin | por endereço | por SKU |
| Máximo de bins | por endereço (+ largura/altura máximas) | por SKU |
| Específicas | outros níveis da estante, um tipo por nível, não exceder espaço atual, remover bins de vazios | mesmo SKU em mais de um endereço |
| Situações (filtro) | Pode melhorar, Corrigir, Liberar, Já está ótimo, Inviável, Faltam dimensões, Sem saldo, Cede bins e é rearranjado, Cede bins: itens ficam fora de bin | Mover, Sem bin, Dimensionado, Desalojado e realocado, Fica onde está |
| Aplicar | selecionados ou todos | selecionados (os SKUs que precisam sair das bins de destino vão junto, com aviso) ou todos |
| Plano de ação, Excel, PDF, tela do caso com 3 cenários | sim | sim |

**Foco nos SKUs filtrados** (os dois modos, ligado por padrão): com filtro de "SKU ou produto" e/ou "Tipo de
material" e **bins existentes**, as bins podem vir do resto do escopo (filial, armazém, "endereço contém", bins atuais).
- **Pool**: o filtro escolhe só **quais produtos** otimizar; o item pode ir para qualquer bin do escopo. Os outros
  produtos ficam onde estão (o "aplicar" só regrava os endereços que o plano toca).
- **Por endereço**: o filtro escolhe os endereços a otimizar, e eles podem receber bins dos outros endereços do escopo
  (as bins trocam de endereço; o total de cada tipo não muda).

Desligado, o filtro de SKU escolhe os endereços e só as bins deles entram, como antes.

**Bins que podem ser usadas** (com foco + bins existentes):
- **Só bins livres** (padrão): só bins sem saldo de outros itens. Ex.: o item cabe em 2 G, mas as G têm saldo de
  outro produto → o plano usa as 14 P livres.
- **Qualquer bin**: pode usar bins com saldo de outros itens (no exemplo, as 2 G). Os itens que perdem a bin seguem
  a opção **Itens que perderem a bin**:
  - **Realocar** (padrão): no pool, vão para as bins que sobraram ou para bins livres, com o mínimo de movimentação
    (situação "Desalojado e realocado"). Por endereço, o endereço doador é rearranjado com as bins que ficam com ele
    mais as que sobraram no plano (situação "Cede bins e é rearranjado"). Doador que não consegue ser rearranjado não
    cede.
  - **Deixar fora de bin (órfãos)**: o doador cede as bins mais vazias. O conteúdo delas fica **fora de bin** no mesmo
    endereço (situação "Cede bins: itens ficam fora de bin"). A tela de resultado lista os órfãos, o plano de ação traz
    o passo "Retirar … fica fora de bin" e a tela do endereço mostra o aviso **Fora de bin (órfão)** até alguém
    informar a bin ou otimizar o item.

Endereços sem saldo que cedem bins aparecem como "Sem saldo: liberar bins" (cedem só o que o plano usa).

Restrições que não fazem sentido em um modo aparecem só no outro (o pool avisa que capacidade dos níveis,
largura/altura e "remover bins de vazios" não se aplicam, porque as bins não mudam de lugar nem de tipo).

- **Escopo**: filial, armazém, endereço, SKU, tipo de material e bins atuais.
- **Quantidade considerada**: saldo atual, **min level** ou **max level** (min/max valem no endereço padrão do
  produto, inclusive sem saldo lançado; SKU sem o nível cadastrado, **ou com 0**, usa o saldo e o resultado avisa).
  O planejado nunca fica abaixo do saldo físico do endereço (min level menor que o saldo → vale o saldo).
- **Capacidade dos níveis** (por endereço, bins existentes): a sugestão nunca põe num nível mais bins do que ele
  comporta, contando as bins dos outros endereços. Tipos sem capacidade cadastrada no nível têm a capacidade
  **estimada pela largura útil do nível**, o que permite sugerir **troca de tipo de bin**; a observação avisa.
- **Pool**: o motor ignora a divisão por endereço e enxerga só as bins físicas (pool por filial + armazém); pode usar
  qualquer quantidade das bins de qualquer endereço. Em empate, prefere as bins dos endereços onde o SKU já está.
  Sem "mesmo SKU em mais de um endereço", cada SKU fica inteiro em um único endereço.
- Quando o planejado (min/max level ou margem) é maior que o saldo, as bins recebem só as **unidades físicas** e o
  resto aparece como **reserva** ("CX-M #1 · 9 (+4 reserva)"; tracejado nos desenhos). O "aplicar" grava só o saldo.
- **Bins existentes × recurso infinito** trata só da **quantidade de bins disponível**; a capacidade dos níveis e as
  demais restrições valem nos dois casos. Com *existentes*, no modo por endereço, se um tipo faltar, o motor refaz
  primeiro as correções (saldo que não cabe) e depois as melhorias, com o que sobra de cada tipo; melhoria sem bins
  disponíveis fica como está, com aviso. Bins de endereços vazios só entram no estoque com "remover bins de
  endereços sem saldo". Ao aplicar só alguns endereços, os que liberam as bins recebidas vão junto (com aviso).
  No pool, *recurso infinito* é só simulação (não se aplica); no modo por endereço, pode ser aplicado.
- SKUs sem dimensões ficam onde estão.

## Motor: heurística ou pesquisa operacional

Na seção "2. O que otimizar" dos dois modos:

- **Motor**: *Pesquisa operacional* (padrão) ou *Heurística* (a de antes, mantida). A pesquisa operacional monta um
  modelo de otimização inteira (OR-Tools CP-SAT) para o escopo inteiro e o resolve de uma vez:
  - **Por endereço**: cada endereço escolhe uma das configurações candidatas (as mesmas da heurística) e quantas bins
    de cada tipo vão para cada nível; todos juntos respeitam a capacidade dos níveis (conta exata, com as bins dos
    endereços fora do escopo), o estoque de bins (bins existentes) e "um tipo por nível". Assim um endereço não
    "pega" o espaço que outro precisaria — ex.: dois endereços que não cabiam juntos na heurística passam a caber.
  - **Pool**: bins iguais do mesmo endereço formam um grupo (endereço × tipo); o modelo decide quantas unidades de
    cada SKU vão para cada grupo e quantas bins usa, com capacidade, mistura de SKUs, um endereço por SKU, um tipo
    por SKU e máximo de bins por SKU. Depois o plano é montado nas bins físicas (as bins onde o SKU já está primeiro).
    Em escopos grandes, cada SKU considera até 30 grupos (os de onde ele está, os da heurística e os melhores por
    volume, largura e capacidade).
- **Ordem de prioridade**: o objetivo escolhido é o 1º critério; "Depois, desempatar por" define o 2º, 3º e 4º
  (bins, volume, largura, mudanças/movimentações). Padrão: objetivo → mínimo de mudanças → demais. O solver resolve
  critério a critério (lexicográfico): fixa o melhor valor de um e otimiza o próximo. Antes de tudo, minimiza as
  violações (falta de bins, endereço sem solução). Com "padrão", a heurística por endereço mantém o desempate antigo.
- **Tempo limite** (padrão 15 s, de 1 a 600): dividido entre as etapas. Se acabar, vale a melhor solução encontrada.
- **Nunca pior que a heurística**: a solução da heurística é o ponto de partida; se, no tempo, o solver não a
  superar, o resultado é o da heurística (com aviso para aumentar o tempo). Sem OR-Tools instalado, idem.
- **Cartão "Pesquisa operacional"** no resultado: situação (ótimo comprovado / melhor solução no tempo / heurística),
  tempo, e para cada critério, na ordem, o valor da heurística × o da pesquisa operacional e se a etapa foi ótima.
- O plano aplicado é exatamente o analisado: a análise fica em cache (30 min) enquanto os dados não mudarem.
- Dependência: `ortools` (no `requirements.txt`). Rode `pip install -r requirements.txt` ao atualizar.

## Compartimentos (bin fração de uma bin inteira)

- No cadastro do tipo de bin, a seção **Compartimento de uma bin inteira** registra a **Bin pai** e a divisão em
  grade: **na largura** (colunas, lado a lado) × **no comprimento** (fileiras, da frente para o fundo), nos dois
  sentidos ao mesmo tempo. Ex.: 2 × 1 = meia bin (1/2); 2 × 3 = 6 compartimentos (1/6). O formulário mostra a grade
  vista de cima e as medidas de cada compartimento.
- Com as dimensões vazias, vêm da bin pai: largura ÷ colunas, comprimento ÷ fileiras, mesma altura; peso e volume ÷
  nº de compartimentos (e acompanham se a bin pai mudar). A bin pai precisa ser uma bin inteira e não pode ser
  excluída enquanto tiver compartimentos.
- Importação de tipos de bin: colunas Bin pai, Divisões na largura e Divisões no comprimento (alternativa: Fração
  1/n + Divisão largura/comprimento). Frações da versão anterior viram grade n×1 ou 1×n automaticamente.
- **Numeração**: os compartimentos de um endereço enchem as bins pai em ordem — da esquerda para a direita, da frente
  para o fundo (`1/6 CX-P #8` = CX-P nº 2, posição 2 de 6).
- **Mapa**: compartimentos ocupam bins pai inteiras (7× `1/6 CX-P` = 2× `CX-P` de largura). De frente aparecem as
  colunas (divisórias tracejadas) e, em cada coluna, as fileiras como faixas (a de baixo é a da frente);
  compartimentos sem uso ficam clareados.
- **Arranjo das unidades (2D e 3D)**: o compartimento aparece dentro da bin pai, na sua posição da grade, com as
  divisórias nos dois sentidos; os outros compartimentos ficam em cinza (mesmo vazio, o compartimento é desenhado).
- **Capacidade dos níveis e otimização**: sem capacidade própria cadastrada no nível, vale a da bin pai × nº de
  compartimentos; a largura de frente de um compartimento é a largura da bin pai ÷ nº de compartimentos.

## Conteúdo das bins (bin física)

- Cada bin física é identificada por **endereço + tipo + nº** (ex.: `B-03-02-03 · CX-M #2` = a 2ª CX-M do endereço;
  primeiro as do nível do endereço, depois as de níveis extras). Essa é a numeração usada nas etiquetas dos planos.
- O sistema guarda **o que está em cada bin** (SKU e quantidade). Onde não foi informado, usa uma **distribuição
  presumida** (marcada como "presumido"): um SKU por bin, respeitando a capacidade, e **nenhuma bin fica vazia**
  enquanto houver unidades para dividir — as bins que sobrariam vão para o SKU mais apertado e cada SKU é dividido
  entre as suas bins na proporção da capacidade (ex.: 10 un. em 2 CX-P = 5 + 5).
- Nas sugestões, a divisão usa os mesmos parâmetros da otimização (enchimento máximo, rotação, peso): se a otimização
  pediu 2 bins por causa do enchimento de 85%, as unidades são divididas entre as 2. Quando o planejado (min/max level
  ou margem) é maior que o saldo, o saldo é dividido entre as bins planejadas e o resto fica como reserva — nenhuma bin
  fica só com reserva (exceto no objetivo "mínimo de movimentações", que não mexe no que já está).
- Informar: no cadastro do endereço (quadro "Conteúdo das bins"), na importação de saldos (coluna opcional **Bin**,
  ex.: `CX-M #2`) ou aplicando um plano do pool. A soma de cada SKU nas bins precisa ser igual ao saldo do endereço;
  se o saldo ou as bins mudarem e deixar de bater, o endereço volta para a distribuição presumida.

## Arranjo das unidades na bin (2D e 3D)

- Para cada bin, a tela mostra **como as unidades ficam dentro dela**: qual medida do produto fica na vertical, a base,
  quantas por camada e quantas camadas (ex.: "altura na vertical · base 10×8 cm · 3×5 por camada · 2 de 4 camadas"),
  com um desenho pequeno (vista de cima e de frente). Unidades que não cabem pela geometria são sinalizadas.
- Aparece na otimização por endereço (coluna "Arranjo das unidades", **hoje** e **depois**, mudando ou não), no pool
  de bins (cada bin de hoje e do plano, inclusive bins novas no modo recurso infinito) e no quadro "Conteúdo das bins"
  do endereço.
- As **camadas são numeradas** de baixo (1) para cima: na vista de frente (números à esquerda), na vista de cima (cada
  coluna mostra o nº da camada mais alta) e no 3D (marcadores ao lado da bin). A legenda traz "camada n: x un.".
- Cada item tem link para a **tela do caso** (`/optimization/case`): o caso é o endereço (otimização por endereço) ou
  o SKU (pool). A tela mostra, em acordeão, o **Antes (hoje)** e o **Depois em até 3 cenários: saldo atual, min level
  e max level**, escolhidos por checkbox no topo (o cenário usado na otimização vem marcado). Cada cenário recalcula a
  otimização com os mesmos parâmetros e escopo, mudando só a quantidade considerada, e informa "planejado x un. ·
  saldo hoje y un." (e quais SKUs não têm o nível cadastrado). Todas as bins do caso aparecem em 3D (girar, zoom,
  vistas de cima/frente/lado) e em 2D; o link abre já rolado até a bin clicada. Usa three.js (carregado da internet,
  como o Bootstrap). O quadro "Conteúdo das bins" do endereço abre `/packing/case` (só o antes) e `/packing/3d`
  continua disponível para ver uma bin isolada.
- A orientação segue a rotação escolhida (só girar no plano ou qualquer orientação). Com vários SKUs na mesma bin, eles
  ficam um após o outro ao longo do comprimento (aproximação).

## Plano de ação (os dois modos)

Nos resultados da otimização (por endereço e pool de bins), os botões **Plano de ação**, **Excel** e **PDF** geram o
plano com os mesmos parâmetros da análise:
- cabeçalho com escopo e parâmetros usados;
- **antes × depois** dos indicadores (bins, volume, largura, enchimento; no pool: bins usadas, endereços ocupados,
  movimentações e unidades), com variação e barras;
- **gráfico de bins por tipo** (antes × depois / existentes × usadas ou necessárias);
- **passo a passo** numerado e agrupado (por corredor / origem), na ordem de execução — retirar antes de colocar;
  no pool, "mover N un. do SKU de A para B (bins …)" e os endereços que ficam vazios; com bins ilimitadas,
  quantas bins providenciar e como armazenar cada SKU — com coluna "Feito" para usar como checklist;
- **sequência segura**: no pool, os passos são ordenados por dependência — uma movimentação só aparece depois das que
  esvaziam a bin de destino — e agrupados em **ondas** (os passos de uma onda podem ser feitos em qualquer ordem).
  A coluna **"Destino no momento"** diz se a bin está livre agora, livre após o passo N, se já tem o mesmo SKU ou se
  será dividida. Trocas circulares (A→B e B→A) passam pela **área de espera** (nome configurável, ex.: DOCA 1).
  Na otimização por endereço, cada endereço tem: esvaziar as bins que mudam (para a área de espera) → retirar/colocar
  bins → guardar o conteúdo nas bins novas;
- **visual por endereço** (bins antes × depois, em escala; no pool, bins usadas antes × depois);
- **de-para** completo.
O Excel traz as abas Resumo (com gráfico), Passo a passo, De-para e Bins por tipo (com gráfico); o PDF (A4 paisagem)
traz as mesmas seções. Requer o pacote `reportlab` (já no `requirements.txt`).

## Telas e tabelas

- Filtros e ordenação por qualquer coluna em todas as listas; busca sem diferenciar acentos/maiúsculas.
- **Como buscar** (vale em todas as caixas de busca): `10*` começa com · `*10` termina com · `14*87` começa com 14 e termina com 87 (`*` = qualquer sequência, `?` = um caractere) · `=10` igual ·
  `"duas palavras"` frase inteira · `campo:termo` só naquele campo (`sku:`, `nome:`, `tipo:`, `end:`). Várias palavras:
  todas precisam aparecer. Ex.: `sku:10* end:A-10*` = SKU começa com 10 e está num endereço que começa com A-10.
  Na lista de produtos também há os seletores **Buscar em** (tudo, SKU, nome, tipo de material, endereço) e **Como**
  (contém, começa com, termina com, igual a); o botão **?** ao lado da busca mostra a ajuda.
- Cabeçalho congelado, rolagem horizontal e largura das colunas ajustável (arraste a borda do cabeçalho; fica
  salva no navegador por página; duplo clique volta ao automático).
- Endereços: colunas de bins, tipo de material (todos os tipos dos produtos atrelados), SKUs e quantidades.
