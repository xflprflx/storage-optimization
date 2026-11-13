"""
Configurações do sistema (tabela app_settings), com cache em memória.

Simulação de separação (sim_*): velocidade de caminhada (m/s), tempo fixo por parada, tempo por peça e tempos a mais
fora da zona de ouro da pega (por item e por peça) — valores iniciais das telas de simulação, otimização e 3D.

Endereços:
  - addr_sort: "natural" (os números ordenam pelo valor: 2 antes de 10) ou "alpha" (ordem alfabética: 10 antes de 2);
  - addr_zero_eq: True = zeros à esquerda não diferenciam os códigos, em qualquer parte do endereço (corredor,
    estante, nível, posição, compartimento): "F.07.01.01" e "F.7.01.02" são a mesma estante "F.7" / "F.07".
    Vale para conflitos de endereço, para agrupar estantes e níveis (mapa, planta, 3D, capacidade dos níveis,
    estruturas e posição pela saída) e para a ordenação.
"""
from __future__ import annotations

import re
from typing import Any

DEFAULTS: dict[str, Any] = {"addr_sort": "natural", "addr_zero_eq": False,
                            # simulação de separação (valores iniciais das telas; cada tela ainda pode mudar)
                            "sim_v": 1.0, "sim_stop": 10.0, "sim_piece": 2.0, "sim_gold": 5.0, "sim_gold_piece": 0.0}
# limites de cada valor da simulação (mínimo, máximo)
SIM_LIMITS = {"sim_v": (0.2, 3.0), "sim_stop": (0.0, 600.0), "sim_piece": (0.0, 600.0), "sim_gold": (0.0, 600.0),
              "sim_gold_piece": (0.0, 600.0)}
_cache: dict[str, Any] = dict(DEFAULTS)
_DIGITS = re.compile(r"\d+")


def load(db) -> None:
    from sqlalchemy import select

    from models import AppSetting

    _cache.clear()
    _cache.update(DEFAULTS)
    for s in db.scalars(select(AppSetting)):
        if s.key in DEFAULTS:
            _cache[s.key] = (s.value or {}).get("v", DEFAULTS[s.key])


def get(key: str) -> Any:
    return _cache.get(key, DEFAULTS.get(key))


def save(db, values: dict) -> None:
    from models import AppSetting

    for k, v in values.items():
        if k not in DEFAULTS:
            continue
        row = db.get(AppSetting, k) or AppSetting(key=k)
        row.value = {"v": v}
        db.add(row)
    db.commit()
    load(db)


def sim_times() -> dict[str, float]:
    """Tempos padrão da simulação de separação, com as chaves do simulador (t_v, t_stop, t_piece, t_gold, t_gold_piece)."""
    return {"t_v": float(get("sim_v")), "t_stop": float(get("sim_stop")), "t_piece": float(get("sim_piece")),
            "t_gold": float(get("sim_gold")), "t_gold_piece": float(get("sim_gold_piece"))}


def canon(code: str) -> str:
    """Código de um segmento (ou endereço) para comparar: maiúsculas e, com addr_zero_eq, os números sem zeros à
    esquerda ("07" -> "7", "C01" -> "C1")."""
    v = (code or "").strip().upper()
    if _cache.get("addr_zero_eq"):
        v = _DIGITS.sub(lambda m: str(int(m.group())), v)
    return v


def sql_canon(code) -> str:
    """Função SQL nk_canon (registrada na conexão): o mesmo que canon(), sempre com a configuração atual."""
    return canon(code if isinstance(code, str) else ("" if code is None else str(code)))
