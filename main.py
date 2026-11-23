"""
Slotta - gestão e otimização de armazenagem
Entrypoint FastAPI (sem autenticação nesta versão).

Execução:  uvicorn main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # pragma: no cover - checagem para quem roda sem o start.bat/start.sh
    raise SystemExit(f"Slotta requer Python 3.11 ou superior (este é o {sys.version.split()[0]}). "
                     "Use start.bat (Windows) ou start.sh (Linux/macOS), que escolhem um Python compatível.")

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from starlette.exceptions import HTTPException as StarletteHTTPException

from database import init_db
from services import i18n
from routers import (
    api,
    appearance,
    auth as auth_routes,
    bin_types,
    branches,
    bulk,
    dashboard,
    imports,
    layout,
    levels,
    lock_reasons,
    packaging_models,
    product_packagings,
    packagings_bulk,
    insights,
    occupancy,
    locations,
    onboarding,
    optimization,
    packing,
    products,
    runs,
    settings,
    simulator,
    site,
    stock,
    structure_kinds,
    structures,
    users,
    warehouses,
)
from routers.common import render
from services.file_reader import cleanup_old_uploads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("slotta")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    cleanup_old_uploads()
    from database import SessionLocal as _SL
    from services.auth import ensure_admin

    with _SL() as db:  # primeiro uso: cria o administrador
        ensure_admin(db)
    try:  # projetos de exemplo antigos: pedidos com a data (simulação de um dia inteiro)
        from database import SessionLocal
        from services.site_examples import refresh_example_orders

        with SessionLocal() as db:
            names = refresh_example_orders(db)
        if names:
            log.info("Pedidos reimportados com data nos projetos de exemplo: %s", ", ".join(names))
    except Exception:  # noqa: BLE001 — não impede o app de subir
        log.exception("Falha ao reimportar os pedidos dos projetos de exemplo")
    log.info("Slotta iniciado - banco de dados pronto.")
    yield


app = FastAPI(title="Slotta", version="1.0.0", lifespan=lifespan)


class LanguageMiddleware:
    """Idioma (cookie nk_lang ou idioma do navegador) e unidades (cookie nk_units) de cada requisição."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        from http.cookies import SimpleCookie

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get("cookie", ""))
        except Exception:  # cookie malformado: ignora
            pass
        lang = cookies["nk_lang"].value if "nk_lang" in cookies else None
        t1 = i18n.set_lang(i18n.normalize_lang(lang) or i18n.lang_from_accept(headers.get("accept-language")))
        t2 = i18n.set_units(cookies["nk_units"].value if "nk_units" in cookies else "auto")
        try:
            if i18n.is_imperial():
                return await self.inner(scope, receive, _imperial_html(send))
            return await self.inner(scope, receive, send)
        finally:
            i18n._LANG.reset(t1)
            i18n._UNITS.reset(t2)


def _imperial_html(send):
    """No sistema imperial, as páginas HTML passam por i18n.units_in_html (medidas dos textos em in/ft/lb)."""
    state = {"start": None, "body": []}

    async def wrapped(message):
        if message["type"] == "http.response.start":
            ctype = dict(message.get("headers") or []).get(b"content-type", b"")
            if ctype.startswith(b"text/html"):
                state["start"] = message
                return
            return await send(message)
        if message["type"] == "http.response.body" and state["start"] is not None:
            state["body"].append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = i18n.units_in_html(b"".join(state["body"]).decode("utf-8")).encode("utf-8")
            start = state["start"]
            start["headers"] = [(k, v) for k, v in start.get("headers") or [] if k.lower() != b"content-length"]
            start["headers"].append((b"content-length", str(len(body)).encode()))
            state["start"] = None
            await send(start)
            return await send({"type": "http.response.body", "body": body})
        return await send(message)

    return wrapped


class AuthMiddleware:
    """Exige login em todas as telas, menos as públicas (login, arquivos estáticos, idioma). Sem sessão: telas vão
    para /login (e voltam depois); JSON e API recebem 401. Senha provisória: só "Minha conta" até trocá-la."""

    PUBLIC = ("/login", "/static/", "/i18n/", "/lang/", "/units/", "/favicon.ico")
    FIRST_ALLOWED = ("/account", "/logout")

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        path = scope.get("path") or "/"
        if path.startswith(self.PUBLIC):
            return await self.inner(scope, receive, send)
        from http.cookies import SimpleCookie

        from anyio import to_thread
        from starlette.responses import JSONResponse

        from database import SessionLocal
        from services import auth

        cookies = SimpleCookie()
        try:
            cookies.load(dict(scope.get("headers") or []).get(b"cookie", b"").decode("latin-1"))
        except Exception:  # cookie malformado: sem sessão
            pass
        token = cookies[auth.COOKIE].value if auth.COOKIE in cookies else None

        def lookup():
            with SessionLocal() as db:
                return auth.user_for_token(db, token)

        user = await to_thread.run_sync(lookup) if token else None
        if user is None:
            if path.startswith("/api") or path.endswith(".json"):
                return await JSONResponse({"detail": "login required"}, status_code=401)(scope, receive, send)
            from urllib.parse import quote

            qs = scope.get("query_string", b"").decode("latin-1")
            nxt = path + ("?" + qs if qs else "")
            target = "/login" + ("" if nxt == "/" else "?next=" + quote(nxt, safe=""))
            return await RedirectResponse(target, status_code=303)(scope, receive, send)
        if user["must_change"] and not path.startswith(self.FIRST_ALLOWED):
            return await RedirectResponse("/account?first=1", status_code=303)(scope, receive, send)
        t = auth.set_current(user)
        try:
            return await self.inner(scope, receive, send)
        finally:
            auth.reset_current(t)


app.add_middleware(LanguageMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.add_middleware(AuthMiddleware)


@app.get("/lang/{code}", include_in_schema=False)
def choose_language(code: str, next: str = "/"):
    """Seletor de idioma: grava o cookie e volta para a página."""
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    if i18n.normalize_lang(code):
        resp.set_cookie("nk_lang", i18n.normalize_lang(code), max_age=5 * 365 * 86400, samesite="lax")
    return resp


@app.get("/units/{mode}", include_in_schema=False)
def choose_units(mode: str, next: str = "/"):
    """Seletor de unidades (auto, métrico, imperial): grava o cookie e volta para a página."""
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    if mode in i18n.UNIT_MODES:
        resp.set_cookie("nk_units", mode, max_age=5 * 365 * 86400, samesite="lax")
    return resp


@app.get("/i18n/{lang}.js", include_in_schema=False)
def i18n_script(lang: str):
    """Catálogo de traduções para o JavaScript das telas (função _t)."""
    import json

    from fastapi.responses import Response

    code = i18n.normalize_lang(lang) or i18n.DEFAULT_LANG
    body = "window.NK_T=" + json.dumps(i18n.catalog(code), ensure_ascii=False, separators=(",", ":")) + ";"
    return Response(body, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=86400"})
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Ícone do Slotta para os navegadores que pedem /favicon.ico."""
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).resolve().parent / "static" / "brand" / "slotta-icon-180.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@app.get("/help/fields.json", include_in_schema=False)
def field_help_catalog():
    """Catálogo da ajuda nos campos (título, explicação e desenho de cada campo de cada tela)."""
    from fastapi.responses import JSONResponse

    from services.field_help import RULES

    rules = [{**r, "t": i18n.lookup(r["t"]), "d": i18n.units_in_text(i18n.lookup(r["d"]))} for r in RULES]
    return JSONResponse(rules, headers={"Cache-Control": "public, max-age=3600"})

for r in (auth_routes, appearance, users, dashboard, onboarding, insights, occupancy, layout, runs, site, optimization, packing, branches, warehouses, locations, levels, bin_types, structure_kinds, structures, lock_reasons, packaging_models, packagings_bulk, product_packagings, products, stock, imports, bulk, settings, simulator, api):
    app.include_router(r.router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api"):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("Erro não tratado em %s", request.url.path)
    try:
        return render(request, "error.html", status_code=500, code=500, detail=str(exc))
    except Exception:  # pragma: no cover
        return HTMLResponse("<h1>Erro interno</h1>", status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000)
