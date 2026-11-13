"""
Pesquisa operacional para a otimização de bins (OR-Tools CP-SAT).

Os dois modos ganham um modelo de programação inteira resolvido de uma vez para o escopo inteiro, em vez de decidir
um item por vez (heurística):

- **Pool de bins** — bins iguais do mesmo endereço formam um grupo (endereço, tipo). Variáveis: unidades de cada SKU
  em cada grupo e quantas bins do grupo ele usa (ou, com SKUs misturados, quantas bins do grupo ficam ocupadas).
  Restrições: guardar toda a quantidade (falta = variável penalizada), capacidade das bins, mistura ou não, um
  endereço por SKU (se não puder dividir), um tipo por SKU, máximo de bins por SKU.
- **Por endereço** — ver `solve_addresses`.

Objetivo lexicográfico: primeiro não deixar falta (ou violação), depois os critérios na ordem de prioridade
escolhida (bins, volume, largura, mudanças/movimentações). Cada etapa fixa o valor alcançado e otimiza a próxima.
A solução da heurística entra como ponto de partida (o resultado nunca fica pior que ela) e há tempo limite: se o
tempo acabar, vale a melhor solução encontrada (com o limite inferior, para mostrar a distância até o ótimo).
"""
from __future__ import annotations

import copy
import math
import time
from typing import Optional

from services.bin_optimizer import BinSpec, CRITERIA, capacity, priority_list
from services.i18n import N_, _

SCALE = 720720  # divisível por 1..16, 18, 20…: frações de bin exatas para as capacidades comuns
CAND_PER_SKU = 30  # grupos de bins candidatos por SKU no modelo do pool (os mais promissores por critério)

STATUS_TXT = {"optimal": N_("solver: ótimo comprovado"),
              "feasible": N_("solver: melhor solução no tempo limite (não comprovada ótima)"),
              "heuristic": N_("HEURÍSTICA: o solver não achou solução melhor no tempo limite"),
              "fallback": N_("HEURÍSTICA: solver indisponível")}
STATUS_HELP = {"feasible": N_("O tempo acabou antes de o solver provar que é a melhor solução possível. Para a solução do "
                           "solver a qualquer custo, marque \"Sem limite de tempo\"."),
               "heuristic": N_("Com o tempo limite, o solver não encontrou solução melhor que a da heurística, que foi a "
                            "usada. Para a solução do solver a qualquer custo, marque \"Sem limite de tempo\" (pode "
                            "demorar bastante em escopos grandes).")}


def available() -> bool:
    try:
        import ortools.sat.python.cp_model  # noqa: F401
        return True
    except Exception:  # pragma: no cover
        return False


class _Lex:
    """Resolve objetivos em sequência (lexicográfico) num mesmo modelo, dividindo o tempo limite entre as etapas.
    `time_limit` None = sem limite: cada etapa vai até provar o ótimo."""

    def __init__(self, model, time_limit: Optional[float]):
        from ortools.sat.python import cp_model

        self.cp = cp_model
        self.model = model
        self.deadline = time.monotonic() + time_limit if time_limit else math.inf
        self.total = time_limit
        self.stages: list[dict] = []
        self.values: Optional[dict] = None
        self.all_optimal = True

    def run(self, objectives: list[tuple[str, object]], hint_vars: list) -> bool:
        n = len(objectives)
        for i, (name, expr) in enumerate(objectives):
            left = self.deadline - time.monotonic()
            if left <= 0.05:
                self.all_optimal = False
                break
            share = left * (0.5 if i == 0 and n > 1 else 1.0 / (n - i))
            self.model.Minimize(expr)
            solver = self.cp.CpSolver()
            if share != math.inf:
                solver.parameters.max_time_in_seconds = max(0.2, share)
            import os as _os
            solver.parameters.num_workers = max(1, min(8, _os.cpu_count() or 1))
            import os
            if os.environ.get("NK_OR_LOG"):
                solver.parameters.log_search_progress = True
            st = solver.Solve(self.model)
            if st not in (self.cp.OPTIMAL, self.cp.FEASIBLE):
                if i == 0:
                    return False
                self.all_optimal = False
                break
            val = int(round(solver.ObjectiveValue()))
            opt = st == self.cp.OPTIMAL
            self.all_optimal &= opt
            self.stages.append({"criterion": name, "value": val, "optimal": opt,
                                "bound": int(math.floor(solver.BestObjectiveBound() + 1e-6))})
            self.values = {v.Index(): solver.Value(v) for v in hint_vars}
            self.model.Add(expr <= val)
            self.model.ClearHints()
            for v in hint_vars:
                self.model.AddHint(v, self.values[v.Index()])
        return self.values is not None

    def val(self, v) -> int:
        return self.values.get(v.Index(), 0) if self.values else 0


def _info(engine_status: str, t0: float, stages: list, heur: dict, final: dict, note: str = "") -> dict:
    return {"engine": "or", "status": engine_status, "status_txt": STATUS_TXT.get(engine_status, engine_status),
            "status_help": STATUS_HELP.get(engine_status, ""),
            "seconds": round(time.monotonic() - t0, 2), "stages": stages, "heuristic": heur, "final": final,
            "note": note}


# --------------------------------------------------------------------------- #
# Pool de bins
# --------------------------------------------------------------------------- #
def _pool_metrics(res, types: dict[int, BinSpec]) -> dict:
    """Valores dos critérios de um resultado do pool (para comparar heurística × solver)."""
    used = [b for b in (res.bins or []) if b.contents and b.location_id not in res.reserved_locations]
    if res.unlimited:
        used = {id(b): b for p in res.plans for b, _x in p.allocations}.values()
    used = list(used)
    return {"short": sum(p.shortage for p in res.plans), "bins": len(used),
            "volume": round(sum((types[b.type_id].volume_cm3 or 0) for b in used) / 1000.0, 1),
            "cost": round(sum((types[b.type_id].cost or 0) for b in used), 2),
            "width": round(sum((types[b.type_id].fw or 0) for b in used), 1),
            "changes": 0 if res.unlimited else round(sum(p.moved_units for p in res.plans), 1)}


def _better(a: dict, b: dict, prio: list) -> bool:
    """a é estritamente melhor que b na ordem (falta primeiro)?"""
    ka = tuple(a[c] for c in ["short"] + prio)
    kb = tuple(b[c] for c in ["short"] + prio)
    return ka < kb


def solve_pool(bins, demands, types: dict[int, BinSpec], prm, reserved_locations=None, no_dims=None):
    from services.pool_optimizer import PoolBin, SkuPlan, finish_pool_result, greedy_pool

    t0 = time.monotonic()
    reserved_locations = reserved_locations or set()
    prio = priority_list(prm.objective, prm.priority)
    if prm.unlimited:
        prio = [c for c in prio if c != "changes"]  # sem bins físicas não há movimentação
    # 1) heurística (ponto de partida e referência), em cópias das bins
    g_bins = copy.deepcopy(bins)
    greedy = greedy_pool(g_bins, demands, types, prm, reserved_locations, no_dims)
    heur = _pool_metrics(greedy, types)
    if not available():
        greedy.solver = _info("fallback", t0, [], heur, heur, _("OR-Tools não está instalado (pip install ortools)."))
        return _rebind(greedy, bins)

    from ortools.sat.python import cp_model

    base_prm = prm.as_params()
    pool = [b for b in bins if b.location_id not in reserved_locations and not b.locked]
    vol = {t: (s.volume_cm3 or 0.0) for t, s in types.items()}
    wid = {t: (s.fw or 0.0) for t, s in types.items()}
    cst = {t: (s.cost or 0.0) for t, s in types.items()}
    caps = {d.sku: {t: (capacity(d.unit, types[t], base_prm) if prm.allowed_types is None or t in prm.allowed_types else 0)
                    for t in types} for d in demands}
    needs = {d.sku: (int(math.ceil(d.qty * (1 + prm.growth) - 1e-9)) if d.qty > 0 else 0) for d in demands}
    S = [d for d in demands if needs[d.sku] > 0 and any(caps[d.sku].values())]
    if not S:
        greedy.solver = _info("optimal", t0, [], heur, heur, _("Nada a otimizar neste pool."))
        return _rebind(greedy, bins)

    # 2) grupos de bins iguais: (endereço, tipo, ocupada por produto fora do foco?) -> bins;
    #    recurso infinito: um grupo por tipo
    dem_skus = {d.sku for d in demands}

    def occ(b) -> int:
        return 1 if any(sk not in dem_skus and q > 0 for sk, q in b.orig.items()) else 0

    def gkey(b):
        return (None, b.type_id, 0) if prm.unlimited else (b.location_id, b.type_id, occ(b))

    groups: dict[tuple, list] = {}
    if prm.unlimited:
        for t in types:
            ub = sum(math.ceil(needs[d.sku] / caps[d.sku][t]) for d in S if caps[d.sku].get(t))
            if ub:
                groups[(None, t, 0)] = [None] * ub
    else:
        for b in sorted(pool, key=lambda b: (b.address, b.type_id, b.seq)):
            groups.setdefault((b.location_id, b.type_id, occ(b)), []).append(b)
    gkeys = list(groups)
    orig = {}  # (sku, g) -> unidades hoje nas bins do grupo
    orig_bins = {}  # (sku, g) -> [unidades em cada bin do grupo onde o SKU está hoje]
    for g in gkeys:
        for b in groups[g]:
            if b is not None:
                for sku, q in b.orig.items():
                    orig[(sku, g)] = orig.get((sku, g), 0) + q
                    orig_bins.setdefault((sku, g), []).append(q)

    # candidatos por SKU (redução se o modelo ficar grande demais)
    used_g = {}  # grupos usados pela heurística (sempre candidatos)
    for p in greedy.plans:
        for b, _n in p.allocations:
            used_g.setdefault(p.sku, set()).add(gkey(b))
    # capacidade de um SKU num grupo: com "mínimo de movimentações", o que já está numa bin pode ficar nela até a
    # capacidade física (100%), como na heurística; nos demais casos vale o enchimento máximo
    full_prm = type(base_prm)(fill_limit=1.0, use_geometry=prm.use_geometry, rotation=prm.rotation,
                              enforce_weight=prm.enforce_weight)
    full_caps = {}

    def cap_of(s_, g):
        c = caps[s_][g[1]]
        if prm.objective == "min_moves" and c and orig.get((s_, g)):
            key = (s_, g[1])
            if key not in full_caps:
                full_caps[key] = capacity(next(d.unit for d in S if d.sku == s_), types[g[1]], full_prm)
            return max(c, full_caps[key])
        return c

    cand: dict[str, list] = {}
    limit_k = CAND_PER_SKU
    reduced = False
    for d in S:
        lst = [g for g in gkeys if caps[d.sku].get(g[1])]
        if len(lst) > limit_k:
            reduced = True
            here = set(d.current) | ({d.default_location_id} if d.default_location_id else set())
            keep = {g for g in lst if g[0] in here or orig.get((d.sku, g))} | (used_g.get(d.sku) or set())
            c_ = caps[d.sku]
            rankings = [sorted(lst, key=lambda g: (vol[g[1]] / c_[g[1]], g)),  # volume por unidade
                        sorted(lst, key=lambda g: (cst[g[1]] / c_[g[1]], g)),  # custo por unidade
                        sorted(lst, key=lambda g: (wid[g[1]] / c_[g[1]], g)),  # largura por unidade
                        sorted(lst, key=lambda g: (-c_[g[1]] * len(groups[g]), g)),  # mais capacidade (menos bins)
                        sorted(lst, key=lambda g: (-(len(groups[g]) * c_[g[1]] >= needs[d.sku]), vol[g[1]] * math.ceil(needs[d.sku] / c_[g[1]]), g))]
            i = 0
            while len(keep) < limit_k and i < len(lst):
                for r in rankings:
                    if len(keep) >= limit_k:
                        break
                    keep.add(r[i])
                i += 1
            lst = [g for g in lst if g in keep]
        cand[d.sku] = lst

    m = cp_model.CpModel()
    mix = prm.mix_skus
    q, k, kept, short = {}, {}, {}, {}
    u = {}
    hint_vars = []
    for d in S:
        s_, need = d.sku, needs[d.sku]
        short[s_] = m.NewIntVar(0, need, f"sh_{s_}")
        hint_vars.append(short[s_])
        for g in cand[s_]:
            c = cap_of(s_, g)
            n_g = len(groups[g])
            q[(s_, g)] = m.NewIntVar(0, min(need, n_g * c), "")
            hint_vars.append(q[(s_, g)])
            if not mix:
                k[(s_, g)] = m.NewIntVar(0, min(n_g, max(math.ceil(need / c), len(orig_bins.get((s_, g), ())))), "")
                m.Add(q[(s_, g)] <= c * k[(s_, g)])
                m.Add(k[(s_, g)] <= q[(s_, g)])
                hint_vars.append(k[(s_, g)])
            o = orig.get((s_, g), 0)
            if o and not prm.unlimited:
                # unidades que ficam na MESMA bin física: no máximo as das bins de hoje que o SKU continua usando
                kept[(s_, g)] = m.NewIntVar(0, int(o), "")
                m.Add(kept[(s_, g)] <= q[(s_, g)])
                hint_vars.append(kept[(s_, g)])
                if not mix:
                    ob = sorted(orig_bins[(s_, g)], reverse=True)
                    kb = m.NewIntVar(0, len(ob), "")
                    m.Add(kb <= k[(s_, g)])
                    m.Add(kept[(s_, g)] <= int(math.ceil(max(ob))) * kb)
                    hint_vars.append(kb)
        m.Add(sum(q[(s_, g)] for g in cand[s_]) + short[s_] == need)
    # capacidade dos grupos
    for g in gkeys:
        n_g = len(groups[g])
        members = [s_ for s_ in cand if (s_, g) in q]
        if not members:
            continue
        if mix:
            u[g] = m.NewIntVar(0, n_g, "")
            hint_vars.append(u[g])
            m.Add(sum(q[(s_, g)] * (SCALE // cap_of(s_, g)) for s_ in members) <= u[g] * SCALE)
        else:
            m.Add(sum(k[(s_, g)] for s_ in members) <= n_g)
    # um endereço por SKU (sem dividir), um tipo por SKU, máximo de bins por SKU
    for d in S:
        s_, need = d.sku, needs[d.sku]
        if not prm.allow_split and not prm.unlimited:
            locs = sorted({g[0] for g in cand[s_]})
            a = {l: m.NewBoolVar("") for l in locs}
            m.Add(sum(a.values()) <= 1)
            for g in cand[s_]:
                m.Add(q[(s_, g)] <= need * a[g[0]])
        if prm.single_type:
            ts = sorted({g[1] for g in cand[s_]})
            bt = {t: m.NewBoolVar("") for t in ts}
            m.Add(sum(bt.values()) <= 1)
            for g in cand[s_]:
                m.Add(q[(s_, g)] <= need * bt[g[1]])
        if prm.max_bins:
            if mix:
                m.Add(sum(q[(s_, g)] * (SCALE // cap_of(s_, g)) for g in cand[s_]) <= prm.max_bins * SCALE)
            else:
                m.Add(sum(k[(s_, g)] for g in cand[s_]) <= prm.max_bins)

    # expressões dos critérios
    if mix:
        nb = {g: u[g] for g in u}
    else:
        nb = {}
        for (s_, g), v in k.items():
            nb.setdefault(g, []).append(v)
        nb = {g: sum(vs) for g, vs in nb.items()}
    exprs = {
        "short": sum(short.values()),
        "bins": sum(nb.values()),
        "volume": sum(int(round(vol[g[1]] / 100.0)) * nb[g] for g in nb),  # decilitros
        "width": sum(int(round(wid[g[1]] * 10)) * nb[g] for g in nb),  # milímetros
        "cost": sum(int(round(cst[g[1]] * 100)) * nb[g] for g in nb),  # centavos
        "changes": -sum(kept.values()) if kept else 0,  # máximo de unidades que ficam onde estão
    }

    # 3) dica inicial = solução da heurística
    g_index = {b.id: gkey(b) for p in greedy.plans for b, _x in p.allocations}
    hq, hk, hu = {}, {}, {}
    for p in greedy.plans:
        seen = set()
        for b, n in p.allocations:
            g = g_index[b.id]
            hq[(p.sku, g)] = hq.get((p.sku, g), 0) + n
            if b.id not in seen:
                hk[(p.sku, g)] = hk.get((p.sku, g), 0) + 1
                seen.add(b.id)
    for p in greedy.plans:
        for b, _x in p.allocations:
            hu.setdefault(g_index[b.id], set()).add(b.id)
    for key, v in q.items():
        m.AddHint(v, int(hq.get(key, 0)))
    for key, v in k.items():
        m.AddHint(v, int(hk.get(key, 0)))
    for g, v in u.items():
        m.AddHint(v, len(hu.get(g, ())))
    for s_, v in short.items():
        m.AddHint(v, next((p.shortage for p in greedy.plans if p.sku == s_), 0))
    for key, v in kept.items():
        m.AddHint(v, int(min(hq.get(key, 0), orig.get(key, 0))))

    stages = [("short", exprs["short"])] + [(c, exprs[c]) for c in prio]
    occ_groups = [g for g in nb if g[2]]
    if occ_groups:
        # bins com saldo de outros produtos ("qualquer bin"): depois do objetivo principal, desalojar o mínimo
        def foreign_units(g):
            return sum(q for b in groups[g] for sk, q in b.orig.items() if sk not in dem_skus) / max(len(groups[g]), 1)
        stages.insert(2, ("displaced", sum(int(round(foreign_units(g))) * nb[g] for g in occ_groups)))
    lex = _Lex(m, prm.time_limit)
    ok = lex.run(stages, hint_vars)
    if not ok:
        greedy.solver = _info("heuristic", t0, lex.stages, heur, heur, _("O solver não achou solução no tempo limite."))
        return _rebind(greedy, bins)

    # 4) monta o plano nas bins físicas
    plans = []
    virtual: list = []
    vgroups: dict[tuple, list] = {}
    for g in gkeys:
        if prm.unlimited:
            n_used = lex.val(u[g]) if mix and g in u else sum(lex.val(k[(s_, g)]) for s_ in cand if (s_, g) in k)
            lst = []
            for _x in range(n_used):
                vb = PoolBin(-(len(virtual) + 1), None, "", g[1], 0, type_code=types[g[1]].code)
                virtual.append(vb)
                lst.append(vb)
            vgroups[g] = lst
        else:
            vgroups[g] = list(groups[g])
    for d in demands:
        need = needs[d.sku]
        plan = SkuPlan(d.sku, d.product_id, d.qty, need, dict(d.current), spread=prm.objective != "min_moves",
                       unit_vol=d.unit.volume_cm3 or 0.0)
        plan.orig_bins = [(b, b.orig[d.sku]) for b in bins if d.sku in b.orig]
        plans.append(plan)
        if need > 0 and not any(caps[d.sku].values()):
            plan.shortage, plan.note = need, _("Não cabe em nenhum tipo de bin do escopo.")
    plan_of = {p.sku: p for p in plans}
    leftover: dict[str, int] = {}
    for g in gkeys:
        members = [(s_, lex.val(q[(s_, g)])) for s_ in cand if (s_, g) in q and lex.val(q[(s_, g)]) > 0]
        if not members:
            continue
        lst = vgroups[g]
        free = list(lst)
        # SKUs que já estão nas bins do grupo escolhem primeiro (mantêm as suas bins)
        members.sort(key=lambda x: (-orig.get((x[0], g), 0), x[0]))
        for s_, units in members:
            c = cap_of(s_, g)
            # as bins onde o SKU já está; depois as vazias hoje; por último as de outros SKUs (que vão sair)
            mine = sorted(free, key=lambda b: (0 if b.orig.get(s_) else (1 if not b.orig else 2),
                                               -(b.orig.get(s_) or 0), b.seq))
            n_bins = lex.val(k[(s_, g)]) if not mix else None
            chosen = mine[:n_bins] if not mix else mine
            put: dict[int, int] = {}

            def room(b):
                return (math.floor((1 - b.used) * c + 1e-9) if mix else c) - put.get(b.id, 0) * (0 if mix else 1)

            rest = units
            # 1º: o que já está em cada bin fica nela (mínimo de movimentação); 2º: completa as bins escolhidas
            for first in (True, False):
                for b in chosen:
                    if rest <= 0:
                        break
                    lim = room(b)
                    if first:
                        lim = min(lim, int(math.floor(b.orig.get(s_, 0) + 1e-9)))
                    take = min(rest, lim)
                    if take <= 0:
                        continue
                    put[b.id] = put.get(b.id, 0) + take
                    if mix:
                        b.used += take / c
                    rest -= take
            for b in chosen:
                n = put.get(b.id, 0)
                if n <= 0:
                    continue
                b.contents[s_] = b.contents.get(s_, 0) + n
                if not mix:
                    b.used = n / c
                    free.remove(b)
                elif b.used >= 1 - 1e-9 and b in free:
                    free.remove(b)
                plan_of[s_].allocations.append((b, n))
            if rest > 0:
                leftover[s_] = leftover.get(s_, 0) + rest
    # Com SKUs misturados o modelo soma frações de bin; ao dividir em bins inteiras pode sobrar alguma unidade:
    # ela vai para outra bin com espaço (no mesmo endereço, se o SKU não pode ser dividido entre endereços)
    for s_, rest in leftover.items():
        p = plan_of[s_]
        locs_ = {b.location_id for b, _x in p.allocations}
        for g in cand.get(s_, []):
            if rest <= 0:
                break
            if not prm.allow_split and not prm.unlimited and locs_ and g[0] not in locs_:
                continue
            c = cap_of(s_, g)
            for b in vgroups.get(g, []):
                if rest <= 0:
                    break
                if not mix and b.contents and set(b.contents) != {s_}:
                    continue
                r = math.floor((1 - b.used) * c + 1e-9)
                take = min(rest, r)
                if take <= 0:
                    continue
                b.contents[s_] = b.contents.get(s_, 0) + take
                b.used += take / c
                p.allocations.append((b, take))
                rest -= take
        if rest > 0:
            p.shortage += rest
            p.note = p.note or _("Sobrou ao montar as bins: revise as restrições ou use a heurística.")
    for d in S:
        sh = lex.val(short[d.sku])
        if sh:
            p = plan_of[d.sku]
            p.shortage += sh
            p.note = p.note or _("Faltam bins no escopo para este SKU.")

    from services.pool_optimizer import PoolResult

    occupied_before = {l for d in demands for l, qq in d.current.items() if qq > 0}
    occupied_before |= {d.default_location_id for d in demands if d.default_location_id}
    res = PoolResult([], bins, list(no_dims or []), set(reserved_locations))
    known = any(b.orig for b in pool)
    res.bins_used_before = (sum(1 for b in pool if b.orig) if known
                            else sum(1 for b in pool if b.location_id in occupied_before))
    res.addresses_before = len({b.location_id for b in pool if b.location_id in occupied_before})
    res.unlimited = prm.unlimited
    res = finish_pool_result(res, bins, pool, [] if prm.unlimited else pool, virtual, plans, prm, vol, wid, known,
                             occupied_before, reserved_locations)
    final = _pool_metrics(res, types)
    import os
    if os.environ.get("NK_OR_DEBUG"):
        print(_("DEBUG stages"), lex.stages, "heur", heur, "final", final)
    if _better(heur, final, prio):
        # o solver não terminou a tempo melhor que a heurística: fica a heurística (com aviso)
        greedy.solver = _info("heuristic", t0, lex.stages, heur, heur,
                              _("A heurística ficou melhor que a solução do solver no tempo limite — aumente o tempo."))
        return _rebind(greedy, bins)
    note = ""
    if reduced:
        note = (_("Para caber no tempo, cada SKU considerou até {limit_k} grupos de bins (endereço × tipo): os de onde ele está, os usados pela heurística e os melhores por volume, largura e capacidade.", limit_k=limit_k))
    res.solver = _info("optimal" if lex.all_optimal else "feasible", t0, lex.stages, heur, final, note)
    return res


def _rebind(res, bins):
    """O resultado da heurística foi calculado em cópias: devolve-o ligado às bins originais."""
    by_id = {b.id: b for b in bins}
    for b in res.bins:
        o = by_id.get(b.id)
        if o is not None:
            o.contents, o.used = b.contents, b.used
    for p in res.plans:
        p._moves, p._phys = None, None  # recalcula as movimentações com as bins originais
        p.allocations = [(by_id.get(b.id, b), n) for b, n in p.allocations]
        p.orig_bins = [(by_id.get(b.id, b), n) for b, n in p.orig_bins]
    res.bins = bins
    return res




# --------------------------------------------------------------------------- #
# Por endereço (modelo global)
# --------------------------------------------------------------------------- #
def _addr_metrics(results) -> dict:
    """Valores dos critérios de um resultado por endereço (para comparar heurística × solver)."""
    from services.bin_optimizer import changes_between

    out = {"short": sum(1 for r in results if r.status == "inviavel"), "bins": 0, "volume": 0.0, "width": 0.0,
           "changes": 0, "cost": 0.0}
    for r in results:
        m = r.suggested_metrics if r.applicable else r.current_metrics
        out["bins"] += m.bins
        out["volume"] += m.volume_l
        out["width"] += m.width_cm
        out["cost"] += m.cost
        if r.applicable:
            out["changes"] += changes_between(r.suggested or {}, r.current or {})
    out["volume"], out["width"], out["cost"] = round(out["volume"], 1), round(out["width"], 1), round(out["cost"], 2)
    return out


def solve_addresses(addresses, types: dict[int, BinSpec], prm, ledger=None):
    """Modelo global do modo por endereço: cada endereço escolhe UMA configuração (das que respeitam as restrições do
    próprio endereço) e quantas bins de cada tipo vão para cada nível; todos juntos respeitam a capacidade dos níveis,
    o estoque de bins (bins existentes) e "um tipo por nível". Retorna (resultados, informações do solver)."""
    from services.bin_optimizer import _key, changes_between, greedy_addresses, optimize_address

    t0 = time.monotonic()
    prio = priority_list(prm.objective, prm.priority)
    base_ledger = copy.deepcopy(ledger) if ledger is not None else None
    greedy = greedy_addresses(addresses, types, prm, copy.deepcopy(ledger) if ledger is not None else None)
    heur = _addr_metrics(greedy)
    if not available():
        return greedy, _info("fallback", t0, [], heur, heur, _("OR-Tools não está instalado (pip install ortools)."))
    from ortools.sat.python import cp_model

    g_by_id = {r.id: r for r in greedy}
    # 1) candidatos de cada endereço
    entries = []  # (endereço, opções [(cfg, Metrics, mudanças, válida)], níveis possíveis) | fixos
    fixed = {}  # id -> configuração depois (endereços sem escolha: sem saldo, sem dados, inviáveis…)
    fixed_pl = {}
    for a in addresses:
        col: dict = {}
        optimize_address(a, types, prm, copy.deepcopy(base_ledger) if base_ledger is not None else None, collect=col)
        g = g_by_id[a.id]
        if "cands" not in col or not col["cands"]:
            after = (g.suggested or {}) if (g.applicable or g.status == "liberar") else dict(a.current)
            fixed[a.id] = after
            fixed_pl[a.id] = (g.placement if g.applicable and g.placement is not None else
                              ({} if g.status == "liberar" else {k: dict(v) for k, v in a.current_placement.items()}))
            continue
        opts, seen = [], set()
        for c, m in col["cands"]:
            key = _key(c)
            if key not in seen:
                seen.add(key)
                opts.append((c, m, changes_between(c, a.current), True))
        if _key(a.current) not in seen:  # manter como está (pode não respeitar: penalizado)
            from services.bin_optimizer import metrics

            opts.append((dict(a.current), metrics(a.current, types), 0, False))
        slots = col.get("slots")
        if slots is not None:
            slots = list(dict.fromkeys(list(slots) + list(a.current_placement)))
        entries.append((a, opts, slots))
    if not entries:
        return greedy, _info("optimal", t0, [], heur, heur, _("Nada a otimizar neste escopo."))

    m = cp_model.CpModel()
    x = {}
    hint_vars = []
    pen = []  # violações (etapa 0)
    pvar = {}  # (endereço, nível, tipo) -> bins
    lvl_slot = ledger.slots if ledger is not None else {}
    for a, opts, slots in entries:
        xs = [m.NewBoolVar("") for _x in opts]
        x[a.id] = xs
        hint_vars += xs
        m.AddExactlyOne(xs)
        for xo, o in zip(xs, opts):
            if not o[3]:
                pen.append(xo)
        if slots is not None:
            all_t = sorted({t for o in opts for t in o[0]} | {t for cfg in a.current_placement.values() for t in cfg})
            for t in all_t:
                tot = sum(xo * o[0].get(t, 0) for xo, o in zip(xs, opts))
                vs = []
                for l in slots:
                    sl = lvl_slot.get(l)
                    caps = sl.caps if sl else None
                    allowed = (not caps) or caps.get(t) or a.current_placement.get(l, {}).get(t)
                    if not allowed:
                        continue
                    ub = max((o[0].get(t, 0) for o in opts), default=0)
                    v = m.NewIntVar(0, ub, "")
                    pvar[(a.id, l, t)] = v
                    vs.append(v)
                    hint_vars.append(v)
                m.Add(sum(vs) == tot) if vs else m.Add(tot == 0)
            # manter = mesmas bins nos mesmos níveis
            keep_i = next((i for i, o in enumerate(opts) if _key(o[0]) == _key(a.current)), None)
            if keep_i is not None:
                for (aid, l, t), v in list(pvar.items()):
                    if aid == a.id:
                        m.Add(v == a.current_placement.get(l, {}).get(t, 0)).OnlyEnforceIf(xs[keep_i])

    # 2) capacidade dos níveis (com as bins dos endereços fora do escopo e dos fixos) e um tipo por nível
    if ledger is not None and prm.respect_levels:
        levels = {l for (_x, l, _x) in pvar}
        cur_in_scope: dict = {}
        for a in addresses:
            for l, cfg in a.current_placement.items():
                for t, n in cfg.items():
                    cur_in_scope.setdefault(l, {})
                    cur_in_scope[l][t] = cur_in_scope[l].get(t, 0) + n
        for l in levels:
            sl = lvl_slot.get(l)
            base = dict(ledger.usage.get(l, {}))
            for t, n in cur_in_scope.get(l, {}).items():
                base[t] = base.get(t, 0) - n  # tira o que é do escopo (decidido pelo modelo)
            for aid, pl in fixed_pl.items():
                for t, n in (pl or {}).get(l, {}).items():
                    base[t] = base.get(t, 0) + n
            terms = [(v, t) for (aid, ll, t), v in pvar.items() if ll == l]
            if sl and sl.caps:
                # conta exata: frações do nível com denominador comum (mmc das capacidades)
                L = 1
                for cv in sl.caps.values():
                    if cv:
                        L = L * cv // math.gcd(L, cv)
                fixed_frac = sum(n * (L // sl.caps[t]) for t, n in base.items() if n > 0 and sl.caps.get(t))
                over = m.NewIntVar(0, 1000, "")  # bins "a mais" no nível (penalizado: só se não houver saída)
                pen.append(over)
                m.Add(sum(v * (L // sl.caps[t]) for v, t in terms if sl.caps.get(t)) + fixed_frac
                      <= L + over * (L // max(1, min(c for c in sl.caps.values() if c))))
            if not prm.mixed_level_types:
                fixed_t = {t for t, n in base.items() if n > 0}
                ts = sorted({t for _x, t in terms} | fixed_t)
                z = {t: m.NewBoolVar("") for t in ts}
                for t in fixed_t:
                    m.Add(z[t] == 1)
                m.Add(sum(z.values()) <= max(1, len(fixed_t)))
                for v, t in terms:
                    m.Add(v <= 1000 * z[t])
    # 3) estoque de bins (bins existentes): o total de cada tipo não passa do que existe no escopo
    if prm.limit_inventory:
        budget: dict = dict(getattr(prm, "extra_inventory", None) or {})
        for a in addresses:
            for t, n in (a.current or {}).items():
                budget[t] = budget.get(t, 0) + n
        tset = set(budget) | {t for _x, opts, _x in entries for o in opts for t in o[0]}
        for t in tset:
            fixed_n = sum(cfg.get(t, 0) for cfg in fixed.values())
            expr = sum(xo * o[0].get(t, 0) for a, opts, _x in entries for xo, o in zip(x[a.id], opts))
            slack = m.NewIntVar(0, 100000, "")
            pen.append(slack)
            m.Add(expr + fixed_n <= budget.get(t, 0) + slack)

    # 4) critérios
    def total(f):
        return sum(xo * f(o) for a, opts, _x in entries for xo, o in zip(x[a.id], opts))
    exprs = {"short": sum(pen) if pen else 0,
             "bins": total(lambda o: o[1].bins),
             "volume": total(lambda o: int(round(o[1].volume_l * 10))),
             "width": total(lambda o: int(round(o[1].width_cm * 10))),
             "cost": total(lambda o: int(round(o[1].cost * 100))),
             "changes": total(lambda o: o[2])}
    own_extra = [v for (aid, l, t), v in pvar.items()
                 if l != next((a.own_level for a, _x, _x in entries if a.id == aid), None)]
    stages = [("short", exprs["short"])] + [(c, exprs[c]) for c in prio]
    if own_extra:
        stages.append(("levels", sum(own_extra)))  # desempate: preferir o próprio nível

    # 5) dica = heurística
    for a, opts, slots in entries:
        g = g_by_id[a.id]
        cfg = (g.suggested if g.applicable else a.current) or {}
        i = next((i for i, o in enumerate(opts) if _key(o[0]) == _key(cfg)), None)
        for j, xo in enumerate(x[a.id]):
            m.AddHint(xo, 1 if j == i else 0)
        pl = (g.placement if g.applicable else a.current_placement) or {}
        for (aid, l, t), v in pvar.items():
            if aid == a.id:
                m.AddHint(v, int(pl.get(l, {}).get(t, 0)))

    lex = _Lex(m, prm.time_limit)
    if not lex.run(stages, hint_vars):
        return greedy, _info("heuristic", t0, lex.stages, heur, heur, _("O solver não achou solução no tempo limite."))

    # 6) resultados (situação e observações montadas como na heurística)
    results = []
    by_entry = {a.id: (a, opts, slots) for a, opts, slots in entries}
    for a in addresses:
        if a.id not in by_entry:
            results.append(g_by_id[a.id])
            continue
        _x, opts, slots = by_entry[a.id]
        i = next(j for j, xo in enumerate(x[a.id]) if lex.val(xo))
        cfg, _m, _c, valid = opts[i]
        pl = None
        if slots is not None:
            pl = {}
            for (aid, l, t), v in pvar.items():
                if aid == a.id and lex.val(v) > 0:
                    pl.setdefault(l, {})[t] = lex.val(v)
        r = optimize_address(a, types, prm, base_ledger, force=(cfg, pl))
        if not valid:
            r.status, r.suggested, r.suggested_metrics, r.placement = "inviavel", None, None, None
            r.notes.append(_("Sem configuração que respeite as restrições do escopo (níveis / bins disponíveis)."))
        results.append(r)
    final = _addr_metrics(results)
    if _better(heur, final, prio):
        return greedy, _info("heuristic", t0, lex.stages, heur, heur,
                             _("A heurística ficou melhor que a solução do solver no tempo limite — aumente o tempo."))
    return results, _info("optimal" if lex.all_optimal else "feasible", t0, lex.stages, heur, final)


# --------------------------------------------------------------------------- #
# Cache da análise (o "aplicar" usa exatamente o plano mostrado)
# --------------------------------------------------------------------------- #
# Com vários núcleos o solver pode achar soluções diferentes (de mesmo valor) a cada execução. Para que o plano
# aplicado seja o mesmo que o usuário viu, a análise com pesquisa operacional fica em cache por alguns minutos,
# enquanto os dados (saldos, bins, conteúdo, cadastros) não mudarem.
_CACHE: dict = {}
_CACHE_TTL = 1800
_CACHE_MAX = 12


def _fingerprint(db) -> tuple:
    from sqlalchemy import text

    out = []
    for t, extra in (("stock_items", "TOTAL(quantity)"), ("location_bins", "TOTAL(quantity)"),
                     ("bin_contents", "TOTAL(quantity)"), ("bin_types", "COUNT(*)"), ("products", "COUNT(*)"),
                     ("level_capacities", "TOTAL(max_bins)"), ("shelf_levels", "TOTAL(height_cm)"),
                     ("storage_locations", "COUNT(*)")):
        try:
            out.append(tuple(db.execute(text(f"SELECT COUNT(*), MAX(updated_at), {extra} FROM {t}")).one()))
        except Exception:
            out.append(tuple(db.execute(text(f"SELECT COUNT(*), {extra} FROM {t}")).one()))
    return tuple(out)


def cached(db, kind: str, location_ids, prm, extra, compute):
    """Executa `compute()` ou devolve o resultado guardado para os mesmos dados e parâmetros."""
    if getattr(prm, "engine", "heuristic") != "or":
        return compute()
    from services.i18n import get_lang

    # idioma na chave: o resultado traz textos prontos (avisos, violações)
    key = (kind, hash(tuple(sorted(location_ids))), repr(prm), repr(extra), _fingerprint(db), get_lang())
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = compute()
    _CACHE[key] = (now, val)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return val
