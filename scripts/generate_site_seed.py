"""Gera as planilhas dos projetos de exemplo da filial nova em samples/exemplos/<exemplo>.xlsx.

Uso:  python scripts/generate_site_seed.py

Os dados de cada exemplo (imóvel, zonas, obstáculos, estruturas, bins e SKUs) ficam em services/site_examples.py,
que também desenha a prévia da planta na tela de escolha. Mesmo formato do modelo de importação do projeto
(abas Instruções, Imóvel, Zonas, Obstáculos, Estruturas, Bins e SKUs). Os dados são determinísticos.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.site_examples import write_all  # noqa: E402

if __name__ == "__main__":
    for p in write_all():
        print(f"Planilha gerada: {p}")
