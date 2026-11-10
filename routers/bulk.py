"""Exclusão em massa dos cadastros: prévia do impacto (relacionados) -> confirmação -> exclusão."""
from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from routers.common import f_bool, form_data, redirect, render, to_int
from services.delete_service import KINDS, filter_ids, impact, run

router = APIRouter(prefix="/bulk-delete", tags=["Exclusão em massa"])


def _back(kind: str, raw) -> str:
    base = KINDS[kind]["back"]
    back = str(raw or "")
    return back if back.startswith(base) else base


def _ids(db: Session, kind: str, form: FormData) -> list[int]:
    if form.get("all") == "1":
        got = filter_ids(db, kind, dict(parse_qsl(str(form.get("qs") or ""))))
        if got is not None:
            return got
    return sorted({i for i in (to_int(v) for v in form.getlist("ids")) if i})


@router.post("/{kind}/preview")
def bulk_preview(kind: str, request: Request, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    if kind not in KINDS:
        return redirect("/", "Tipo de cadastro desconhecido.", "danger")
    back = _back(kind, form.get("back"))
    ids = _ids(db, kind, form)
    if not ids:
        return redirect(back, "Marque os itens a excluir.", "warning")
    imp = impact(db, kind, ids)
    # "todos do filtro": a confirmação reenvia o filtro (não um campo por item), e a exclusão o resolve de novo
    by_filter = form.get("all") == "1" and filter_ids(db, kind, {}) is not None
    return render(request, "bulk/confirm.html", kind=kind, cfg=KINDS[kind], ids=list(imp["items"]), imp=imp, back=back,
                  shown=list(imp["items"].items())[:300], all_qs=str(form.get("qs") or "") if by_filter else None)


@router.post("/{kind}/run")
def bulk_run(kind: str, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    if kind not in KINDS:
        return redirect("/", "Tipo de cadastro desconhecido.", "danger")
    back = _back(kind, form.get("back"))
    ids = _ids(db, kind, form)
    if not ids:
        return redirect(back, "Nada a excluir.", "warning")
    cascade = f_bool(form, "cascade")
    try:
        done, skipped = run(db, kind, ids, cascade)
        db.commit()
    except Exception as exc:  # noqa: BLE001  (restrição do banco não prevista: nada é gravado)
        db.rollback()
        return redirect(back, f"Exclusão não feita: {exc}", "danger")
    label = KINDS[kind]["label"]
    msg = f"{done} {label} excluído(s)" + (" com os registros relacionados." if cascade and done else ".")
    if skipped:
        msg += f" {skipped} ignorado(s) por terem registros relacionados (marque \"excluir também os relacionados\")."
    return redirect(back, msg, "warning" if skipped else "success")
