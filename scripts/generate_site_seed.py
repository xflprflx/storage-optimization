"""Gera a planilha de exemplo (seed) de um projeto de filial nova: samples/seed_filial_nova.xlsx.

Uso:  python scripts/generate_site_seed.py

Mesmo formato do modelo de importação do projeto (abas Instruções, Imóvel, Estruturas, Bins e SKUs), com dados
fictícios mas realistas de uma filial de materiais de construção/manutenção: galpão de 48 × 30 m, estantes de
picking de 3 tamanhos e um porta-palete (desligado), 5 caixas bin com custo e ~450 SKUs com dimensões, peso,
saldo, min level e max level. Os dados são determinísticos (semente fixa).
"""
from __future__ import annotations

import random
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

OUT = Path(__file__).resolve().parent.parent / "samples" / "seed_filial_nova.xlsx"

BUILDING = [
    ("Comprimento (m)", 48),
    ("Largura (m)", 30),
    ("Pé-direito útil (m)", 7.5),
    ("Largura dos corredores (m)", 1.2),
    ("Afastamento das paredes (m)", 0.3),
    ("Faixa reservada na frente — docas/expedição (m)", 8),
    ("Faixa reservada no fundo (m)", 3),
    ("Corredores transversais (qtd.)", 1),
    ("Largura dos transversais (m)", 2),
    ("Orientação das fileiras", "automática"),
]
# código, nome, tipo, frente, profundidade, altura, níveis, vão, carga/nível, custo, modo, qtd fixa, mín, máx
STRUCTURES = [
    ("EST-100-40", "Estante leve 100 × 40, 6 níveis", "Estante", 100, 40, 210, 6, 33, 100, 420, "Otimizar", None, None, None),
    ("EST-100-60", "Estante média 100 × 60, 5 níveis", "Estante", 100, 60, 210, 5, 40, 150, 560, "Otimizar", None, None, None),
    ("EST-120-60", "Estante reforçada 120 × 60, 4 níveis", "Estante", 120, 60, 240, 4, 58, 250, 790, "Otimizar", None, None, 60),
    ("PP-270-110", "Porta-palete 270 × 110, 3 níveis", "Porta-palete", 270, 110, 600, 3, 160, 2000, 2400, "Não usar", None, None, None),
]
# código, nome, altura, largura, comprimento (cm, internas), peso máx. (kg), volume (L, vazio = calculado), custo (R$)
BINS = [
    ("BIN-3", "Caixa bin nº 3", 12, 14, 25, 5, None, 6.90),
    ("BIN-5", "Caixa bin nº 5", 17, 19, 33, 10, None, 12.50),
    ("BIN-7", "Caixa bin nº 7", 18, 30, 38, 15, None, 24.90),
    ("BIN-8", "Caixa bin nº 8", 21, 40, 55, 25, None, 46.00),
    ("CX-50", "Caixa organizadora 50 L", 32, 38, 52, 30, 50, 58.00),
]
# família, material, (alt, larg, comp) mín–máx (cm), peso (kg) mín–máx, giro (multiplicador de saldo)
FAMILIES = [
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
SIZES = ["3/16", "1/4", "5/16", "3/8", "1/2", "M6", "M8", "M10", "M12", "6 mm", "8 mm", "10 mm", "16 A", "20 A",
         "25 A", "32 A", "20 mm", "25 mm", "32 mm", "P", "M", "G", "GG", "1,5 mm²", "2,5 mm²", "4 mm²", "9 W", "12 W"]


def _skus(rng: random.Random) -> list[tuple]:
    out = []
    n = 0
    for fam, mat, (h0, h1), (w0, w1), (l0, l1), (p0, p1), giro in FAMILIES:
        for size in rng.sample(SIZES, k=rng.randint(18, 28)):
            n += 1
            h, w, ln = (round(rng.uniform(a, b), 1) for a, b in ((h0, h1), (w0, w1), (l0, l1)))
            peso = round(rng.uniform(p0, p1), 3)
            # max level ~ consumo de 30–60 dias; min ~ 30% do max; saldo entre o min e o max
            mx = max(4, int(rng.lognormvariate(3.6, 0.9) * giro))
            mn = max(1, int(mx * rng.uniform(0.2, 0.4)))
            saldo = rng.randint(mn, mx)
            out.append((f"NV{n:05d}", f"{fam} {size}", mat, h, w, ln, peso, saldo, mn, mx))
    # alguns casos para mostrar avisos: sem dimensão e sem quantidade
    out.append(("NV90001", "Item sem dimensões cadastradas", "Diversos", None, None, None, 1.0, 10, 5, 20))
    out.append(("NV90002", "Item descontinuado (sem quantidade)", "Diversos", 5, 5, 5, 0.1, 0, None, None))
    return out


def build(path: Path = OUT) -> Path:
    rng = random.Random(20261107)
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
        "Planilha de exemplo (seed) — Projeto de filial nova",
        "",
        "Dados fictícios de uma filial de materiais de construção/manutenção, prontos para importar num projeto:",
        "Otimização de bins → Projeto de filial nova → Criar projeto a partir de planilha (ou Importar, dentro do projeto).",
        "Imóvel: medidas em metros. Estruturas: medidas em cm; Modo = Otimizar, Fixa ou Não usar. Bins: medidas internas "
        "em cm, peso em kg, custo em R$. SKUs: dimensões em cm, peso em kg por unidade, saldo/min/max em unidades.",
        "Ao importar, as estruturas e bins são cadastradas (códigos existentes de bins mantêm as medidas; o custo é "
        "atualizado). Os SKUs ficam só no projeto.",
        "Inclui 1 SKU sem dimensões e 1 sem quantidade para mostrar os avisos do plano.",
    ]:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 130
    sheet(wb.create_sheet("Imóvel"), ["Campo", "Valor"], BUILDING, {1: 50, 2: 16})
    sheet(wb.create_sheet("Estruturas"),
          ["Código", "Nome", "Tipo", "Frente (cm)", "Profundidade (cm)", "Altura (cm)", "Níveis", "Vão útil (cm)",
           "Carga por nível (kg)", "Custo por módulo (R$)", "Modo", "Qtd fixa", "Mín.", "Máx."], STRUCTURES, {2: 36})
    sheet(wb.create_sheet("Bins"),
          ["Código", "Nome", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso máx. (kg)", "Volume (L)",
           "Custo por bin (R$)"], BINS, {2: 28})
    sheet(wb.create_sheet("SKUs"),
          ["SKU", "Produto", "Material", "Altura (cm)", "Largura (cm)", "Comprimento (cm)", "Peso (kg)", "Saldo",
           "Min level", "Max level"], _skus(rng), {2: 36})
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


if __name__ == "__main__":
    p = build()
    print(f"Planilha gerada: {p}")
