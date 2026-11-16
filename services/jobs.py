"""Tarefas em segundo plano com progresso e cancelamento (buscas que rodam o otimizador várias vezes).

`start(owner, fn)` roda `fn(progress, cancelled)` numa thread (com o idioma e as unidades da requisição) e devolve o
token; `get(token, owner)` devolve o estado (só para o mesmo dono: filial, projeto…). Fica em memória: reiniciar o
servidor perde as tarefas em andamento."""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
from typing import Callable, Optional

from services.i18n import _

_JOBS: dict[str, dict] = {}


class Cancelled(Exception):
    """A busca foi cancelada pelo usuário."""


def start(owner: str, fn: Callable) -> str:
    for k in [k for k, j in _JOBS.items() if time.time() - j["t0"] > 3600]:  # limpa o que ficou parado há mais de 1 h
        _JOBS.pop(k, None)
    token = uuid.uuid4().hex
    job = {"owner": owner, "step": 0, "total": 0, "label": _("Começando"), "done": False, "error": "", "res": None,
           "cancel": False, "t0": time.time(), "t1": None}
    _JOBS[token] = job

    def progress(step, total, label):
        job.update(step=step, total=total, label=label)

    def work():
        try:
            job["res"] = fn(progress, lambda: job["cancel"])
        except Cancelled:
            job["error"] = _("Busca cancelada.")
        except Exception as exc:  # noqa: BLE001  (o erro volta para a tela de espera)
            job["error"] = _("Não foi possível concluir a busca: {exc}", exc=exc)
        finally:
            job["done"], job["t1"] = True, time.time()

    threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()
    return token


def get(token: str, owner: str) -> Optional[dict]:
    j = _JOBS.get(token or "")
    return j if j and j["owner"] == owner else None


def owner_of(token: str) -> Optional[str]:
    j = _JOBS.get(token or "")
    return j["owner"] if j else None


def status(j: Optional[dict]) -> dict:
    if j is None:
        return {"error": _("A busca expirou ou o servidor foi reiniciado: rode de novo.")}
    end = j["t1"] or time.time()
    return {"step": j["step"], "total": j["total"], "label": j["label"], "done": j["done"], "error": j["error"],
            "elapsed": round(end - j["t0"], 1), "cancelling": j["cancel"] and not j["done"]}
