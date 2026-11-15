"""Onboarding integrado: guia passo a passo (trilhas com o progresso do cadastro) e os tours de cada tela. O que cada
usuário já viu (tours e o convite do Painel) fica gravado no usuário."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from database import get_db
from models import User
from routers.common import redirect, render
from services import onboarding
from services.auth import current_user
from services.i18n import N_, _

router = APIRouter(prefix="/onboarding", tags=[N_("Guia")])


def _me(db: Session) -> User:
    return db.get(User, current_user()["id"])


@router.get("")
def guide_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "onboarding.html", prog=onboarding.progress(db))


@router.get("/tours.json")
def tours_json():
    """Tours de todas as telas no idioma atual (o navegador escolhe pelo caminho)."""
    return JSONResponse(onboarding.tours_payload(), headers={"Cache-Control": "no-cache"})


@router.post("/seen")
async def tour_seen(request: Request, db: Session = Depends(get_db)):
    """O navegador avisa que o usuário viu (ou concluiu) um tour: {"id": ..., "finished": true|false}."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    user = _me(db)
    onboarding.mark_seen(user, str(body.get("id") or ""), bool(body.get("finished")))
    db.commit()
    return JSONResponse({"seen": user.tour_seen or {}})


@router.post("/reset")
def reset(request: Request, db: Session = Depends(get_db)):
    """Marca tudo como não visto para o usuário logado (tours e o convite do guia no Painel)."""
    onboarding.reset_progress(_me(db))
    db.commit()
    from urllib.parse import urlparse

    ref = urlparse(request.headers.get("referer") or "")
    back = ref.path if ref.path.startswith("/") and not ref.path.startswith("//") else "/onboarding"
    return redirect(back or "/onboarding", _("Onboarding reiniciado: todos os tours estão como não vistos."))


@router.post("/welcome")
def welcome(request: Request, show: str = "", db: Session = Depends(get_db)):
    """Esconde (ou volta a mostrar) o convite do guia no Painel, para o usuário logado."""
    user = _me(db)
    prefs = dict(user.prefs or {})
    if show:
        prefs.pop("welcome_hidden", None)
    else:
        prefs["welcome_hidden"] = True
    user.prefs = prefs
    db.commit()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


def welcome_hidden(db: Session) -> bool:
    u = current_user()
    user = db.get(User, u["id"]) if u else None
    return bool(user and (user.prefs or {}).get("welcome_hidden"))
