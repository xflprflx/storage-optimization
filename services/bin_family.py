"""Família bin pai + compartimentos fracionáveis (tipo filho com "permitir fracionar a bin pai").

Uma bin pai dividida em k compartimentos é uma caixa só: num endereço com n compartimentos do tipo t há ⌈n/k⌉ bins
pai divididas (a última pode ter compartimentos vazios). Por isso, com bins existentes, a família é contada em bins
pai: cada endereço usa as bins pai inteiras dele + ⌈n/k⌉ por tipo de compartimento, e o total não passa do que existe
hoje no escopo. Fracionar (bin pai -> k compartimentos) e juntar (k compartimentos de uma bin pai -> bin pai,
tirando a divisória) são só a mesma bin mudando de forma, e o ⌈n/k⌉ faz os compartimentos de um endereço ocuparem as
mesmas bins pai (as irmãs juntas).

`splits` = {compartimento: (bin pai, k)} — só os tipos com a opção marcada.
"""
from __future__ import annotations

from typing import Iterable, Optional


def ceil_div(n: int, k: int) -> int:
    return -(-int(n) // int(k)) if n > 0 else 0


def parents_of(splits: Optional[dict]) -> dict[int, list[tuple[int, int]]]:
    """{bin pai: [(compartimento, k)]}."""
    out: dict[int, list] = {}
    for t, (p, k) in (splits or {}).items():
        out.setdefault(p, []).append((t, k))
    return out


def family_types(splits: Optional[dict]) -> set:
    """Tipos contados em família (bins pai e compartimentos fracionáveis)."""
    return set(splits or {}) | {p for p, _k in (splits or {}).values()}


def parent_units(cfgs: Iterable[dict], p: int, kids: list[tuple[int, int]]) -> int:
    """Bins pai usadas pela família `p` nas configurações (uma por endereço): inteiras + ⌈n/k⌉ por compartimento."""
    tot = 0
    for cfg in cfgs:
        tot += int(cfg.get(p, 0))
        for t, k in kids:
            tot += ceil_div(cfg.get(t, 0), k)
    return tot


def family_stock(currents: Iterable[dict], splits: Optional[dict], extra: Optional[dict] = None) -> dict[int, int]:
    """Bins pai de cada família que existem no escopo (inteiras + as divididas em compartimentos), + as de fora
    (`extra`, foco: compartimentos de outros endereços contam por bin pai completa)."""
    cur = list(currents)
    out = {}
    for p, kids in parents_of(splits).items():
        out[p] = parent_units(cur, p, kids) + int((extra or {}).get(p, 0)) + sum(
            int((extra or {}).get(t, 0)) // k for t, k in kids)
    return out


def over_families(currents: list, afters: list, splits: Optional[dict], extra: Optional[dict] = None) -> list[int]:
    """Famílias (bin pai) cujo uso passa do que existe."""
    stock = family_stock(currents, splits, extra)
    fam = parents_of(splits)
    return [p for p, kids in fam.items() if parent_units(afters, p, kids) > stock.get(p, 0)]


def report(currents: list, afters: list, splits: Optional[dict]) -> list[dict]:
    """Por compartimento: bins pai a fracionar (`split`) ou a juntar (`join`) e os compartimentos que ficam vazios
    nas bins pai divididas (`empty`). [{"parent", "child", "k", "split", "join", "empty", "before", "after"}]"""
    out = []
    for t, (p, k) in sorted((splits or {}).items()):
        before = sum(ceil_div(c.get(t, 0), k) for c in currents)
        after = sum(ceil_div(a.get(t, 0), k) for a in afters)
        empty = sum(ceil_div(a.get(t, 0), k) * k - int(a.get(t, 0)) for a in afters)
        if before == after:
            continue
        out.append({"parent": p, "child": t, "k": k, "split": max(0, after - before), "join": max(0, before - after),
                    "empty": empty, "before": before, "after": after})
    return out
