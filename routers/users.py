"""Usuários (só administradores): criar, editar, redefinir senha, ativar/desativar e zerar o onboarding."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import User
from routers.common import form_data, redirect, render
from services import auth
from services.i18n import N_, _

router = APIRouter(prefix="/users", tags=[N_("Usuários")])


def require_admin() -> dict:
    u = auth.current_user()
    if not u or not u["is_admin"]:
        raise HTTPException(403, _("Só administradores podem gerenciar usuários."))
    return u


def _other_admins(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True), User.active.is_(True),
                                                                    User.id != user_id)) or 0


@router.get("")
def users_page(request: Request, db: Session = Depends(get_db), me: dict = Depends(require_admin)):
    users = list(db.scalars(select(User).order_by(func.lower(User.username))))
    return render(request, "users/list.html", users=users, me=me)


@router.get("/new")
def user_new(request: Request, me: dict = Depends(require_admin)):
    return render(request, "users/form.html", user=None, data={"active": True}, error=None)


@router.post("/new")
def user_create(request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db),
                me: dict = Depends(require_admin)):
    username = (form.get("username") or "").strip()
    data = {"username": username, "name": (form.get("name") or "").strip()[:150], "is_admin": form.get("is_admin") == "1",
            "active": True}
    pw = form.get("password") or ""
    error = None
    if not username or len(username) > 60 or any(c.isspace() for c in username):
        error = _("Informe um nome de usuário sem espaços (até 60 caracteres).")
    elif db.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
        error = _("Já existe um usuário {u}.", u=username)
    else:
        error = auth.password_problem(pw, username)
    if error:
        return render(request, "users/form.html", status_code=400, user=None, data=data, error=error)
    db.add(User(username=username, name=data["name"], is_admin=data["is_admin"], active=True,
                password_hash=auth.hash_password(pw), must_change_password=form.get("must_change") == "1"))
    db.commit()
    return redirect("/users", _("Usuário {u} criado.", u=username))


@router.get("/{uid}/edit")
def user_edit(uid: int, request: Request, db: Session = Depends(get_db), me: dict = Depends(require_admin)):
    user = db.get(User, uid)
    if not user:
        return redirect("/users", _("Usuário não encontrado."), "danger")
    return render(request, "users/form.html", user=user, data={"username": user.username, "name": user.name,
                                                                "is_admin": user.is_admin, "active": user.active}, error=None)


@router.post("/{uid}/edit")
def user_update(uid: int, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db),
                me: dict = Depends(require_admin)):
    user = db.get(User, uid)
    if not user:
        return redirect("/users", _("Usuário não encontrado."), "danger")
    is_admin, active = form.get("is_admin") == "1", form.get("active") == "1"
    data = {"username": user.username, "name": (form.get("name") or "").strip()[:150], "is_admin": is_admin, "active": active}
    error = None
    if (not is_admin or not active) and user.is_admin and user.active and not _other_admins(db, user.id):
        error = _("Mantenha pelo menos um administrador ativo.")
    pw = form.get("password") or ""
    if not error and pw:
        error = auth.password_problem(pw, user.username)
    if error:
        return render(request, "users/form.html", status_code=400, user=user, data=data, error=error)
    user.name, user.is_admin, user.active = data["name"], is_admin, active
    if pw:  # senha redefinida pelo administrador: provisória, o usuário troca no próximo acesso
        user.password_hash = auth.hash_password(pw)
        user.must_change_password = True
        auth.end_user_sessions(db, user.id)
    if not active:
        auth.end_user_sessions(db, user.id)
    db.commit()
    return redirect("/users", _("Usuário {u} atualizado.", u=user.username))


@router.post("/{uid}/reset-onboarding")
def user_reset_onboarding(uid: int, db: Session = Depends(get_db), me: dict = Depends(require_admin)):
    user = db.get(User, uid)
    if user:
        from services.onboarding import reset_progress

        reset_progress(user)
        db.commit()
    return redirect("/users", _("Onboarding de {u} zerado: tudo marcado como não visto.", u=user.username if user else ""))
