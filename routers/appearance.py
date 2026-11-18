"""Aparência 3D no cadastro de bins e estruturas: prévia (peças resolvidas pelo servidor) e o prompt para a IA."""
from __future__ import annotations

from fastapi import APIRouter, Request

from services.i18n import N_

router = APIRouter(prefix="/appearance", tags=[N_("Aparência 3D")])


@router.post("/preview")
async def preview(request: Request):
    """{kind: "bin"|"struct", model, code, w, h, d, name, type} -> {look (None = desenho simples), error, notes (o que
    foi consertado no código colado), model, auto}."""
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
    appearance, error, notes = (parse(code, dims, structure=kind == "struct") if model == CUSTOM else (None, "", []))
    look = None if error else look_of(kind, model, appearance, dims, *texts)
    found = {}
    if kind == "struct":  # cores de cada peça: as da composição (formulário) e as que a IA identificou no código
        from services.appearance import apply_piece_colors, piece_colors

        found = piece_colors(appearance) if appearance else {}
        comp = [(str(r.get("code") or ""), "level" if r.get("per") == "level" else "module",
                 (str(r.get("color") or "").lower() or None) or found.get(str(r.get("code") or "").upper()))
                for r in body.get("pieces") or [] if isinstance(r, dict)]
        look = apply_piece_colors(look, comp)
    return {"look": look, "error": error, "notes": notes, "model": model, "piece_colors": found,
            "auto": suggest(kind, *texts) if model == "" else None}
