"""Autenticação: senhas (PBKDF2-SHA256 da biblioteca padrão), sessões no banco e o usuário da requisição.

- Senha: guardada só como hash "pbkdf2_sha256$<iterações>$<sal>$<hash>" (base64); a comparação é em tempo constante.
- Sessão: o cookie nk_session leva um token aleatório; o banco guarda só o SHA-256 dele, com validade de
  SESSION_DAYS dias (renovada com o uso). Sair, trocar a senha ou desativar o usuário encerra as sessões.
- Primeiro uso: sem nenhum usuário, cria o administrador "admin". A senha inicial vem de NEOKIOSKI_ADMIN_PASSWORD
  ou, sem ela, é a senha de instalação (só o hash fica no código) e precisa ser trocada no primeiro acesso.
- Tentativas erradas: depois de MAX_FAILS seguidas, o usuário fica bloqueado por LOCK_MINUTES minutos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

COOKIE = "nk_session"
SESSION_DAYS = 14
ITERATIONS = 600_000
MIN_PASSWORD = 8
MAX_FAILS, LOCK_MINUTES = 5, 5
ADMIN_USERNAME = "admin"
# hash da senha de instalação do administrador (a senha em si não fica no código)
INSTALL_ADMIN_HASH = "pbkdf2_sha256$600000$gjMVMzrG+/03FTtVGRhyCw==$ma7oceRoY4QU0Z6XoZlZjMj+kJPsNF1lbNUIDJjdxR0="

_current: ContextVar[Optional[dict]] = ContextVar("nk_user", default=None)
_fails: dict[str, list] = {}  # usuário -> [tentativas erradas seguidas, bloqueado até (epoch)]


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def password_problem(password: str, username: str = "") -> Optional[str]:
    """Motivo para recusar a senha nova (None = aceita)."""
    from services.i18n import _

    if len(password or "") < MIN_PASSWORD:
        return _("A senha precisa ter pelo menos {n} caracteres.", n=MIN_PASSWORD)
    if username and password.strip().lower() == username.strip().lower():
        return _("A senha não pode ser igual ao nome de usuário.")
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_admin(db: Session) -> None:
    """Sem nenhum usuário (instalação nova ou banco de versão anterior), cria o administrador."""
    from models import User

    if db.scalar(select(func.count()).select_from(User)):
        return
    env = os.getenv("NEOKIOSKI_ADMIN_PASSWORD")
    db.add(User(username=ADMIN_USERNAME, name="Administrador", is_admin=True, active=True,
                password_hash=hash_password(env) if env else INSTALL_ADMIN_HASH, must_change_password=not env))
    db.commit()


def locked_for(username: str) -> int:
    """Segundos que faltam do bloqueio por tentativas erradas (0 = liberado)."""
    n, until = _fails.get(username.lower(), [0, 0])
    return max(0, int(until - time.time()))


def authenticate(db: Session, username: str, password: str):
    """Usuário se a senha confere (e ele está ativo e não bloqueado); None caso contrário."""
    from models import User

    key = (username or "").strip().lower()
    if not key or locked_for(key):
        return None
    user = db.scalar(select(User).where(func.lower(User.username) == key))
    if user is not None and user.active and verify_password(password, user.password_hash):
        _fails.pop(key, None)
        return user
    if user is None:  # mesmo custo de uma senha errada: não revela se o usuário existe
        verify_password(password, INSTALL_ADMIN_HASH)
    n = _fails.get(key, [0, 0])[0] + 1
    _fails[key] = [0, time.time() + LOCK_MINUTES * 60] if n >= MAX_FAILS else [n, 0]
    return None


def start_session(db: Session, user) -> str:
    from models import UserSession

    token = secrets.token_urlsafe(32)
    now = datetime.now()
    db.add(UserSession(token_hash=_token_hash(token), user_id=user.id, expires_at=now + timedelta(days=SESSION_DAYS),
                       last_seen_at=now))
    user.last_login_at = now
    db.execute(delete(UserSession).where(UserSession.expires_at < now))  # limpa as vencidas
    db.commit()
    return token


def end_session(db: Session, token: Optional[str]) -> None:
    from models import UserSession

    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        db.commit()


def end_user_sessions(db: Session, user_id: int, keep_token: Optional[str] = None) -> None:
    """Encerra as sessões do usuário (troca de senha, desativação); keep_token mantém a sessão atual."""
    from models import UserSession

    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_token:
        stmt = stmt.where(UserSession.token_hash != _token_hash(keep_token))
    db.execute(stmt)


def user_for_token(db: Session, token: Optional[str]) -> Optional[dict]:
    """Dados do usuário da sessão (válida e com usuário ativo); renova a validade com o uso."""
    from models import User, UserSession

    if not token:
        return None
    now = datetime.now()
    row = db.execute(select(UserSession, User).join(User, User.id == UserSession.user_id)
                     .where(UserSession.token_hash == _token_hash(token))).first()
    if row is None:
        return None
    sess, user = row
    if sess.expires_at < now or not user.active:
        db.delete(sess)
        db.commit()
        return None
    if not sess.last_seen_at or now - sess.last_seen_at > timedelta(minutes=10):  # renova sem gravar a cada clique
        sess.last_seen_at = now
        sess.expires_at = now + timedelta(days=SESSION_DAYS)
        db.commit()
    return {"id": user.id, "username": user.username, "name": user.name or user.username, "is_admin": user.is_admin,
            "must_change": user.must_change_password}


def current_user() -> Optional[dict]:
    return _current.get()


def set_current(user: Optional[dict]):
    return _current.set(user)


def reset_current(token) -> None:
    _current.reset(token)
