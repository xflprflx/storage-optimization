"""Aparência 3D no cadastro de bins e estruturas: prévia (peças resolvidas pelo servidor) e o prompt para a IA."""
from __future__ import annotations

from fastapi import APIRouter, Request

from services.i18n import N_

router = APIRouter(prefix="/appearance", tags=[N_("Aparência 3D")])


@router.post("/preview")
async def preview(request: Request):
    """{kind: "bin"|"struct", model, code, w, h, d, name, type} -> {look (None = desenho simples), error, model}."""
    from services.appearance import CUSTOM, look_of, model_choices, parse, suggest

    body = await request.json()
    kind = "struct" if body.get("kind") == "struct" else "bin"
    model, code = str(body.get("model") or ""), str(body.get("code") or "").strip()
    if model not in {k for k, _l in model_choices(kind)}:
        model = ""
    if code and model == "":
        model = CUSTOM
    try:
        dims = tuple(float(str(body.get(k) or 0).replace(",", ".")) for k in ("w", "h", "d"))
    except ValueError:
        dims = (0.0, 0.0, 0.0)
    texts = (str(body.get("type") or ""), str(body.get("name") or "")) if kind == "struct" else (str(body.get("name") or ""), "")
    appearance, error = (parse(code) if model == CUSTOM else (None, ""))
    look = None if error else look_of(kind, model, appearance, dims, *texts)
    return {"look": look, "error": error, "model": model,
            "auto": suggest(kind, *texts) if model == "" else None}
