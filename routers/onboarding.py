"""Onboarding integrado: guia passo a passo (trilhas com o progresso do cadastro) e os tours de cada tela."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from database import get_db
from routers.common import render
from services import onboarding
from services.i18n import N_

router = APIRouter(prefix="/onboarding", tags=[N_("Guia")])

HIDE_COOKIE = "nk_onb_hide"


@router.get("")
def guide_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "onboarding.html", prog=onboarding.progress(db))


@router.get("/tours.json")
def tours_json():
    """Tours de todas as telas no idioma atual (o navegador escolhe pelo caminho)."""
    return JSONResponse(onboarding.tours_payload(), headers={"Cache-Control": "no-cache"})


@router.post("/welcome")
def welcome(request: Request, show: str = ""):
    """Esconde (ou volta a mostrar) o convite do guia no painel."""
    resp = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    if show:
        resp.delete_cookie(HIDE_COOKIE)
    else:
        resp.set_cookie(HIDE_COOKIE, "1", max_age=5 * 365 * 86400, samesite="lax")
    return resp
