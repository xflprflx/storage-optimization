"""Pool de bins com a família bin pai + compartimentos fracionáveis (as bins ficam no lugar; só mudam de forma).

Uma bin pai dividida em k compartimentos é uma caixa só: os compartimentos de um tipo, no mesmo endereço e nível,
formam grupos de k (cada grupo é uma bin pai; o último pode estar incompleto). Sobre o resultado do Pool:

1. falta bin para algum SKU: fraciona bins pai que guardam pouco (o conteúdo cabe num compartimento) — os outros
   compartimentos ficam para quem faltou — ou junta grupos de compartimentos vazios numa bin pai inteira, quando o SKU
   que faltou só cabe na bin pai; e roda de novo (até 3 vezes, se a falta diminuir);
2. consolida: tenta esvaziar os grupos com menos compartimentos usados, levando o conteúdo para compartimentos vazios
   de grupos já usados — só se a caminhada e a zona de ouro da pega não pioram (posição pela saída) e o SKU continua
   num endereço só (quando não pode dividir entre endereços); as irmãs ficam no mesmo pai;
3. junta: cada grupo completo de compartimentos que fica vazio no plano volta a ser uma bin pai inteira (tira-se a
   divisória).

As operações ficam em `PoolResult.reshape`: [{"kind": "split"|"join", "location_id", "address", "level", "parent",
"child", "k", "removed": [PoolBin], "created": [PoolBin]}]. Ao aplicar, as bins do endereço mudam (LocationBin) e a
numeração das bins é refeita (`renumber`).
"""
from __future__ import annotations

import dataclasses
import math
from typing import Callable, Optional

from services.i18n import _

EPS = 1e-9


def groups_of(bins: list, splits: dict) -> tuple[dict, dict]:
    """Compartimentos fracionáveis agrupados por bin pai: ({id da bin: grupo}, {grupo: [bins]}).
    Grupo = (endereço, nível, tipo, nº do grupo)."""
    by: dict[tuple, list] = {}
    for b in bins:
        if b.type_id in splits and b.location_id is not None:
            by.setdefault((b.location_id, b.level, b.type_id), []).append(b)
    gid, groups = {}, {}
    for (lid, lv, t), lst in by.items():
        k = splits[t][1]
        for i, b in enumerate(sorted(lst, key=lambda b: b.seq)):
            g = (lid, lv, t, i // k)
            gid[b.id] = g
            groups.setdefault(g, []).append(b)
    return gid, groups


def _fresh(bins: list) -> list:
    """Cópias das bins sem o plano (para rodar de novo)."""
    return [dataclasses.replace(b, contents={}, used=0.0) for b in bins]


def _new_id(bins: list) -> Callable[[], int]:
    nxt = [max([b.id for b in bins] + [0]) + 1]

    def f():
        nxt[0] += 1
        return nxt[0] - 1
    return f


def _seq_after(bins: list, lid: int, t: int) -> int:
    return max([b.seq for b in bins if b.location_id == lid and b.type_id == t] + [0])


def _usable(b, reserved: set) -> bool:
    return not b.locked and b.location_id not in reserved and b.location_id is not None


def renumber(bins: list) -> None:
    """Numera as bins de cada endereço e tipo como o cadastro numera as bins físicas (nível do endereço primeiro, depois
    os outros níveis; dentro do nível, na ordem atual)."""
    from services.layout_service import natural_key

    by: dict[tuple, list] = {}
    for b in bins:
        if b.location_id is not None:
            by.setdefault((b.location_id, b.type_id), []).append(b)
    for lst in by.values():
        lst.sort(key=lambda b: (b.level != "", natural_key(b.level) if b.level else (0,), b.seq, b.id))
        for i, b in enumerate(lst, 1):
            b.seq = i


def _split_op(bins, b, t, k, nid, types) -> dict:
    """Fraciona a bin pai `b` em k compartimentos `t` (o conteúdo dela, se houver, vai para o 1º)."""
    s0 = _seq_after(bins, b.location_id, t)
    created = [dataclasses.replace(b, id=nid(), type_id=t, seq=s0 + i + 1, contents={}, used=0.0, orig={},
                                   type_code=types[t].code if t in types else str(t)) for i in range(k)]
    created[0].orig = dict(b.orig)
    return {"kind": "split", "location_id": b.location_id, "address": b.address, "level": b.level, "parent": b.type_id,
            "child": t, "k": k, "removed": [b], "created": created}


def _join_op(bins, grp, p, nid, types) -> dict:
    b0 = grp[0]
    s0 = _seq_after(bins, b0.location_id, p)
    parent = dataclasses.replace(b0, id=nid(), type_id=p, seq=s0 + 1, contents={}, used=0.0, orig={},
                                 type_code=types[p].code if p in types else str(p))
    return {"kind": "join", "location_id": b0.location_id, "address": b0.address, "level": b0.level, "parent": p,
            "child": b0.type_id, "k": len(grp), "removed": list(grp), "created": [parent]}


def apply_ops(bins: list, ops: list) -> list:
    gone = {b.id for op in ops for b in op["removed"]}
    return [b for b in bins if b.id not in gone] + [c for op in ops for c in op["created"]]


def _shortage_ops(res, bins, demands, types, prm, reserved, splits) -> list:
    """Operações para quem ficou sem bin: fracionar bins pai que guardam pouco (cabe num compartimento) ou juntar
    grupos de compartimentos vazios, quando o SKU só cabe na bin pai."""
    from services.bin_optimizer import capacity

    base = prm.as_params()
    full = dataclasses.replace(base, fill_limit=1.0)
    by_sku = {d.sku: d for d in demands}
    kids: dict[int, list] = {}
    for t, (p, k) in splits.items():
        kids.setdefault(p, []).append((t, k))
    nid = _new_id(bins)
    ops, used_ids = [], set()
    _gid, groups = groups_of(bins, splits)
    for plan in [p for p in res.plans if p.shortage and p.sku in by_sku]:
        unit = by_sku[plan.sku].unit
        need = plan.shortage
        cap = {t: capacity(unit, types[t], base) if t in types and (prm.allowed_types is None or t in prm.allowed_types)
               else 0 for t in types}
        # 1) cabe num compartimento: fraciona bins pai cujo conteúdo do plano caiba num compartimento
        for t, (p, k) in sorted(splits.items(), key=lambda kv: -cap.get(kv[0], 0)):
            if need <= 0 or not cap.get(t):
                continue
            slots = math.ceil(need / cap[t])
            cands = []
            for b in bins:
                if b.type_id != p or b.id in used_ids or not _usable(b, reserved):
                    continue
                if len(b.contents) > 1:
                    continue
                if b.contents:
                    (sku, q), = b.contents.items()
                    d = by_sku.get(sku)
                    if d is None or capacity(d.unit, types[t], base) < q - EPS:
                        continue
                if b.orig and any(s not in by_sku for s in b.orig):
                    continue  # guarda produto que não entra no Pool
                if b.orig and (len(b.orig) > 1 or capacity(by_sku[next(iter(b.orig))].unit, types[t], full)
                               < next(iter(b.orig.values())) - EPS):
                    continue  # o que está hoje na bin pai precisa caber num compartimento (vai para o 1º)
                cands.append(b)
            cands.sort(key=lambda b: (bool(b.contents), b.address, b.id))  # bins pai livres primeiro
            for b in cands:
                if slots <= 0:
                    break
                ops.append(_split_op(bins, b, t, k, nid, types))
                used_ids.add(b.id)
                slots -= k - (1 if b.contents else 0)
                need -= (k - (1 if b.contents else 0)) * cap[t]
        # 2) só cabe na bin pai: junta grupos completos de compartimentos vazios no plano
        for p, kl in kids.items():
            if need <= 0 or not cap.get(p):
                continue
            for g, grp in sorted(groups.items(), key=lambda kv: kv[0][:2]):
                if need <= 0:
                    break
                t = g[2]
                if splits[t][0] != p or len(grp) != splits[t][1]:
                    continue
                if any(x.contents or x.id in used_ids or not _usable(x, reserved) for x in grp):
                    continue
                if any(s not in by_sku for x in grp for s in x.orig):
                    continue
                ops.append(_join_op(bins, grp, p, nid, types))
                used_ids |= {x.id for x in grp}
                need -= cap[p]
    return ops


def _not_worse(geo, src, dst) -> bool:
    """Levar o conteúdo de `src` para `dst` não piora a caminhada (até 1 m) nem tira da zona de ouro da pega."""
    if geo is None:
        return True
    a, b = geo.get(src), geo.get(dst)
    if a is None or b is None:
        return src == dst
    if a.golden is True and b.golden is False:
        return False
    return b.walk_m <= a.walk_m + 1.0


def consolidate(res, splits: dict, prm, reserved: set) -> int:
    """Esvazia os grupos com menos compartimentos usados, levando o conteúdo para compartimentos vazios de grupos já
    usados (irmãs no mesmo pai). Retorna quantos grupos ficaram vazios."""
    geo = (prm.slotting or {}).get("geo") if prm.slotting else None
    _gid, groups = groups_of(res.bins, splits)
    plan_of = {p.sku: p for p in res.plans}
    freed = 0

    def used_cells(grp):
        return [x for x in grp if x.contents]
    partial = [g for g, grp in groups.items() if used_cells(grp) and len(used_cells(grp)) < len(grp)]
    for g in sorted(partial, key=lambda g: (len(used_cells(groups[g])), g[:2])):
        src = used_cells(groups[g])
        if not src or any(not _usable(x, reserved) for x in groups[g]):
            continue
        moves = []
        taken: set = set()
        ok = True
        for b1 in src:
            if len(b1.contents) != 1:
                ok = False
                break
            (sku, _q), = b1.contents.items()
            if sku in b1.orig and prm.objective == "min_moves":
                ok = False  # já está nesta bin: mudar só para juntar irmãs seria uma movimentação a mais
                break
            plan = plan_of.get(sku)
            if plan is None:
                ok = False
                break
            others = {bb.location_id for bb, _m in plan.allocations if bb is not b1}
            best = None
            for g2, grp2 in groups.items():
                if g2 == g or g2[2] != g[2] or not used_cells(grp2):
                    continue
                for b2 in grp2:
                    if b2.contents or b2.id in taken or not _usable(b2, reserved):
                        continue
                    if any(s not in plan_of for s in b2.orig):
                        continue
                    if not prm.allow_split and others and others != {b2.location_id}:
                        continue
                    if not _not_worse(geo, b1.location_id, b2.location_id):
                        continue
                    key = (-len(used_cells(grp2)), geo[b2.location_id].walk_m if geo and b2.location_id in geo else 0,
                           b2.address, b2.id)
                    if best is None or key < best[0]:
                        best = (key, b2)
            if best is None:
                ok = False
                break
            taken.add(best[1].id)
            moves.append((b1, best[1], plan))
        if not ok or not moves:
            continue
        for b1, b2, plan in moves:  # mesmo tipo: mesma capacidade
            b2.contents, b2.used = dict(b1.contents), b1.used
            b1.contents, b1.used = {}, 0.0
            plan.allocations = [(b2 if bb is b1 else bb, m) for bb, m in plan.allocations]
        freed += 1
    return freed


def join_empty(res, splits: dict, reserved: set, types: dict) -> list:
    """Grupos completos de compartimentos vazios no plano (e sem produto que fica) viram bins pai inteiras."""
    _gid, groups = groups_of(res.bins, splits)
    in_plan = {p.sku for p in res.plans}
    nid = _new_id(res.bins)
    ops = []
    for g, grp in sorted(groups.items(), key=lambda kv: kv[0]):
        t = g[2]
        p, k = splits[t]
        if len(grp) != k or any(x.contents or not _usable(x, reserved) for x in grp):
            continue
        if any(s not in in_plan for x in grp for s in x.orig):
            continue
        ops.append(_join_op(res.bins, grp, p, nid, types))
    return ops


def reshape(res, bins: list, demands: list, types: dict, prm, reserved, no_dims, secondary, solve: Callable):
    """Fracionar/juntar sobre o resultado do Pool (ver o topo do módulo)."""
    from services.pool_optimizer import rebuild_pool_result

    splits = {t: pk for t, pk in (prm.splits or {}).items() if t in types and pk[0] in types}
    if not splits or prm.unlimited:
        return res
    reserved = set(reserved or set())
    short = lambda r: sum(p.shortage for p in r.plans)  # noqa: E731
    best, ops_best, cur, ops = res, [], bins, []
    for _it in range(3):
        if not short(best):
            break
        new = _shortage_ops(best, best.bins, demands, types, prm, reserved, splits)
        if not new:
            break
        ops = ops_best + new
        cur = apply_ops(_fresh(bins), [])  # parte das bins originais, sem plano
        cur = _apply_fresh(cur, ops)
        r2 = solve(cur, demands, types, prm, reserved, no_dims, secondary)
        if short(r2) < short(best):
            best, ops_best = r2, ops
        else:
            break
    moved = consolidate(best, splits, prm, reserved)
    joins = join_empty(best, splits, reserved, types)
    if moved or joins or ops_best:
        final_bins = apply_ops(best.bins, joins)
        out = rebuild_pool_result(final_bins, demands, best.plans, types, prm, reserved, no_dims)
        for k_ in ("solver", "orphans", "displaced", "fixed_pids", "focus"):
            setattr(out, k_, getattr(best, k_))
        out.reshape = _used_ops(ops_best, best) + joins
        if moved:
            out.notes = list(getattr(best, "notes", []) or []) + [
                _("{n} bin(s) pai dividida(s) esvaziada(s) para as irmãs ficarem juntas.", n=moved)]
        return out
    return best


def _apply_fresh(bins: list, ops: list) -> list:
    """Aplica operações (feitas sobre outras cópias das mesmas bins) nas cópias novas, pelo id."""
    gone = {b.id for op in ops for b in op["removed"]}
    out = [b for b in bins if b.id not in gone]
    for op in ops:
        for c in op["created"]:
            out.append(dataclasses.replace(c, contents={}, used=0.0))
    return out


def _used_ops(ops: list, res) -> list:
    """As operações valem com as bins do resultado (cópias novas): troca as referências pelas do resultado."""
    by_id = {b.id: b for b in res.bins}
    out = []
    for op in ops:
        out.append({**op, "created": [by_id.get(c.id, c) for c in op["created"]]})
    return out
