"""Catálogo de projetos de exemplo da filial nova (planilhas prontas para importar).

Cada exemplo tem os dados do imóvel, zonas, obstáculos, estruturas, bins e um gerador de SKUs. Os mesmos dados
servem para gerar a planilha (`write_workbook`, usado por scripts/generate_site_seed.py) e para desenhar a prévia
da planta na tela de escolha (`preview_svg`), antes de criar o projeto.

As estruturas e bins usam os mesmos códigos e medidas em todos os exemplos (ao importar, um exemplo não altera o
que outro cadastrou); muda só o modo de cada estrutura no projeto.
"""
from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = BASE_DIR / "samples" / "exemplos"

# código, nome, tipo, frente, profundidade, altura, níveis, vão, carga/nível, custo
STRUCTURES = {
    "EST-100-40": ("Estante leve 100 × 40, 6 níveis", "Estante", 100, 40, 210, 6, 33, 100, 420),
    "EST-100-60": ("Estante média 100 × 60, 5 níveis", "Estante", 100, 60, 210, 5, 40, 150, 560),
    "EST-120-60": ("Estante reforçada 120 × 60, 4 níveis", "Estante", 120, 60, 240, 4, 58, 250, 790),
    "PP-270-110": ("Porta-palete 270 × 110, 3 níveis", "Porta-palete", 270, 110, 600, 3, 160, 2000, 2400),
}
# zona de ouro (cm do piso) das estantes: entre a cintura e o ombro
GOLDEN = {"EST-100-40": (75, 150), "EST-100-60": (75, 150), "EST-120-60": (75, 150),
          "PP-270-110": (0, 160)}  # porta-palete: a separação boa é no nível do chão
# código, nome, altura, largura, comprimento (cm, internas), peso máx. (kg), volume (L, vazio = calculado), custo (R$)
BINS = {
    "BIN-3": ("Caixa bin nº 3", 12, 14, 25, 5, None, 6.90),
    "BIN-5": ("Caixa bin nº 5", 17, 19, 33, 10, None, 12.50),
    "BIN-7": ("Caixa bin nº 7", 18, 30, 38, 15, None, 24.90),
    "BIN-8": ("Caixa bin nº 8", 21, 40, 55, 25, None, 46.00),
    "CX-50": ("Caixa organizadora 50 L", 32, 38, 52, 30, 50, 58.00),
    "PALETE": ("Palete PBR 120 × 100 (carga até 150 cm)", 150, 120, 100, 1000, None, 85.00),
}
# família, material, (alt, larg, comp) mín–máx (cm), peso (kg) mín–máx, giro (multiplicador de saldo)
SMALL = [
    ("Parafuso sextavado", "Fixação", (2, 6), (2, 6), (4, 10), (0.05, 0.4), 3.0),
    ("Bucha de nylon", "Fixação", (2, 5), (3, 6), (5, 9), (0.02, 0.2), 4.0),
    ("Abraçadeira", "Fixação", (2, 4), (4, 9), (10, 22), (0.05, 0.3), 2.0),
    ("Disjuntor", "Elétrico", (8, 9), (2, 6), (6, 8), (0.1, 0.5), 1.5),
    ("Tomada", "Elétrico", (3, 5), (6, 8), (10, 12), (0.08, 0.2), 2.0),
    ("Cabo flexível (rolo)", "Elétrico", (8, 12), (18, 25), (18, 25), (1.5, 6.0), 0.6),
    ("Lâmpada LED", "Elétrico", (6, 7), (6, 7), (11, 13), (0.05, 0.1), 2.5),
    ("Joelho PVC", "Hidráulico", (4, 9), (4, 9), (5, 11), (0.02, 0.2), 2.5),
    ("Registro de gaveta", "Hidráulico", (6, 10), (6, 12), (10, 16), (0.3, 1.2), 0.8),
    ("Torneira", "Hidráulico", (8, 12), (10, 16), (18, 28), (0.4, 1.5), 0.6),
    ("Fita isolante", "Elétrico", (2, 3), (9, 10), (9, 10), (0.03, 0.06), 3.0),
    ("Luva de proteção", "EPI", (2, 4), (12, 14), (24, 28), (0.05, 0.15), 2.0),
    ("Óculos de segurança", "EPI", (5, 6), (6, 7), (16, 18), (0.04, 0.08), 1.5),
    ("Chave combinada", "Ferramentas", (1, 2), (3, 5), (14, 30), (0.1, 0.5), 0.8),
    ("Broca", "Ferramentas", (1, 2), (2, 3), (10, 18), (0.02, 0.1), 1.5),
    ("Trena", "Ferramentas", (5, 8), (6, 9), (7, 10), (0.15, 0.4), 0.7),
    ("Silicone (tubo)", "Químico", (5, 6), (5, 6), (22, 25), (0.3, 0.35), 1.2),
    ("Lixa", "Abrasivo", (1, 1), (23, 23), (28, 28), (0.01, 0.02), 3.0),
]
BULKY = [  # itens volumosos/pesados, que vão para palete
    ("Argamassa 20 kg", "Construção", (10, 12), (35, 36), (45, 47), (20, 20), 4.0),
    ("Tinta 18 L", "Químico", (36, 37), (32, 33), (32, 33), (22, 24), 2.0),
    ("Caixa de piso 2,5 m²", "Construção", (8, 9), (45, 46), (45, 46), (21, 23), 3.0),
    ("Rejunte 5 kg (caixa c/ 4)", "Construção", (20, 22), (30, 31), (40, 41), (20, 21), 2.0),
    ("Cimento 50 kg", "Construção", (12, 13), (40, 41), (60, 62), (50, 50), 5.0),
]
SIZES = ["3/16", "1/4", "5/16", "3/8", "1/2", "M6", "M8", "M10", "M12", "6 mm", "8 mm", "10 mm", "16 A", "20 A",
         "25 A", "32 A", "20 mm", "25 mm", "32 mm", "P", "M", "G", "GG", "1,5 mm²", "2,5 mm²", "4 mm²", "9 W", "12 W"]
COLORS = ["branco", "gelo", "cinza", "areia", "grafite", "bege", "marfim", "preto", "azul", "verde"]


def _skus(rng: random.Random, families: list, per_family: tuple, variants: list, start: int = 1,
          extras: bool = True, scale: float = 1.0, prefix: str = "NV") -> list[tuple]:
    out, n = [], start - 1
    for fam, mat, (h0, h1), (w0, w1), (l0, l1), (p0, p1), giro in families:
        for v in rng.sample(variants, k=min(len(variants), rng.randint(*per_family))):
            n += 1
            h, w, ln = (round(rng.uniform(a, b), 1) for a, b in ((h0, h1), (w0, w1), (l0, l1)))
            peso = round(rng.uniform(p0, p1), 3)
            # max level ~ consumo de 30–60 dias; min ~ 30% do max; saldo entre o min e o max
            mx = max(4, int(rng.lognormvariate(3.6, 0.9) * giro * scale))
            mn = max(1, int(mx * rng.uniform(0.2, 0.4)))
            out.append((f"{prefix}{n:05d}", f"{fam} {v}", mat, h, w, ln, peso, rng.randint(mn, mx), mn, mx))
    if extras:  # casos para mostrar avisos: sem dimensão e sem quantidade
        out.append((f"{prefix}90001", "Item sem dimensões cadastradas", "Diversos", None, None, None, 1.0, 10, 5, 20))
        out.append((f"{prefix}90002", "Item descontinuado (sem quantidade)", "Diversos", 5, 5, 5, 0.1, 0, None, None))
    return out


EXAMPLES = [
    {
        "slug": "galpao-medio",
        "title": "Galpão médio de picking",
        "description": "Galpão de 48 × 30 m com docas na frente, um corredor transversal e estantes de 3 tamanhos "
                       "(porta-palete desligado). Zona única, orientação automática.",
        "tags": ["48 × 30 m", "zona única", "~400 SKUs", "cabeceiras", "pedidos"],
        "building": {"length_m": 48, "width_m": 30, "clear_height_m": 7.5, "aisle_m": 1.2, "wall_gap_m": 0.3,
                     "front_reserved_m": 8, "back_reserved_m": 3, "cross_aisles": 1, "cross_aisle_m": 2,
                     "avenue_m": 2, "orientation": "automática", "end_caps": "sim", "cap_align": "encostada"},
        "zones": [], "obstacles": [],
        "structures": {"EST-100-40": ("Otimizar", None, None, None), "EST-100-60": ("Otimizar", None, None, None),
                       "EST-120-60": ("Otimizar", None, None, 60), "PP-270-110": ("Não usar", None, None, None)},
        "bins": ["BIN-3", "BIN-5", "BIN-7", "BIN-8", "CX-50"],
        "skus": lambda rng: _skus(rng, SMALL, (18, 28), SIZES, prefix="GM"),
        "orders": 1800,  # aba Pedidos: um mês de histórico (saídas com sinal negativo)
    },
    {
        "slug": "perimetro-escritorio",
        "title": "Fileiras nas paredes + escritório no canto",
        "description": "Imóvel de 24 × 26 m com zonas posicionadas por X/Y: fileiras perpendiculares encostadas nas "
                       "paredes da esquerda e da direita (esta abaixo do escritório), fileiras horizontais no meio (mais "
                       "curtas no alto), uma fileira curta perpendicular junto ao escritório e circulação entre as zonas.",
        "tags": ["24 × 26 m", "5 zonas X/Y", "obstáculo", "~350 SKUs", "pedidos"],
        "building": {"length_m": 24, "width_m": 26, "clear_height_m": 6, "aisle_m": 1.5, "wall_gap_m": 0.1,
                     "front_reserved_m": 0, "back_reserved_m": 0, "cross_aisles": 0, "cross_aisle_m": 2,
                     "avenue_m": 1.5, "orientation": "automática", "end_caps": "sim", "cap_align": "encostada"},
        "zones": [("Parede esquerda", 0.1, 0.1, 0.6, 25.8, "perpendicular", None, None, "EST-100-40"),
                  ("Central alto", 2.2, 0.1, 14.0, 7.3, "ao longo do comprimento", None, None, "EST-100-60"),
                  ("Junto ao escritório", 19.1, 0.1, 0.6, 7.3, "perpendicular", None, None, "EST-100-40"),
                  ("Central", 2.2, 9.0, 18.6, 16.9, "ao longo do comprimento", None, None, "EST-100-60"),
                  ("Parede direita", 23.3, 9.0, 0.6, 16.9, "perpendicular", None, None, "EST-100-40")],
        "obstacles": [("Escritório", 19.8, 0, 4.2, 7.4)],
        # paredes com a estante leve (mínimo de 45 módulos: preenche as fileiras de parede); centro com a média
        "structures": {"EST-100-60": ("Otimizar", None, None, None), "EST-100-40": ("Otimizar", None, 45, None)},
        "bins": ["BIN-3", "BIN-5", "BIN-7", "BIN-8"],
        "skus": lambda rng: _skus(rng, SMALL, (16, 24), SIZES, scale=4.5, prefix="PE"),
        "orders": 1500,  # aba Pedidos: um mês de histórico (saídas com sinal negativo)
    },
    {
        "slug": "imovel-estreito",
        "title": "Imóvel estreito com avenida no fundo",
        "description": "Loja/depósito de 14 × 3,2 m onde só cabe um corredor. Em vez de o corredor terminar na parede "
                       "(o colaborador vai até o fundo e volta), uma zona no fundo com fileira perpendicular de frente "
                       "para a avenida abre a circulação.",
        "tags": ["14 × 3,2 m", "2 zonas", "avenida", "~110 SKUs", "pedidos"],
        "building": {"length_m": 14, "width_m": 3.2, "clear_height_m": 4.5, "aisle_m": 1.2, "wall_gap_m": 0.1,
                     "front_reserved_m": 1.5, "back_reserved_m": 0, "cross_aisles": 0, "cross_aisle_m": 2,
                     "avenue_m": 1.2, "orientation": "automática", "end_caps": "sim", "cap_align": "encostada"},
        "zones": [("Picking", None, None, None, None, "ao longo do comprimento", None, None, ""),
                  ("Fundo", None, None, 1.4, None, "perpendicular", None, None, "")],
        "obstacles": [],
        "structures": {"EST-100-40": ("Otimizar", None, None, None), "EST-100-60": ("Otimizar", None, None, None)},
        "bins": ["BIN-3", "BIN-5", "BIN-7"],
        "skus": lambda rng: _skus(rng, SMALL[:12], (8, 10), SIZES, scale=0.5, prefix="IE"),
        "orders": 600,  # aba Pedidos: um mês de histórico (saídas com sinal negativo)
    },
    {
        "slug": "cd-paletes-picking",
        "title": "CD com porta-paletes e picking",
        "description": "Centro de distribuição de 60 × 36 m: zona de porta-paletes junto às docas, com corredor de "
                       "empilhadeira de 3,2 m, e zona de picking em estantes com corredor de 1,2 m e um transversal, "
                       "separadas por uma avenida de 3,5 m. Itens volumosos vão em palete; os pequenos, em bins. "
                       "Recebimento (60 m²) e expedição (45 m²) posicionados pelo sistema junto às docas.",
        "tags": ["60 × 36 m", "2 zonas", "porta-paletes", "setores", "~350 SKUs", "pedidos"],
        "building": {"length_m": 60, "width_m": 36, "clear_height_m": 9, "aisle_m": 1.2, "wall_gap_m": 0.3,
                     "front_reserved_m": 10, "back_reserved_m": 0, "cross_aisles": 0, "cross_aisle_m": 2,
                     "avenue_m": 3.5, "orientation": "automática", "end_caps": "sim", "cap_align": "encostada"},
        "zones": [("Porta-paletes", None, None, 28, None, "ao longo do comprimento", 3.2, None, "PP-270-110"),
                  ("Picking", None, None, None, None, "ao longo do comprimento", None, 1, "EST-100-60, EST-120-60")],
        "obstacles": [],
        # setores: só a área necessária; o sistema escolhe o lugar junto às docas
        "sectors": [("Recebimento", 60, 5, "docas"), ("Expedição", 45, 5, "docas")],
        "orders": 2500,  # aba Pedidos: histórico de um mês (saídas com sinal negativo)
        "structures": {"PP-270-110": ("Otimizar", None, None, None), "EST-100-60": ("Otimizar", None, None, None),
                       "EST-120-60": ("Otimizar", None, None, None)},
        "bins": ["BIN-3", "BIN-5", "BIN-7", "BIN-8", "PALETE"],
        "skus": lambda rng: _skus(rng, SMALL, (14, 20), SIZES, extras=False, prefix="CD")
        + _skus(rng, BULKY, (8, 10), COLORS, start=5001, extras=True, scale=3, prefix="CD"),
    },
]
BY_SLUG = {e["slug"]: e for e in EXAMPLES}
DEFAULT = "galpao-medio"


def path_of(slug: str) -> Path:
    return EXAMPLES_DIR / f"{slug}.xlsx"


# --------------------------------------------------------------------------- #
# Planilha
# --------------------------------------------------------------------------- #
def _orders(rng: random.Random, skus: list[str], n_orders: int) -> list[tuple]:
    """Histórico de pedidos fictício de um mês, no formato de relatório de movimentação (saída com sinal negativo):
    popularidade bem desigual (poucos SKUs saem muito) e grupos de itens que costumam sair juntos (ex.: parafuso,
    porca e arruela da mesma medida = SKUs vizinhos no código)."""
    import datetime as dt

    order = skus[:]
    rng.shuffle(order)
    weights = [1 / (i + 1) ** 1.1 for i in range(len(order))]  # Zipf: o 1º sai muito mais que o 100º
    idx = {s: i for i, s in enumerate(skus)}
    rows = []
    start = dt.date(2026, 10, 1)
    for n in range(1, n_orders + 1):
        lines = set(rng.choices(order, weights=weights, k=rng.choice((1, 1, 2, 2, 3, 4, 6))))
        for s in list(lines):  # itens "irmãos" (código vizinho) entram junto com boa chance
            i = idx[s]
            for j in (i + 1, i + 2):
                if j < len(skus) and rng.random() < 0.45:
                    lines.add(skus[j])
        day = start + dt.timedelta(days=rng.randrange(31))
        for s in sorted(lines):
            rows.append((f"P{n:05d}", day, s, -rng.choice((1, 1, 2, 3, 5, 10, 12))))
    return rows


def write_workbook(ex: dict, path: Optional[Path] = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    from services.site_service import BUILDING_FIELDS

    path = path or path_of(ex["slug"])
    rng = random.Random(f"neokioski-{ex['slug']}")
    head = (Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F2A2A"))
    wb = Workbook()

    def sheet(ws, cols, rows, widths=None):
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head
        for r in rows:
            ws.append(list(r))
        for i, _ in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, 15)
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Instruções"
    for line in [
        f"Projeto de exemplo — {ex['title']}",
        "",
        ex["description"],
        "",
        "Dados fictícios, prontos para importar: Otimização de bins → Projeto de filial nova → escolha o exemplo "
        "(ou Criar projeto a partir de planilha / Importar, dentro do projeto).",
        "Imóvel: medidas em metros. Zonas: faixas ao longo do comprimento a partir da frente; Obstáculos: X ao longo "
        "do comprimento e Y ao longo da largura, a partir do canto superior esquerdo da planta.",
        "Estruturas: medidas em cm; Modo = Otimizar, Fixa ou Não usar. Bins: medidas internas em cm, peso em kg, custo "
        "em R$. SKUs: dimensões em cm, peso em kg por unidade, saldo/min/max em unidades.",
        "Ao importar, as estruturas e bins são cadastradas (códigos existentes de bins mantêm as medidas; o custo é "
        "atualizado). Os SKUs ficam só no projeto.",
        "Pedidos: um mês de histórico (pedido, data, SKU, quantidade), no formato de relatório de movimentação — a "
        "saída vem com sinal negativo: marque \"Converter quantidades para valor absoluto\" no mapeamento. Serve para "
        "a posição dos SKUs pela saída (curva ABC e itens que saem juntos), no projeto e nos modos Por endereço e Pool.",
    ]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 130
    bd = ex["building"]
    rows = [(label, bd.get(k)) for k, label, _, _ in BUILDING_FIELDS if k in bd]
    rows += [("Orientação das fileiras", bd["orientation"]), ("Cabeceiras (sim/não)", bd["end_caps"]),
             ("Posição da cabeceira (encostada/borda)", bd["cap_align"])]
    sheet(wb.create_sheet("Imóvel"), ["Campo", "Valor"], rows, {1: 50, 2: 22})
    sheet(wb.create_sheet("Zonas"), ["Nome", "X (m)", "Y (m)", "Comprimento em X (m)", "Largura em Y (m)",
                                     "Orientação das fileiras", "Corredor (m)", "Corredores transversais",
                                     "Estruturas permitidas (códigos)"],
          ex["zones"], {1: 22, 6: 30, 9: 34})
    sheet(wb.create_sheet("Obstáculos"), ["Nome", "X (m)", "Y (m)", "Comprimento em X (m)", "Largura em Y (m)"],
          ex["obstacles"], {1: 24})
    sheet(wb.create_sheet("Setores"), ["Nome", "Área necessária (m²)", "Lado mínimo (m)",
                                       "Onde (automático, docas, fundo, lateral, fixa)"],
          ex.get("sectors") or [], {1: 22, 4: 40})
    sheet(wb.create_sheet("Estruturas"),
          ["Código", "Nome", "Tipo", "Frente (cm)", "Profundidade (cm)", "Altura (cm)", "Níveis", "Vão útil (cm)",
           "Carga por nível (kg)", "Custo por módulo (R$)", "Modo", "Qtd fixa", "Mín.", "Máx.",
           "Zona de ouro início (cm)", "Zona de ouro fim (cm)"],
          [(code, *STRUCTURES[code], *mode, *GOLDEN.get(code, (None, None))) for code, mode in ex["structures"].items()],
          {2: 36})
    sheet(wb.create_sheet("Bins"),
          ["Código", "Nome", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso máx. (kg)", "Volume (L)",
           "Custo por bin (R$)"], [(code, *BINS[code]) for code in ex["bins"]], {2: 36})
    skus = ex["skus"](rng)
    sheet(wb.create_sheet("SKUs"),
          ["SKU", "Produto", "Material", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso (kg)", "Saldo",
           "Min level", "Max level"], skus, {2: 36})
    if ex.get("orders"):
        sheet(wb.create_sheet("Pedidos"), ["Pedido", "Data", "SKU", "Quantidade"],
              _orders(random.Random(f"pedidos-{ex['slug']}"), [r[0] for r in skus], ex["orders"]), {1: 14, 2: 12})
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# --------------------------------------------------------------------------- #
# Prévia da planta (antes de otimizar): zonas, avenidas, obstáculos e fileiras disponíveis
# --------------------------------------------------------------------------- #
def building_of(ex: dict):
    from services.site_planner import Building, Obstacle, SectorSpec, StructureSpec, ZoneSpec

    bd = ex["building"]
    b = Building(**{k: float(bd[k]) for k in ("length_m", "width_m", "clear_height_m", "aisle_m", "wall_gap_m",
                                               "front_reserved_m", "back_reserved_m", "cross_aisle_m", "avenue_m")},
                 cross_aisles=int(bd["cross_aisles"]), end_caps=bd["end_caps"] == "sim",
                 cap_align="edge" if bd["cap_align"] == "borda" else "row")
    ids = {code: i + 1 for i, code in enumerate(STRUCTURES)}
    structs = []
    for code, (mode, *_r) in ex["structures"].items():
        if mode == "Não usar":
            continue
        name, _kind, w, d, h, lv, vh, load, cost = STRUCTURES[code]
        gf, gt = GOLDEN.get(code, (None, None))
        structs.append(StructureSpec(ids[code], code, name, w, d, h, lv, vh, load, cost, golden_from=gf, golden_to=gt))
    for name, x, y, ln, wd, ori, aisle, cross, codes in ex["zones"]:
        sids = {ids[c.strip()] for c in (codes or "").split(",") if c.strip()} or None
        b.zones.append(ZoneSpec(ln, "across" if ori.startswith("perp") else "along", aisle, int(cross or 0), sids, name,
                                x_m=x, y_m=y, width_m=wd))
    b.obstacles = [Obstacle(*o) for o in ex["obstacles"]]
    places = {"docas": "front", "fundo": "back", "lateral": "side", "automático": "auto"}
    b.sectors = [SectorSpec(n, a, s, places.get(p, "auto")) for n, a, s, p in ex.get("sectors") or []]
    return b, structs


@lru_cache(maxsize=None)
def preview_svg(slug: str, width_px: int = 520) -> str:
    from services.site_planner import SitePlan, StructLine, build_layout, floor_svg

    ex = BY_SLUG[slug]
    b, structs = building_of(ex)
    plan = SitePlan(building=b, floor=build_layout(b, structs), structs=[StructLine(struct=s) for s in structs])
    return floor_svg(plan, width_px).replace('style="max-height:70vh"', 'style="max-height:260px"')


def write_all() -> list[Path]:
    return [write_workbook(ex) for ex in EXAMPLES]
