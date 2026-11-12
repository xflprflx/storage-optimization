"""Capacidade dos níveis físicos ("em um nível cabem X bins do tipo Y") e níveis reservados."""
from __future__ import annotations

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from database import get_db
from models import (BinType, Branch, LevelCapacity, LocationBin, ShelfLevel, StorageLocation, StructureType, Warehouse,
                    branch_warehouse)
from routers.common import f_int, f_str, form_data, redirect, render, to_int
from services.addressing import normalize_code
from services.file_reader import parse_decimal
from services.level_service import (ensure_level, fit_structure, matching_levels, set_capacity, set_height,
                                   spill_message, spill_overflow, structure_message, usage_for)
from services.i18n import N_, _

router = APIRouter(prefix="/levels", tags=[N_("Capacidade dos níveis")])
PER_PAGE = 100


def _wid(value) -> int | None:
    return None if value in (None, "", "none") else to_int(value)


def _lookups(db: Session) -> dict:
    wh_branches: dict[int, list[int]] = {}
    for b, w in db.execute(select(branch_warehouse.c.branch_id, branch_warehouse.c.warehouse_id)).all():
        wh_branches.setdefault(w, []).append(b)
    return {
        "branches": list(db.scalars(select(Branch).order_by(Branch.code))),
        "warehouses": list(db.scalars(select(Warehouse).order_by(Warehouse.code))),
        "wh_branches": wh_branches,
        "bin_types": list(db.scalars(select(BinType).order_by(BinType.code))),
        "structures": list(db.scalars(select(StructureType).order_by(StructureType.code))),
    }


def _back(form_or_qp) -> str:
    keys = ("branch_id", "warehouse_id", "aisle", "bay", "level", "status", "page")
    q = {k: form_or_qp.get(f"f_{k}", form_or_qp.get(k, "")) for k in keys}
    q = {k: v for k, v in q.items() if v}
    return "/levels" + ("?" + urlencode(q) if q else "")


@router.get("")
def levels_page(request: Request, branch_id: str = "", warehouse_id: str = "", aisle: str = "", bay: str = "",
                level: str = "", status: str = "", page: int = 1, db: Session = Depends(get_db)):
    lk = _lookups(db)
    bid = to_int(branch_id)
    if not bid:
        first = db.execute(select(StorageLocation.branch_id, StorageLocation.warehouse_id)
                           .order_by(StorageLocation.branch_id).limit(1)).first()
        if first:
            bid, warehouse_id = first[0], (str(first[1]) if first[1] else "none")
    ctx = {"branch_id": bid, "warehouse_id": warehouse_id or "none", "aisle": aisle, "bay": bay, "level": level,
           "status": status, "rows": [], "total": 0, "page_no": 1, "pages": 1, "error": None, **lk}
    if not bid:
        return render(request, "levels/index.html", **ctx)
    wid = _wid(warehouse_id)
    try:
        keys = matching_levels(db, bid, wid, aisle, bay, level)
    except ValueError as exc:
        ctx["error"] = str(exc)
        return render(request, "levels/index.html", **ctx)

    usage_all = usage_for(db, keys) if status else None
    if status:
        def keep(k):
            u = usage_all[k]
            return {"over": u.over, "nocap": not u.has_capacity, "extra": bool(u.borrowed) or u.lent > 0,
                    "full": (u.fraction or 0) >= 0.999, "noheight": u.height is None, "free": u.fraction is not None and u.fraction < 0.999}.get(status, True)
        keys = [k for k in keys if keep(k)]
    total = len(keys)
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(1, page), pages)
    page_keys = keys[(page - 1) * PER_PAGE : page * PER_PAGE]
    levels = {k: ensure_level(db, k) for k in page_keys}
    db.commit()
    usage = usage_for(db, page_keys)
    loc_counts = {}
    for a, b_, lv, n in db.execute(
        select(StorageLocation.aisle, StorageLocation.bay, StorageLocation.level, func.count())
        .where(StorageLocation.branch_id == bid,
               StorageLocation.warehouse_id.is_(None) if wid is None else StorageLocation.warehouse_id == wid)
        .group_by(StorageLocation.aisle, StorageLocation.bay, StorageLocation.level)
    ):
        loc_counts[(a, b_, lv)] = n
    bt_h = {bt.id: (bt.code, float(bt.height)) for bt in lk["bin_types"] if bt.height is not None}
    from services.structure_service import bay_structures

    bay_st = bay_structures(db, bid, wid)
    rows = []
    for k in page_keys:
        sl = levels[k]
        u = usage[k]
        tallest = max((bt_h[t][1] for t, n in u.used.items() if n and t in bt_h), default=None)
        rows.append({"key": k, "code": "-".join(k[2:]), "level": sl, "usage": u, "tallest": tallest,
                     "locations": loc_counts.get(k[2:], 0), "struct": bay_st.get(k[2:4])})
    ctx.update(rows=rows, total=total, page_no=page, pages=pages)
    return render(request, "levels/index.html", **ctx)


@router.post("/save")
def save_capacities(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Edição em tabela: campos cap_<nível>_<tipo> (vazio/0 = sem capacidade para o tipo)."""
    changed = 0
    errors = []
    for name, raw in form.multi_items():
        if not name.startswith("cap_"):
            continue
        try:
            _x, lid, tid = name.split("_")
            lid, tid = int(lid), int(tid)
        except ValueError:
            continue
        raw = str(raw).strip()
        val = to_int(raw) if raw else 0
        if raw and (val is None or val < 0 or val > 100_000):
            errors.append(_("Valor inválido: {raw}", raw=f"{raw!r}"))
            continue
        cap = db.scalar(select(LevelCapacity).where(LevelCapacity.shelf_level_id == lid, LevelCapacity.bin_type_id == tid))
        if val:
            if cap is None:
                if db.get(ShelfLevel, lid) and db.get(BinType, tid):
                    db.add(LevelCapacity(shelf_level_id=lid, bin_type_id=tid, max_bins=val))
                    changed += 1
            elif cap.max_bins != val:
                cap.max_bins = val
                changed += 1
        elif cap is not None:
            db.delete(cap)
            changed += 1
    for name, raw in form.multi_items():
        if name.startswith("height_"):
            sl = db.get(ShelfLevel, to_int(name[7:]))
            if not sl:
                continue
            try:
                val = parse_decimal(raw)
            except ValueError:
                errors.append(_("Altura inválida: {raw}", raw=f"{raw!r}"))
                continue
            if val is not None and (val <= 0 or val > 5000):
                errors.append(_("Altura fora do intervalo (0–5000 cm): {raw}", raw=f"{raw!r}"))
                continue
            if sl.height_cm != val:
                sl.height_cm = val
                changed += 1
        if name.startswith("notes_"):
            sl = db.get(ShelfLevel, to_int(name[6:]))
            if sl and sl.notes != str(raw).strip()[:200]:
                sl.notes = str(raw).strip()[:200]
    db.flush()
    adj = (spill_message(db, spill_overflow(db)) + " " + structure_message(fit_structure(db))).strip()
    db.commit()
    if errors:
        return redirect(_back(form), _("{changed} alteração(ões) salva(s). {adj} Ignorados: ", changed=changed, adj=adj) + "; ".join(errors[:5]), "warning")
    return redirect(_back(form), _("{changed} alteração(ões) salva(s). {adj}", changed=changed, adj=adj).strip(), "info" if adj else "success")


@router.post("/bulk")
def bulk_capacity(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    bid = f_int(form, "f_branch_id")
    wid = _wid(form.get("f_warehouse_id"))
    tid = f_int(form, "bin_type_id")
    raw = f_str(form, "max_bins")
    max_bins = to_int(raw) if raw else 0
    raw_h = f_str(form, "height")
    clear_h = f_str(form, "clear_height") == "1"
    try:
        height = parse_decimal(raw_h)
    except ValueError:
        return redirect(_back(form), _("Altura do vão inválida."), "danger")
    if height is not None and (height <= 0 or height > 5000):
        return redirect(_back(form), _("Altura do vão deve estar entre 0 e 5000 cm."), "danger")
    if not bid:
        return redirect(_back(form), _("Selecione a filial."), "danger")
    if tid and not db.get(BinType, tid):
        return redirect(_back(form), _("Tipo de bin inválido."), "danger")
    if not tid and height is None and not clear_h:
        return redirect(_back(form), _("Informe um tipo de bin com a quantidade e/ou a altura do vão."), "warning")
    if raw and (max_bins is None or max_bins < 0):
        return redirect(_back(form), _("Quantidade de bins inválida."), "danger")
    try:
        keys = matching_levels(db, bid, wid, f_str(form, "b_aisle"), f_str(form, "b_bay"), f_str(form, "b_level"))
    except ValueError as exc:
        return redirect(_back(form), str(exc), "danger")
    if not keys:
        return redirect(_back(form), _("Nenhum nível corresponde às condições."), "warning")
    msgs = []
    if tid:
        n = set_capacity(db, keys, tid, max_bins or None)
        code = db.get(BinType, tid).code
        msgs.append(_("Capacidade de {max_bins}× {code} definida em {n} nível(is).", max_bins=max_bins, code=code, n=n) if max_bins
                    else _("Capacidade de {code} removida de {n} nível(is).", code=code, n=n))
    if height is not None or clear_h:
        n = set_height(db, keys, height)
        msgs.append(_("Altura do vão de {height} cm definida em {n} nível(is).", height=f"{height:g}", n=n) if height is not None
                    else _("Altura do vão removida de {n} nível(is).", n=n))
        adj = structure_message(fit_structure(db, keys))
        if adj:
            msgs.append(adj)
    if tid:
        db.flush()
        spilled = spill_message(db, spill_overflow(db, {k[:4] for k in keys}))
        if spilled:
            msgs.append(spilled)
    db.commit()
    return redirect(_back(form), " ".join(msgs), "info" if len(msgs) > 1 else "success")


@router.post("/reserved")
def add_reserved(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Cria um nível físico sem endereços (reservado para bins de outros níveis da estante)."""
    bid = f_int(form, "f_branch_id")
    wid = _wid(form.get("f_warehouse_id"))
    aisle, bay, level = (normalize_code(f_str(form, k)) for k in ("r_aisle", "r_bay", "r_level"))
    if not bid or not aisle or not bay or not level:
        return redirect(_back(form), _("Informe corredor, estante e nível."), "danger")
    if any(len(v) > 20 for v in (aisle, bay, level)):
        return redirect(_back(form), _("Códigos com no máximo 20 caracteres."), "danger")
    has_bay = db.scalar(select(func.count()).select_from(StorageLocation).where(
        StorageLocation.branch_id == bid, StorageLocation.aisle == aisle, StorageLocation.bay == bay,
        StorageLocation.warehouse_id.is_(None) if wid is None else StorageLocation.warehouse_id == wid))
    sl = ensure_level(db, (bid, wid, aisle, bay, level))
    sl.notes = f_str(form, "r_notes", 200) or sl.notes
    db.commit()
    msg = _("Nível {aisle}-{bay}-{level} cadastrado.", aisle=aisle, bay=bay, level=level)
    if not has_bay:
        return redirect(_back(form), msg + _(" Atenção: ainda não há endereços na estante {aisle}-{bay}.", aisle=aisle, bay=bay), "warning")
    return redirect(_back(form), msg)


@router.post("/{level_id}/delete")
def delete_level(level_id: int, form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    sl = db.get(ShelfLevel, level_id)
    if not sl:
        return redirect(_back(form), _("Nível não encontrado."), "danger")
    n_bins = db.scalar(select(func.count()).select_from(LocationBin).where(LocationBin.shelf_level_id == level_id))
    if n_bins:
        return redirect(_back(form), _("O nível {code} guarda bins de outros endereços ({n_bins} linha(s)). Mova-as antes de excluir.", code=sl.code, n_bins=n_bins), "danger")
    n_locs = db.scalar(select(func.count()).select_from(StorageLocation).where(
        StorageLocation.branch_id == sl.branch_id, StorageLocation.aisle == sl.aisle, StorageLocation.bay == sl.bay,
        StorageLocation.level == sl.level,
        StorageLocation.warehouse_id.is_(None) if sl.warehouse_id is None else StorageLocation.warehouse_id == sl.warehouse_id))
    code = sl.code
    db.delete(sl)
    db.commit()
    if n_locs:
        return redirect(_back(form), _("Capacidades do nível {code} removidas (o nível continua existindo pelos seus endereços).", code=code))
    return redirect(_back(form), _("Nível reservado {code} excluído.", code=code))


@router.post("/structure")
def apply_structure_bulk(form: FormData = Depends(form_data), db: Session = Depends(get_db)):
    """Aplica (ou remove) uma estrutura de armazenagem às estantes que atendem às condições."""
    from services.structure_service import apply_structure, matching_bays, remove_structure

    bid = f_int(form, "f_branch_id")
    wid = _wid(form.get("f_warehouse_id"))
    if not bid:
        return redirect(_back(form), _("Selecione a filial."), "danger")
    try:
        bays = matching_bays(db, bid, wid, f_str(form, "s_aisle"), f_str(form, "s_bay"))
    except ValueError as exc:
        return redirect(_back(form), str(exc), "danger")
    if not bays:
        return redirect(_back(form), _("Nenhuma estante corresponde às condições."), "warning")
    sid = form.get("structure_type_id") or ""
    if sid == "remove":
        n = remove_structure(db, bays)
        db.commit()
        return redirect(_back(form), _("Estrutura removida de {n} estante(s). Vão e capacidades dos níveis foram mantidos.", n=n))
    st = db.get(StructureType, int(sid)) if str(sid).isdigit() else None
    if st is None:
        return redirect(_back(form), _("Escolha a estrutura de armazenagem."), "danger")
    on = lambda k: str(form.get(k) or "").lower() in {"1", "on", "true"}  # noqa: E731
    try:
        gap = float(parse_decimal(form.get("top_gap") or "5") or 0)
    except ValueError:
        return redirect(_back(form), _("Folga acima da bin inválida."), "danger")
    try:
        fl = parse_decimal(form.get("first_level") or "")
    except ValueError:
        return redirect(_back(form), _("Altura do 1º nível inválida."), "danger")
    if fl is not None and fl < 0:
        return redirect(_back(form), _("Altura do 1º nível não pode ser negativa."), "danger")
    stats = apply_structure(db, bays, st, create_levels=on("create_levels"), set_heights=on("set_heights"),
                            set_caps=on("set_caps"), replace_caps=on("replace_caps"), top_gap_cm=gap,
                            first_level_cm=fl)
    msgs = [_("Estrutura {code} aplicada a {bays} estante(s) ({levels} níveis", code=st.code, bays=stats['bays'], levels=stats['levels'])
            + (f", {stats['levels_created']} criados" if stats["levels_created"] else "") + ")"
            + (_(", 1º nível a {fl} cm do piso nessas estantes", fl=f"{fl:g}") if fl is not None else "") + "."]
    if on("set_caps"):
        msgs.append(_("Capacidades calculadas pela frente × profundidade do módulo e pela altura das bins."))
    if fl is not None and float(fl) + sum(st.vaos()) > float(st.height) + 1e-6:
        msgs.append(_("Atenção: 1º nível a {fl} cm + vãos ({sum} cm) passam da altura da estrutura ({float} cm).", fl=f"{fl:g}", sum=f"{sum(st.vaos()):g}", float=f"{float(st.height):g}"))
    if stats["extra_levels"]:
        msgs.append(_("Atenção: {extra_levels} nível(is) além dos {levels} da estrutura.", extra_levels=stats['extra_levels'], levels=st.levels))
    adj = structure_message(stats["fit"])
    if adj:
        msgs.append(adj)
    spilled = spill_message(db, stats["spill"])
    if spilled:
        msgs.append(spilled)
    db.commit()
    return redirect(_back(form), " ".join(msgs), "warning" if stats["extra_levels"] else "success")
