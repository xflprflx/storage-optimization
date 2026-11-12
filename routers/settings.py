"""Configurações do sistema (endereços)."""
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
    app_settings.save(db, {"addr_sort": "alpha" if form.get("addr_sort") == "alpha" else "natural",
                           "addr_zero_eq": form.get("addr_zero_eq") == "1"})
    return redirect("/settings", _("Configurações salvas."))
