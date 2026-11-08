"""
Resultados de otimização guardados (tabela optimization_runs): gravação automática a cada execução, sem duplicar a
mesma execução (mesmos parâmetros e mesmos dados), e comparação lado a lado.

Indicadores num formato comum (`summary["kpis"]`): [{"label", "before", "after", "value", "unit", "places",
"better": "low" | "high" | None, "hint"}] — Por endereço e Pool têm antes → depois; o projeto tem um valor.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import OptimizationRun

KINDS = {"address": "Por endereço", "pool": "Pool de bins", "site": "Projeto de filial nova"}
IGNORED_PARAMS = {"page", "sort", "dir", "status", "run", "order"}  # não mudam o resultado (só a exibição)
MAX_DETAILS = 3000


def _plain(v):
    """Valores JSON (Decimal/datetime -> número/texto; conjuntos -> listas)."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, (set, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def data_fingerprint(db: Session) -> tuple:
    """O que os dados de entrada têm (quantidades e contagens), sem datas de atualização: a manutenção feita ao
    iniciar o sistema não muda o resultado e não deve criar outro registro."""
    from sqlalchemy import text

    out = []
    for t, extra in (("stock_items", "TOTAL(quantity)"), ("location_bins", "TOTAL(quantity)"),
                     ("bin_types", "TOTAL(height) + TOTAL(width) + TOTAL(length)"), ("products", "COUNT(*)"),
                     ("level_capacities", "TOTAL(max_bins)"), ("shelf_levels", "TOTAL(height_cm)"),
                     ("storage_locations", "COUNT(*)"), ("stock_policies", "TOTAL(min_level) + TOTAL(max_level)"),
                     ("pick_histories", "TOTAL(LENGTH(data))")):
        try:
            out.append(tuple(db.execute(text(f"SELECT COUNT(*), {extra} FROM {t}")).one()))
        except Exception:  # noqa: BLE001
            out.append(())
    return tuple(out)


def run_key(kind: str, params: dict, fingerprint) -> str:
    raw = json.dumps({"k": kind, "p": {k: v for k, v in sorted(params.items()) if k not in IGNORED_PARAMS},
                      "f": _plain(fingerprint)}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def save_run(db: Session, kind: str, title: str, params: dict, summary: dict, details: dict, url: str,
             fingerprint=None, branch_id: Optional[int] = None, project_id: Optional[int] = None) -> OptimizationRun:
    """Grava (ou reaproveita, se a mesma execução já está guardada) e devolve o resultado."""
    key = run_key(kind, params, fingerprint)
    run = db.scalar(select(OptimizationRun).where(OptimizationRun.key == key).order_by(OptimizationRun.id.desc()))
    if run is None:
        run = OptimizationRun(kind=kind, key=key)
        db.add(run)
    run.title, run.url, run.branch_id, run.project_id = title[:200], url[:2000], branch_id, project_id
    run.params = _plain({k: v for k, v in params.items() if k not in IGNORED_PARAMS})
    run.summary = _plain(summary)
    rows = details.get("rows") or []
    run.details = _plain({**details, "rows": rows[:MAX_DETAILS], "total_rows": len(rows)})
    run.updated_at = datetime.now()
    db.commit()
    return run


def kpi_rows(runs: list[OptimizationRun]) -> list[dict]:
    """Linhas da comparação: um indicador por linha, o valor de cada resultado e a diferença para o 1º."""
    labels: list[str] = []
    for r in runs:
        for k in (r.summary or {}).get("kpis", []):
            if k["label"] not in labels:
                labels.append(k["label"])
    out = []
    for lb in labels:
        cells = []
        for r in runs:
            k = next((k for k in (r.summary or {}).get("kpis", []) if k["label"] == lb), None)
            v = None if k is None else (k.get("after") if k.get("after") is not None else k.get("value"))
            cells.append({"k": k, "v": v})
        base = cells[0]["v"]
        meta = next((c["k"] for c in cells if c["k"]), {}) or {}
        for c in cells:
            c["delta"] = (c["v"] - base) if isinstance(c["v"], (int, float)) and isinstance(base, (int, float)) else None
        out.append({"label": lb, "cells": cells, "unit": meta.get("unit", ""), "places": meta.get("places", 0),
                    "better": meta.get("better")})
    return out


def param_diff(runs: list[OptimizationRun]) -> list[dict]:
    """Parâmetros que mudam entre os resultados comparados."""
    keys = sorted({k for r in runs for k in (r.params or {})})
    out = []
    for k in keys:
        vals = [json.dumps((r.params or {}).get(k), ensure_ascii=False, sort_keys=True) for r in runs]
        if len(set(vals)) > 1:
            out.append({"key": k, "values": [(r.params or {}).get(k) for r in runs]})
    return out


def kpi_from_card(k: dict) -> dict:
    """Cartão das telas Por endereço/Pool (routers.optimization.kpi) -> indicador comum."""
    if "value" in k:
        return {"label": k["label"], "value": k["value"], "unit": "", "places": 0, "better": None,
                "hint": k.get("hint", "")}
    return {"label": k["label"], "before": k.get("before"), "after": k.get("after"), "unit": k.get("unit", ""),
            "places": 1 if k.get("pct") else k.get("places", 0),
            "better": None if k.get("before") is None else ("low" if k.get("good_negative", True) else "high"),
            "hint": k.get("hint", "")}
