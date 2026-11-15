"""Entrar, sair e "Minha conta" (troca da própria senha)."""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import User
from routers.common import form_data, redirect, render
from services import auth
from services.i18n import N_, _

router = APIRouter(tags=[N_("Acesso")])


def _safe_next(nxt: str) -> str:
    """Só caminhos internos (evita redirecionar para outro site depois do login)."""
    u = urlparse(nxt or "/")
    if u.scheme or u.netloc or not (nxt or "/").startswith("/") or (nxt or "").startswith("//"):
        return "/"
    return nxt or "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if auth.current_user():
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", next=_safe_next(next), error=None, username="")


@router.post("/login")
def login_submit(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    username = (form.get("username") or "").strip()
    nxt = _safe_next(form.get("next") or "/")
    user = auth.authenticate(db, username, form.get("password") or "")
    if user is None:
        wait = auth.locked_for(username)
        error = (_("Muitas tentativas erradas. Tente de novo em {m} min.", m=max(1, round(wait / 60))) if wait
                 else _("Usuário ou senha incorretos."))
        return render(request, "login.html", status_code=401, next=nxt, error=error, username=username)
    token = auth.start_session(db, user)
    resp = RedirectResponse("/account?first=1" if user.must_change_password else nxt, status_code=303)
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    auth.end_session(db, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@router.get("/account")
def account_page(request: Request, db: Session = Depends(get_db)):
    user = db.get(User, auth.current_user()["id"])
    return render(request, "account.html", user=user, first=bool(request.query_params.get("first")) or user.must_change_password)


@router.post("/account")
def account_save(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    user = db.get(User, auth.current_user()["id"])
    name = (form.get("name") or "").strip()[:150]
    new, confirm = form.get("new_password") or "", form.get("confirm_password") or ""
    first = user.must_change_password
    if name:
        user.name = name
    if new or first:
        error = None
        if not auth.verify_password(form.get("current_password") or "", user.password_hash):
            error = _("A senha atual não confere.")
        elif new != confirm:
            error = _("A confirmação é diferente da senha nova.")
        elif auth.verify_password(new, user.password_hash):
            error = _("A senha nova precisa ser diferente da atual.")
        else:
            error = auth.password_problem(new, user.username)
        if error:
            db.rollback()
            return render(request, "account.html", status_code=400, user=user, first=first, error=error)
        user.password_hash = auth.hash_password(new)
        user.must_change_password = False
        auth.end_user_sessions(db, user.id, keep_token=request.cookies.get(auth.COOKIE))  # sai dos outros aparelhos
    db.commit()
    if first:
        return redirect("/", _("Senha definida. Bem-vindo!"))
    return redirect("/account", _("Conta atualizada."))
