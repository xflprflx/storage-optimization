"""Telas de importação (Produtos e Saldos) via CSV/XLSX: upload -> mapeamento -> processamento."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import SessionLocal
from models import Branch, Warehouse
from routers.common import f_bool, f_str, form_data, redirect, render, to_int
from services.file_reader import (
    auto_map,
    normalize_header,
    read_preview,
    remove_upload,
    resolve_column,
    save_upload,
    token_path,
)
from services.master_importer import MASTER_KINDS, MasterOptions
from services.product_importer import PRODUCT_FIELDS, ImportOptions, import_products
from services.stock_importer import QTY_SYNONYMS, STOCK_FIELDS, StockImportOptions, import_stock

router = APIRouter(prefix="/imports", tags=["Importações"])

KINDS = {
    "products": {"title": "Importação de Produtos", "fields": PRODUCT_FIELDS},
    "stock": {"title": "Importação de Saldos de Estoque", "fields": STOCK_FIELDS},
    **{k: {"title": v["title"], "fields": v["fields"]} for k, v in MASTER_KINDS.items()},
}


def _mapping_template(kind: str) -> str:
    return "imports/mapping_master.html" if kind in MASTER_KINDS else f"imports/mapping_{kind}.html"

SAMPLE_PRODUCTS = (
    "SKU;Descricao;Tipo de material;Altura;Largura;Comprimento;Peso;Estoque minimo;Estoque maximo;Filial;Armazem;Endereco\n"
    "PRD-001;Parafuso sextavado M8;Fixação;0,5;0,5;3;0,02;100;500;01;CD01;A-01-01-01\n"
    "PRD-002;Arruela lisa 8mm;Fixação;0,1;1,6;1,6;0,005;200;1000;01;CD01;A-01-01-02\n"
    "PRD-003;Porca M8;Fixação;0,6;1,3;1,3;0,01;;;02;CD02;\n"
)
SAMPLE_STOCK = (
    "Filial;Armazem;Posicao;SKU;Qtd Disponivel;Qtd Reservada;Qtd Avariada\n"
    "01;CD01;A-01-01-01;PRD-001;120;10;2\n"
    "01;CD01;A-01-01-02;PRD-002;300;0;0\n"
    "02;CD02;B-01-01-01-A;PRD-003;45,5;4,5;0\n"
)


def _lookups(db: Session) -> dict:
    return {
        "branches": list(db.scalars(select(Branch).order_by(Branch.code))),
        "warehouses": list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
    }


@router.get("")
def imports_home(request: Request):
    return render(request, "imports/index.html")


@router.get("/template/{kind}.csv")
def download_template(kind: str):
    if kind in MASTER_KINDS:
        content = MASTER_KINDS[kind]["sample"]
    else:
        content = SAMPLE_PRODUCTS if kind == "products" else SAMPLE_STOCK
    return Response(
        content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="modelo_{kind}.csv"'},
    )


# --------------------------------------------------------------------------- #
# Histórico de pedidos (saídas) de uma filial: envio -> confirmação da aba e das colunas -> resumo salvo
# --------------------------------------------------------------------------- #
def _safe_back(raw) -> str:
    back = str(raw or "").strip()
    return back if back.startswith("/") and not back.startswith("//") else "/imports/history"


def _history_mapping_page(request: Request, db: Session, token: str, filename: str, branch_id: int, to_abs: bool,
                          back: str, chosen: dict | None = None, error: str = "", status_code: int = 200):
    from services.file_reader import token_path
    from services.site_import import read_sheets
    from services.slotting import HISTORY_FIELDS, HISTORY_KINDS, detect_history, guess_history_cols

    sheets = read_sheets(token_path(token), limit=13)
    det = detect_history(sheets)
    info = {name: {"headers": rows[0] if rows else [], "rows": rows[1:13],
                   "guess": {k: guess_history_cols(k, rows[0] if rows else []) for k in HISTORY_FIELDS}}
            for name, rows in sheets.items()}
    choice = chosen or {"kind": det["kind"], "sheet": det["sheet"],
                        "cols": info.get(det["sheet"], {}).get("guess", {}).get(det["kind"], {})}
    return render(request, "imports/mapping_history.html", status_code=status_code, token=token, filename=filename,
                  branch=db.get(Branch, branch_id), to_abs=to_abs, back=back, sheets=info, choice=choice,
                  kinds=HISTORY_KINDS, fields={k: {f: lbl for f, (lbl, _r) in v.items()} for k, v in HISTORY_FIELDS.items()},
                  required={k: [f for f, (_l, r) in v.items() if r] for k, v in HISTORY_FIELDS.items()}, error=error)


@router.get("/history")
def history_form(request: Request, branch_id: str = "", back: str = ""):
    db = SessionLocal()
    try:
        from models import PickHistory
        from services.slotting import summary_text

        branches = list(db.scalars(select(Branch).order_by(Branch.code)))
        saved = {h.branch_id: (h.filename, summary_text(h.data), h.updated_at) for h in db.scalars(select(PickHistory))}
    finally:
        db.close()
    return render(request, "imports/history.html", branches=branches, branch_id=to_int(branch_id), saved=saved,
                  back=_safe_back(back) if back else "", error=None)


@router.post("/history/upload")
async def history_upload(request: Request):
    from services.file_reader import save_upload

    form = await request.form()
    back = _safe_back(form.get("back"))
    bid = to_int(form.get("branch_id"))
    upload = form.get("file")
    db = SessionLocal()
    try:
        if not bid or db.get(Branch, bid) is None:
            return redirect(back, "Escolha a filial do histórico.", "danger")
        if upload is None or not getattr(upload, "filename", ""):
            return redirect(back, "Escolha o arquivo do histórico.", "danger")
        try:
            token = await save_upload(upload)
        except ValueError as exc:
            return redirect(back, str(exc), "danger")
        try:
            return _history_mapping_page(request, db, token, str(upload.filename)[:200], bid,
                                         str(form.get("abs") or "") in {"1", "on", "true"}, back)
        except Exception as exc:  # noqa: BLE001  (arquivo corrompido etc.)
            remove_upload(token)
            return redirect(back, f"Não foi possível ler o arquivo: {exc}", "danger")
    finally:
        db.close()


@router.post("/history/confirm")
async def history_confirm(request: Request, form: FormData = Depends(form_data)):
    from models import PickHistory
    from services.slotting import HISTORY_FIELDS, read_history, summary_text

    back = _safe_back(form.get("back"))
    token = f_str(form, "token")
    bid = to_int(form.get("branch_id"))
    filename = f_str(form, "filename")
    to_abs = f_bool(form, "abs")
    try:
        path = token_path(token)
    except ValueError as exc:
        return redirect(back, str(exc), "danger")
    if form.get("action") == "cancel":
        remove_upload(token)
        return redirect(back, "Envio do histórico cancelado.", "secondary")
    kind = f_str(form, "kind") if f_str(form, "kind") in HISTORY_FIELDS else "orders"
    cols = {f: to_int(form.get(f"col_{f}")) if str(form.get(f"col_{f}") or "").isdigit() else None
            for f in HISTORY_FIELDS[kind]}
    mapping = {"kind": kind, "sheet": f_str(form, "sheet"), "cols": cols}
    db = SessionLocal()
    try:
        if not bid or db.get(Branch, bid) is None:
            return redirect(back, "Filial não encontrada.", "danger")
        try:
            data, err = await run_in_threadpool(read_history, path, to_abs, mapping)
        except Exception as exc:  # noqa: BLE001
            data, err = None, f"não foi possível ler o arquivo ({exc})."
        if err:
            return _history_mapping_page(request, db, token, filename, bid, to_abs, back, mapping,
                                         f"Histórico não importado: {err}", 422)
        remove_upload(token)
        h = db.scalar(select(PickHistory).where(PickHistory.branch_id == bid)) or PickHistory(branch_id=bid)
        h.filename, h.data = filename[:200], data
        db.add(h)
        db.commit()
        br = db.get(Branch, bid)
        neg = ""
        if data.get("neg"):
            neg = f" {data['neg']} quantidade(s) negativa(s) " + ("convertida(s) para valor absoluto." if data["abs"]
                                                                 else "ignorada(s) (sem a conversão).")
        return redirect(back, f"Histórico de saídas salvo para a filial {br.code}: {summary_text(data)} "
                              f"(aba {data.get('sheet')}).{neg}", "success")
    finally:
        db.close()


@router.get("/{kind}")
def upload_form(kind: str, request: Request):
    if kind not in KINDS:
        return render(request, "imports/index.html", status_code=404)
    return render(request, "imports/upload.html", kind=kind, title=KINDS[kind]["title"], error=None,
                  master=MASTER_KINDS.get(kind))


@router.post("/{kind}/upload")
async def upload_file(kind: str, request: Request, file: UploadFile = File(...), has_header: str = Form("")):
    if kind not in KINDS:
        return render(request, "imports/index.html", status_code=404)
    header = has_header.lower() in {"1", "on", "true"}
    try:
        token = await save_upload(file)
        headers, rows = read_preview(token_path(token), header)
    except Exception as exc:
        return render(request, "imports/upload.html", status_code=422, kind=kind, title=KINDS[kind]["title"],
                      error=f"Não foi possível ler o arquivo: {exc}", master=MASTER_KINDS.get(kind))

    fields = KINDS[kind]["fields"]
    suggested = auto_map(headers, {k: v[2] for k, v in fields.items()}) if header else {k: None for k in fields}
    qty_suggested: list[int] = []
    if kind == "stock" and header:
        used = {v for v in suggested.values() if v is not None}
        targets = [normalize_header(s) for s in QTY_SYNONYMS]
        for i, h in enumerate(headers):
            nh = normalize_header(h)
            if i not in used and any(nh.startswith(t) or t in nh for t in targets):
                qty_suggested.append(i)

    db = SessionLocal()
    try:
        lookups = _lookups(db)
    finally:
        db.close()
    return render(request, _mapping_template(kind), kind=kind, title=KINDS[kind]["title"], token=token,
                  filename=file.filename, has_header=header, headers=headers, rows=rows, fields=fields,
                  mapping=suggested, qty_columns=qty_suggested, errors=None, master=MASTER_KINDS.get(kind), **lookups)


@router.post("/{kind}/process")
async def process_file(kind: str, request: Request, form: FormData = Depends(form_data)):
    if kind not in KINDS:
        return render(request, "imports/index.html", status_code=404)
    token = f_str(form, "token")
    has_header = f_bool(form, "has_header")
    fields = KINDS[kind]["fields"]

    def _rerender(error: str):
        headers, rows = read_preview(token_path(token), has_header)
        db = SessionLocal()
        try:
            lookups = _lookups(db)
        finally:
            db.close()
        mapping = {k: to_int(form.get(f"map_{k}")) for k in fields}
        qty_cols = [i for i in (to_int(v) for v in form.getlist("qty_columns")) if i is not None]
        return render(request, _mapping_template(kind), status_code=422, kind=kind, title=KINDS[kind]["title"],
                      token=token, filename=f_str(form, "filename"), has_header=has_header, headers=headers, rows=rows,
                      fields=fields, mapping=mapping, qty_columns=qty_cols, errors=error, form_values=form,
                      master=MASTER_KINDS.get(kind), **lookups)

    try:
        path = token_path(token)
        headers, _ = read_preview(path, has_header, limit=1)
        mapping = {k: resolve_column(form.get(f"map_{k}"), headers) for k in fields}
        missing = [fields[k][0] for k, (_, required, _) in fields.items() if required and mapping.get(k) is None]
        if missing:
            return _rerender(f"Mapeie as colunas obrigatórias: {', '.join(missing)}.")
        used = [v for v in mapping.values() if v is not None]
        if len(used) != len(set(used)):
            return _rerender("A mesma coluna foi mapeada para mais de um campo.")
    except ValueError as exc:
        if "Token" in str(exc) or "expirado" in str(exc):
            return render(request, "imports/upload.html", status_code=422, kind=kind, title=KINDS[kind]["title"], error=str(exc))
        return _rerender(str(exc))

    if kind in MASTER_KINDS:
        mopts = MasterOptions(
            has_header=has_header,
            update_existing=f_bool(form, "update_existing"),
            default_branch_code=f_str(form, "default_branch_code"),
            default_warehouse_code=f_str(form, "default_warehouse_code"),
            replace_bins=f_bool(form, "replace_bins"),
            sync=f_bool(form, "sync"),
            drop_levels=f_bool(form, "drop_levels"),
        )
        runner = MASTER_KINDS[kind]["run"]

        def job():
            with SessionLocal() as db:
                return runner(db, path, mapping, mopts)

        try:
            result = await run_in_threadpool(job)
        except ValueError as exc:
            return _rerender(str(exc))
        remove_upload(token)
        return render(request, "imports/result_master.html", kind=kind, title=KINDS[kind]["title"], result=result,
                      filename=f_str(form, "filename"), master=MASTER_KINDS[kind])

    if kind == "products":
        opts = ImportOptions(
            has_header=has_header,
            default_branch_code=f_str(form, "default_branch_code"),
            default_warehouse_code=f_str(form, "default_warehouse_code"),
            update_existing=f_bool(form, "update_existing"),
            create_locations=f_bool(form, "create_locations"),
            update_default_location=f_bool(form, "update_default_location"),
            sync=f_bool(form, "sync"),
        )

        def job():
            with SessionLocal() as db:
                return import_products(db, path, mapping, opts)
    else:
        qty_cols = []
        for v in form.getlist("qty_columns"):
            try:
                idx = resolve_column(v, headers)
            except ValueError as exc:
                return _rerender(str(exc))
            if idx is not None:
                qty_cols.append(idx)
        qty_cols = sorted(set(qty_cols))
        if not qty_cols:
            return _rerender("Selecione ao menos uma coluna de quantidade (as selecionadas serão somadas).")
        overlap = set(qty_cols) & {v for v in mapping.values() if v is not None}
        if overlap:
            return _rerender("Colunas de quantidade não podem ser as mesmas de Filial/Armazém/Posição/SKU.")
        if mapping.get("branch_code") is None and not f_str(form, "default_branch_code"):
            return _rerender("Mapeie a coluna Filial ou selecione uma filial padrão.")
        opts = StockImportOptions(
            has_header=has_header,
            default_branch_code=f_str(form, "default_branch_code"),
            default_warehouse_code=f_str(form, "default_warehouse_code"),
            create_missing_products=f_bool(form, "create_missing_products"),
            duplicate_mode="last" if f_str(form, "duplicate_mode") == "last" else "sum",
            zero_missing=f_bool(form, "zero_missing"),
            allow_negative=f_bool(form, "allow_negative"),
        )

        def job():
            with SessionLocal() as db:
                return import_stock(db, path, mapping, qty_cols, opts)

    try:
        result = await run_in_threadpool(job)
    except ValueError as exc:
        return _rerender(str(exc))
    remove_upload(token)
    qty_labels = [headers[i] for i in qty_cols] if kind == "stock" else []
    return render(request, "imports/result.html", kind=kind, title=KINDS[kind]["title"], result=result,
                  filename=f_str(form, "filename"), qty_labels=qty_labels)
