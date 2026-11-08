"""
Neokioski - Sistema de Gestão de Estoque
Entrypoint FastAPI (sem autenticação nesta versão).

Execução:  uvicorn main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # pragma: no cover - checagem para quem roda sem o start.bat/start.sh
    raise SystemExit(f"Neokioski requer Python 3.11 ou superior (este é o {sys.version.split()[0]}). "
                     "Use start.bat (Windows) ou start.sh (Linux/macOS), que escolhem um Python compatível.")

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import init_db
from routers import (
    api,
    bin_types,
    branches,
    bulk,
    dashboard,
    imports,
    layout,
    levels,
    locations,
    optimization,
    packing,
    products,
    runs,
    site,
    stock,
    structures,
    warehouses,
)
from routers.common import render
from services.file_reader import cleanup_old_uploads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("neokioski")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    cleanup_old_uploads()
    log.info("Neokioski iniciado - banco de dados pronto.")
    yield


app = FastAPI(title="Neokioski", version="1.0.0", lifespan=lifespan)

for r in (dashboard, layout, runs, site, optimization, packing, branches, warehouses, locations, levels, bin_types, structures, products, stock, imports, bulk, api):
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
