"""Configurações do sistema (endereços e tempos padrão da simulação de separação)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from routers.common import form_data, redirect, render
from services import app_settings
from services.i18n import N_, _

router = APIRouter(prefix="/settings", tags=[N_("Configurações")])


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    app_settings.load(db)
    return render(request, "settings.html", s={k: app_settings.get(k) for k in app_settings.DEFAULTS})


@router.post("")
def settings_save(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    from services.file_reader import parse_decimal

    values = {"addr_sort": "alpha" if form.get("addr_sort") == "alpha" else "natural",
              "addr_zero_eq": form.get("addr_zero_eq") == "1"}
    bad = []
    for k, (lo, hi) in app_settings.SIM_LIMITS.items():  # tempos padrão da simulação
        try:
            v = parse_decimal(form.get(k))
        except ValueError:
            v = None
        if v is None or not (lo <= float(v) <= hi):
            bad.append(k)
            continue
        values[k] = float(v)
    app_settings.save(db, values)
    if bad:
        return redirect("/settings", _("Configurações salvas, exceto tempos de simulação inválidos (fora dos limites)."), "warning")
    return redirect("/settings", _("Configurações salvas."))
