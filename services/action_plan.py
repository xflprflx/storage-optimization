"""
Plano de ação da otimização (os dois modos), em uma estrutura única usada pela tela, pelo Excel e pelo PDF:

  - cabeçalho: título, modo, data, escopo e parâmetros usados
  - indicadores antes × depois (com variação)
  - passo a passo numerado (ordem de execução, agrupado por corredor/endereço)
  - de-para detalhado
  - bins por tipo antes × depois
  - avisos
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from services.layout_service import natural_key


def _addr_key(address: str):
    return tuple(natural_key(p) for p in str(address or "").split("-"))


def _fmt(v, places=0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{places}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class Kpi:
    label: str
    before: Optional[float]
    after: Optional[float]
    unit: str = ""
    lower_is_better: bool = True
    places: int = 0

    @property
    def delta(self) -> Optional[float]:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before

    @property
    def pct(self) -> Optional[float]:
        if self.delta is None or not self.before:
            return None
        return self.delta / self.before * 100.0

    @property
    def good(self) -> Optional[bool]:
        d = self.delta
        if d is None or abs(d) < 1e-9:
            return None
        return (d < 0) == self.lower_is_better


@dataclass
class Step:
    n: int
    group: str  # corredor / endereço de agrupamento
    action: str  # Trocar bins, Retirar, Colocar, Mover, Providenciar, Liberar…
    where: str  # endereço / origem
    detail: str  # texto do passo
    to: str = ""  # destino (de-para)
    qty: str = ""
    kind: str = "info"  # css: change, add, remove, move, info
    status: str = ""  # situação do destino no momento do passo (livre agora / livre após o passo N …)


@dataclass
class ActionPlan:
    title: str
    mode: str
    created: datetime
    scope: list  # [(rótulo, valor)]
    params: list  # [(rótulo, valor)]
    kpis: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    depara_cols: list = field(default_factory=list)
    depara: list = field(default_factory=list)  # linhas (listas)
    visuals: list = field(default_factory=list)  # [{"label", "before": [(tipo, n, larg_cm)], "after": [...]}]
    by_type: list = field(default_factory=list)  # [(código, antes, depois)]
    warnings: list = field(default_factory=list)
    applicable: bool = True

    @property
    def n_steps(self) -> int:
        return len(self.steps)


# --------------------------------------------------------------------------- #
# Modo "Por endereço"
# --------------------------------------------------------------------------- #
def _cfg_text(cfg: dict, types) -> str:
    if not cfg:
        return "nenhuma bin"
    return " + ".join(f"{n}× {types[t].code if t in types else t}" for t, n in sorted(cfg.items()) if n)


def _placement_by_level(pl: Optional[dict], own) -> dict:
    """{nível (texto, '' = próprio): {tipo: qtd}}"""
    out: dict[str, dict[int, int]] = {}
    for key, cfg in (pl or {}).items():
        lv = "" if key == own else (key[4] if isinstance(key, tuple) else str(key))
        d = out.setdefault(lv, {})
        for t, n in cfg.items():
            if n:
                d[t] = d.get(t, 0) + n
    return out


def build_address_plan(results, summary, types, scope: list, params: list, contents: Optional[dict] = None,
                       staging: str = "ÁREA DE ESPERA") -> ActionPlan:
    plan = ActionPlan("Plano de ação — Otimização por endereço", "address", datetime.now(), scope, params)
    act = sorted([r for r in results if r.applicable], key=lambda r: _addr_key(r.address))
    s = summary
    changed = len(act)
    plan.kpis = [
        Kpi("Bins", s.bins_before, s.bins_after, "", True),
        Kpi("Volume das bins", s.volume_before, s.volume_after, " L", True, 1),
        Kpi("Largura de frente", s.width_before, s.width_after, " cm", True),
        Kpi("Enchimento médio", s.fill_before, s.fill_after, "%", False, 1),
        Kpi("Endereços com ação", None, changed, "", True),
    ]
    n = 0
    for r in act:
        aisle = r.address.split("-")[0]
        before_lv = _placement_by_level(r.current_placement, r.own_level) if r.current_placement else {"": dict(r.current)}
        after_lv = _placement_by_level(r.placement, r.own_level) if r.placement else {"": dict(r.suggested or {})}
        if not r.current_placement and not r.current:
            before_lv = {}
        levels = sorted(set(before_lv) | set(after_lv), key=lambda x: (x != "", natural_key(x) if x else 0))
        sub = []
        for lv in levels:
            b, a = before_lv.get(lv, {}), after_lv.get(lv, {})
            where = r.address + (f" (nível {lv})" if lv else "")
            for t in sorted(set(b) | set(a)):
                d = a.get(t, 0) - b.get(t, 0)
                code = types[t].code if t in types else str(t)
                if d < 0:
                    sub.append(("Retirar", where, f"Retirar {-d}× {code}", f"{-d}× {code}", "remove"))
                elif d > 0:
                    sub.append(("Colocar", where, f"Colocar {d}× {code}", f"{d}× {code}", "add"))
        if r.status == "liberar":
            sub = [("Liberar", r.address, f"Retirar todas as bins ({_cfg_text(r.current, types)}) — endereço sem saldo",
                    _cfg_text(r.current, types), "remove")]
        # retirar antes de colocar
        sub.sort(key=lambda x: 0 if x[0] in ("Retirar", "Liberar") else 1)
        if r.status == "orfao":
            sub = [("Retirar", r.address, f"{tag}: retirar {_fmt(q)} un. de {sku} — fica fora de bin no endereço "
                    "(órfão, sem destino no plano: tratar à mão)", f"{_fmt(q)} un.", "remove")
                   for sku, addr_, tag, q in (getattr(s, "orphans", None) or []) if addr_ == r.address] + sub
        cc = (contents or {}).get(r.id)
        if cc and r.status not in ("liberar", "orfao"):
            # 1) esvaziar as bins que mudam (para a área de espera) … 3) guardar nas bins novas
            pre = [("Esvaziar", r.address, f"Tirar {_fmt(q)} un. de {sku} da {tag} e levar para a {staging}", f"{_fmt(q)} un.",
                    "remove") for tag, sku, q in cc["empty"]]
            post = [("Guardar", r.address, f"{tag}: guardar " + " + ".join(f"{_fmt(q)} un. de {sku}" for sku, q in items)
                     + (f" (vindos da {staging})" if cc["empty"] else ""), "", "add") for tag, items in cc["store"]]
            sub = pre + sub + post
        for action, where, detail, qty, kind in sub:
            n += 1
            plan.steps.append(Step(n, f"Corredor {aisle}", action, where, detail, "", qty, kind))
        plan.depara.append([
            r.address, r.status_label, _cfg_text(r.current, types), _cfg_text(r.suggested or {}, types),
            r.bins_delta, round(r.volume_delta, 1), round(r.width_delta, 1),
            None if r.fill_now is None else round(r.fill_now, 1), None if r.fill_after is None else round(r.fill_after, 1),
            "; ".join(r.notes),
        ])
        if len(plan.visuals) < 60:
            plan.visuals.append({
                "label": r.address,
                "before": [(types[t].code if t in types else str(t), n_, types[t].w if t in types else None)
                           for t, n_ in sorted(r.current.items()) if n_],
                "after": [(types[t].code if t in types else str(t), n_, types[t].w if t in types else None)
                          for t, n_ in sorted((r.suggested or {}).items()) if n_],
            })
    plan.depara_cols = ["Endereço", "Situação", "Bins antes", "Bins depois", "Δ bins", "Δ volume (L)", "Δ largura (cm)",
                        "Enchimento antes (%)", "Enchimento depois (%)", "Observações"]
    # bins por tipo
    before: dict[int, int] = {}
    after: dict[int, int] = {}
    for r in results:
        for t, q in r.current.items():
            before[t] = before.get(t, 0) + q
        cfg = r.suggested if r.applicable else r.current
        for t, q in (cfg or {}).items():
            after[t] = after.get(t, 0) + q
    plan.by_type = [(types[t].code if t in types else str(t), before.get(t, 0), after.get(t, 0))
                    for t in sorted(set(before) | set(after), key=lambda t: types[t].code if t in types else str(t))]
    others = {k: v for k, v in s.by_status.items() if k in ("inviavel", "sem_dados")}
    if getattr(s, "orphans", None):
        plan.warnings.append(f"{len(s.orphans)} item(ns) ficam órfãos (fora de bin) para liberar bins aos itens filtrados.")
    if others.get("inviavel"):
        plan.warnings.append(f"{others['inviavel']} endereço(s) inviáveis com as restrições escolhidas — ficam como estão.")
    if others.get("sem_dados"):
        plan.warnings.append(f"{others['sem_dados']} endereço(s) com SKU sem dimensões — ficam como estão.")
    if not act:
        plan.warnings.append("Nenhuma ação: os endereços do escopo já estão no melhor arranjo para os parâmetros escolhidos.")
    return plan


# --------------------------------------------------------------------------- #
# Sequenciamento das movimentações (dependências + área de espera)
# --------------------------------------------------------------------------- #
def sequence_moves(moves: list, addr, staging: str = "ÁREA DE ESPERA") -> list[dict]:
    """Ordena as movimentações para que cada destino esteja livre quando chegar a vez dele.

    - Uma movimentação PARA a bin X depende das movimentações que TIRAM de X outros SKUs (que saem de lá).
    - Executa em "ondas": cada onda tem só passos cujo destino já está livre (podem ser feitos em qualquer ordem).
    - Se sobrar um ciclo (A→B e B→A, ou maior), um item vai para a ÁREA DE ESPERA e volta depois, quando o
      destino dele estiver livre.
    Retorna [{"wave", "move", "leg": "direct"|"to_staging"|"from_staging", "after": [nº dos passos que liberam]}]
    na ordem de execução (o nº do passo é a posição + 1)."""
    from itertools import count

    ids = count()
    nodes = []  # cada nó: dict(move, leg, blockers=set(node ids))
    for mv in moves:
        nodes.append({"id": next(ids), "move": mv, "leg": "direct"})
    out_of: dict[int, list] = {}  # bin -> nós que tiram algo dela
    for n in nodes:
        if n["move"].src_bin is not None:
            out_of.setdefault(n["move"].src_bin.id, []).append(n)
    for n in nodes:
        dst = n["move"].dst_bin
        n["blockers"] = {o["id"] for o in out_of.get(dst.id, []) if o["move"].sku != n["move"].sku} if dst is not None else set()
    walk = lambda n: (_addr_key(addr(n["move"].src_loc)) if n["move"].src_loc else (), _addr_key(addr(n["move"].dst_loc)),  # noqa: E731
                      n["move"].sku)
    remaining = {n["id"]: n for n in nodes}
    freed_by: dict[int, int] = {}  # nó -> nº do passo que liberou (o passo dele ou a ida para a espera)
    done: set[int] = set()
    seq: list[dict] = []
    wave = 0
    while remaining:
        wave += 1
        ready = sorted((n for n in remaining.values() if n["blockers"] <= done), key=walk)
        if not ready:
            # ciclo: manda para a espera o primeiro item (na ordem de caminhada) que segura um destino
            holding = {b for n in remaining.values() for b in n["blockers"]}
            cand = sorted((n for n in remaining.values() if n["id"] in holding), key=walk)[0]
            seq.append({"wave": wave, "move": cand["move"], "leg": "to_staging", "after": []})
            freed_by[cand["id"]] = len(seq)
            done.add(cand["id"])  # a bin de origem foi esvaziada
            cand2 = dict(cand, id=next(ids), leg="from_staging")
            del remaining[cand["id"]]
            remaining[cand2["id"]] = cand2
            continue
        for n in ready:
            seq.append({"wave": wave, "move": n["move"], "leg": n["leg"],
                        "after": sorted(freed_by[b] for b in n["blockers"] if b in freed_by)})
            freed_by[n["id"]] = len(seq)
            done.add(n["id"])
            del remaining[n["id"]]
    return seq


# --------------------------------------------------------------------------- #
# Modo "Cega aos endereços" (pool de bins)
# --------------------------------------------------------------------------- #
def build_pool_plan(results, locs, types, scope: list, params: list, unlimited: bool,
                    staging: str = "ÁREA DE ESPERA") -> ActionPlan:
    title = "Plano de ação — Pool de bins" + (" (dimensionamento com bins ilimitadas)" if unlimited else "")
    plan = ActionPlan(title, "pool_unlimited" if unlimited else "pool", datetime.now(), scope, params)
    plan.applicable = not unlimited and not any(r.shortage for r in results)
    code = lambda t: types[t].code if t in types else str(t)  # noqa: E731
    addr = lambda l: locs[l][0] if l in locs else "?"  # noqa: E731
    plans = [p for r in results for p in r.plans]
    by_type: dict[int, list] = {}
    for r in results:
        for t, v in r.by_type.items():
            bt = by_type.setdefault(t, [0, 0])
            bt[0] += v["existing"]
            bt[1] += v["needed"]
    plan.by_type = [(code(t), v[0], v[1]) for t, v in sorted(by_type.items(), key=lambda kv: code(kv[0]))]
    used_before = sum(r.bins_used_before for r in results)
    used_after = sum(r.bins_used_after for r in results)
    n = 0
    if unlimited:
        plan.kpis = [Kpi("Bins existentes no escopo", sum(v[0] for v in by_type.values()), None),
                     Kpi("Bins usadas hoje (estimado)", used_before, None),
                     Kpi("Bins necessárias (ideal)", None, used_after),
                     Kpi("Tipos de bin no plano", None, len([1 for v in by_type.values() if v[1]]), "", False)]
        for c, have, need in plan.by_type:
            if need > have:
                n += 1
                plan.steps.append(Step(n, "Providenciar bins", "Providenciar", c, f"Providenciar {need - have}× {c} "
                                       f"(necessárias {need}, existentes {have})", "", f"{need - have}× {c}", "add"))
            elif have > need:
                n += 1
                plan.steps.append(Step(n, "Bins que sobram", "Sobra", c, f"Sobram {have - need}× {c} "
                                       f"(necessárias {need}, existentes {have})", "", f"{have - need}× {c}", "remove"))
        for p in sorted(plans, key=lambda p: p.sku):
            per: dict[int, list] = {}
            for b, q in p.allocations:
                per.setdefault(b.type_id, []).append(q)
            for t, qs in per.items():
                n += 1
                plan.steps.append(Step(n, "Armazenamento por SKU", "Armazenar", p.sku,
                                       f"{p.sku}: {len(qs)}× {code(t)} ({' + '.join(str(q) for q in qs)} un.)",
                                       "", f"{len(qs)}× {code(t)}", "info"))
            plan.depara.append([p.sku, p.needed, ", ".join(f"{addr(l)} ({_fmt(q)})" for l, q in sorted(p.current.items(), key=lambda kv: _addr_key(addr(kv[0])))),
                                " + ".join(f"{len(v)}× {code(t)}" for t, v in per.items()) or "—", p.shortage or None, p.note])
        plan.depara_cols = ["SKU", "Quantidade planejada", "Onde está hoje (saldo)", "Bins necessárias", "Sem bin (un.)", "Observação"]
    else:
        moves = sum(r.moves for r in results)
        units = sum(r.moved_units for r in results)
        plan.kpis = [Kpi("Bins usadas", used_before, used_after),
                     Kpi("Endereços ocupados", sum(r.addresses_before for r in results), sum(r.addresses_after for r in results)),
                     Kpi("Movimentações", None, moves),
                     Kpi("Unidades movimentadas", None, units)]
        # passo a passo: ordenado por dependência (o destino precisa estar livre) + área de espera em ciclos
        all_moves = [mv for p in plans for mv in p.moves]
        tag = lambda b: b.tag if b is not None else "fora de bin"  # noqa: E731
        seq = sequence_moves(all_moves, addr, staging)
        n_staged = 0
        # órfãos (opção "deixar fora de bin"): primeiro libera as bins que os itens filtrados vão ocupar
        orphans = [o for r in results for o in (r.orphans or []) if o[2] is not None]
        for sku, l, b, q in orphans:
            n += 1
            plan.steps.append(Step(n, "Liberar bins (itens órfãos)", "Retirar", f"{addr(l)} · {b.tag}",
                                   f"{sku}: retirar {_fmt(q)} un. da bin — fica fora de bin no endereço (órfão, sem destino "
                                   "no plano: tratar à mão)", "fora de bin", _fmt(q), "remove"))
        if orphans:
            plan.warnings.append(f"{len(orphans)} item(ns) ficam órfãos (fora de bin) para liberar bins aos itens filtrados.")
        for item in seq:
            mv, leg = item["move"], item["leg"]
            src = f"{addr(mv.src_loc)} · {tag(mv.src_bin)}"
            dst = f"{addr(mv.dst_loc)} · {tag(mv.dst_bin)}"
            others = [k for k in (mv.dst_bin.orig if mv.dst_bin else {}) if k != mv.sku and k in mv.dst_bin.contents]
            if leg == "to_staging":
                n_staged += 1
                status = "área de espera (troca circular)"
                n += 1
                plan.steps.append(Step(n, f"Onda {item['wave']}", "Para a espera", src, f"Levar {_fmt(mv.qty)} un. de {mv.sku} "
                                       f"para {staging} (o destino {dst} ainda está ocupado — troca circular)", staging,
                                       _fmt(mv.qty), "remove", status))
                continue
            if item["after"]:
                status = "livre após o(s) passo(s) " + ", ".join(str(a) for a in item["after"])
            elif others:
                status = "divide a bin com " + ", ".join(others) + " (que fica)"
            elif mv.dst_bin is not None and mv.sku in mv.dst_bin.orig:
                status = "completa a bin (já tem este SKU)"
            else:
                status = "livre agora"
            origin = staging if leg == "from_staging" else src
            same = mv.src_loc == mv.dst_loc and leg != "from_staging"
            action = "Da espera" if leg == "from_staging" else ("Trocar de bin" if same else "Mover")
            text = (f"Levar {_fmt(mv.qty)} un. de {mv.sku} da {staging} para {dst}" if leg == "from_staging" else
                    f"Mover {_fmt(mv.qty)} un. de {mv.sku} de {src} para {dst}")
            n += 1
            plan.steps.append(Step(n, f"Onda {item['wave']}", action, origin, text, dst, _fmt(mv.qty), "move", status))
            plan.depara.append([mv.sku, addr(mv.src_loc), tag(mv.src_bin), addr(mv.dst_loc), tag(mv.dst_bin), mv.qty,
                                "sim" if leg == "from_staging" else "", status])
        plan.depara_cols = ["SKU", "Endereço origem", "Bin origem", "Endereço destino", "Bin destino", "Quantidade",
                            "Passa pela área de espera", "Destino no momento do passo"]
        if n_staged:
            plan.warnings.append(f"{n_staged} movimentação(ões) formam trocas circulares e passam pela {staging}.")
        transfers = all_moves
        # endereços que ficam vazios
        freed = []
        for r in results:
            used = {b.location_id for b in r.bins if b.contents} | r.reserved_locations
            before = {l for p in r.plans for l, q in p.current.items() if q > 0}
            freed += sorted((addr(l) for l in before - used), key=_addr_key)
        for a in freed:
            n += 1
            plan.steps.append(Step(n, "Ao final", "Liberado", a, f"Endereço {a} fica vazio (todas as bins livres)", "", "", "info"))
        # visual: bins usadas por endereço antes × depois (top endereços que mudam)
        usage: dict[str, list] = {}
        for r in results:
            occ = {l for p in r.plans for l, q in p.current.items() if q > 0}
            known = any(b.orig for b in r.bins)
            for b in r.bins:
                if b.location_id in r.reserved_locations:
                    continue
                u = usage.setdefault(addr(b.location_id), [0, 0, 0])
                u[2] += 1
                if (b.orig if known else b.location_id in occ):
                    u[0] += 1
                if b.contents:
                    u[1] += 1
        changed = sorted(((a, v) for a, v in usage.items() if v[0] != v[1]), key=lambda kv: _addr_key(kv[0]))[:60]
        plan.visuals = [{"label": a, "before_used": v[0], "after_used": v[1], "total": v[2]} for a, v in changed]
        if any(r.shortage for r in results):
            plan.warnings.append("Faltam bins para alguns SKUs: o plano está incompleto e não pode ser aplicado.")
        if not transfers:
            plan.warnings.append("Nenhuma movimentação necessária para os parâmetros escolhidos.")
    no_dims = [s for r in results for s in r.no_dims]
    if no_dims:
        plan.warnings.append(f"{len(no_dims)} SKU(s) sem dimensões ficaram fora do plano: {', '.join(no_dims[:15])}"
                             + ("…" if len(no_dims) > 15 else ""))
    short = [p for p in plans if p.shortage]
    if short:
        plan.warnings.append(f"{len(short)} SKU(s) sem bin: " + ", ".join(f"{p.sku} ({p.shortage} un.)" for p in short[:10])
                             + ("…" if len(short) > 10 else ""))
    return plan
