"""Unitização: as embalagens do produto (em hierarquia) e as paletizações, e a chave que liga/desliga o uso delas.

- embalagens: N por produto (ProductPackaging). Cada uma é feita de `inner_qty` unidades da embalagem de dentro
  (vazio = unidades do produto): display de 6 un. → caixa de 4 displays → master de 2 caixas. Medidas externas e peso
  da embalagem cheia; sem medidas, as do modelo de embalagem. Uma é a padrão (a primeira opção em toda bin);
- paletizações: X por embalagem (PackagingPallet) — caixas por camada × camadas num tipo de palete (tipo de bin com
  Armazena em = palete). Palete sem paletização cadastrada: calculada pelo arranjo das caixas (e avisada);
- chave de unitização: filial ("off" desligada / "cases" só caixas / "full" caixas e paletes), rodada (só restringe)
  e SKU ("never" = sempre unidades soltas). Vale o mais restrito. Desligada = o cadastro de embalagens é ignorado.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

PALLET_MAX_H = 150.0  # cm de carga acima da base do palete, quando o tipo não diz (máximo acima da borda)
MODES = ("off", "cases", "full")  # do mais restrito ao mais amplo
RUN_MODES = ("branch", "off", "cases")  # rodada: conforme a filial, ou restringe


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


# --------------------------------------------------------------------------- #
# Chave de unitização
# --------------------------------------------------------------------------- #
def effective(*modes) -> str:
    """O mais restrito dos modos ("branch"/None = sem restrição)."""
    ms = [m for m in modes if m in MODES]
    return min(ms, key=MODES.index) if ms else "full"


def branch_modes(db: Session) -> dict:
    """{filial: modo de unitização}."""
    from models import Branch

    return {b.id: (b.unitization or "off") for b in db.scalars(select(Branch))}


def branch_mode(db: Session, branch_id) -> str:
    from models import Branch

    b = db.get(Branch, int(branch_id)) if str(branch_id or "").isdigit() else None
    return (b.unitization or "off") if b is not None else "off"


def scope_mode(db: Session, branch_id, run: str = "branch") -> str:
    """Modo da rodada: o da filial do escopo (sem filial = o mais amplo entre as filiais) restringido pela rodada."""
    if str(branch_id or "").isdigit():
        base = branch_mode(db, branch_id)
    else:
        ms = set(branch_modes(db).values()) or {"off"}
        base = max(ms, key=MODES.index)
    return effective(base, run if run in MODES else None)


# --------------------------------------------------------------------------- #
# Medidas, peso e unidades de uma embalagem
# --------------------------------------------------------------------------- #
def dims_of(k) -> tuple:
    """(altura, largura, comprimento) externas: as do modelo de embalagem (uma embalagem "virtual" da planilha pode
    trazer medidas próprias)."""
    own = (_f(getattr(k, "height", None)), _f(getattr(k, "width", None)), _f(getattr(k, "length", None)))
    if all(own):
        return own
    m = getattr(k, "model", None)
    if m is not None and all(v is not None for v in (m.height, m.width, m.length)):
        return (_f(m.height), _f(m.width), _f(m.length))
    return own


def weight_of(k, product, _depth: int = 0) -> Optional[float]:
    """Peso da embalagem cheia: o informado, ou o conteúdo (unidades × peso, ou as embalagens de dentro) + a tara."""
    if k.weight is not None:
        return float(k.weight)
    if not k.inner_qty or _depth > 10:
        return None
    inner = (weight_of(k.inner, product, _depth + 1) if k.inner is not None
             else (_f(getattr(product, "weight", None)) if product is not None else None))
    if inner is None:
        return None
    m = getattr(k, "model", None)
    return inner * int(k.inner_qty) + ((_f(m.tare) or 0.0) if m is not None else 0.0)


def inner_dims(k, product) -> tuple:
    """Medidas do que vai dentro da embalagem: a embalagem de dentro ou a unidade do produto."""
    if k.inner is not None:
        return dims_of(k.inner)
    return (_f(product.height), _f(product.width), _f(product.length)) if product is not None else (None,) * 3


def pack_from(k, product):
    """Pack do otimizador a partir da embalagem (com as paletizações por tipo de palete)."""
    from services.bin_optimizer import Pack

    units = k.units()
    if not units:
        return None
    h, w, ln = dims_of(k)
    pallets: dict = {}
    for pp in getattr(k, "pallets", None) or []:
        pallets.setdefault(pp.pallet_type_id, []).append((pp.layer, pp.layers))
    return Pack(units, h, w, ln, weight_of(k, product), None, None,
                getattr(product, "store_pref", None) or "auto", k.name or "", pallets)


ANY = "any"  # embalagem habilitada em alguma filial
ALL = "all"  # todas as embalagens do produto


def enabled_in(k, branch=ANY) -> bool:
    """A embalagem entra na otimização da filial (`branch` = id), em alguma filial (ANY) ou sempre (ALL)."""
    if branch == ALL:
        return True
    if hasattr(k, "branches"):
        ids = {b.id for b in k.branches}
        return bool(ids) if branch == ANY else branch in ids
    return bool(getattr(k, "enabled", True))  # embalagem "virtual" (planilha do projeto)


def ordered(product, branch=ANY) -> list:
    """Embalagens do produto na ordem do cadastro, só as habilitadas na filial (`branch` = id; ANY = em alguma
    filial; ALL = todas)."""
    ks = [k for k in (getattr(product, "packagings", None) or []) if enabled_in(k, branch)]
    return sorted(ks, key=lambda k: (k.sort or 0, k.id or 0))


def default_of(product):
    """A primeira embalagem habilitada (sem nenhuma habilitada, a primeira do cadastro)."""
    ks = ordered(product) or ordered(product, ALL)
    return ks[0] if ks else None


def product_packs(product, mode: str, branch=ANY) -> tuple:
    """(1ª embalagem habilitada, as outras habilitadas) do otimizador para o modo de unitização; "off", SKU "never" ou
    nenhuma habilitada = (None, ()). Sem embalagens diferentes por bin, só a 1ª habilitada vale."""
    if product is None or mode == "off" or getattr(product, "unitization", "branch") == "never":
        return None, ()
    packs = [pk for pk in (pack_from(k, product) for k in ordered(product, branch)) if pk is not None and pk.ok]
    if not packs:
        return None, ()
    if mode == "cases":
        packs = [replace(pk, no_pallet=True) for pk in packs]
    alts = tuple(packs[1:]) if getattr(product, "mixed_packs", False) else ()
    return packs[0], alts


def load_products(db: Session, product_ids) -> dict:
    """{id: produto} com as embalagens, modelos e paletizações carregados (só os que têm embalagem)."""
    from models import Product, ProductPackaging

    ids = list({i for i in product_ids if i})
    out = {}
    opts = selectinload(Product.packagings).options(
        selectinload(ProductPackaging.pallets), selectinload(ProductPackaging.model),
        selectinload(ProductPackaging.inner), selectinload(ProductPackaging.branches))
    for i in range(0, len(ids), 900):
        q = (select(Product).where(Product.id.in_(ids[i:i + 900]), Product.packagings.any()).options(opts))
        for p in db.scalars(q):
            out[p.id] = p
    return out


def load_packs(db: Session, product_ids, mode: str = "full") -> dict:
    """{produto: (1ª embalagem habilitada, outras)} dos produtos com embalagem, no modo de unitização."""
    if mode == "off":
        return {}
    out = {}
    for pid, p in load_products(db, product_ids).items():
        pk, alts = product_packs(p, mode)
        if pk is not None:
            out[pid] = (pk, alts)
    return out


def unit_of(p, qty: float = 0, mode: str = "full", branch=ANY):
    """ProductUnit do otimizador com as embalagens do produto habilitadas na filial, no modo de unitização."""
    from services.bin_optimizer import ProductUnit

    pk, alts = product_packs(p, mode, branch)
    return ProductUnit(p.sku, qty, _f(p.height), _f(p.width), _f(p.length), _f(p.weight), pk, alts)


def pallet_notes(units, types, mode: str = "full") -> list[str]:
    """Aviso: SKUs que vão para palete fechado sem paletização cadastrada para aquele palete (calculada)."""
    from services.i18n import _

    if mode != "full":
        return []
    pals = [t for t in types if getattr(t, "store_as", "") == "pallet"]
    if not pals:
        return []
    out = []
    for t in pals:
        n = sum(1 for u in units if u.pack is not None and not u.pack.no_pallet and not u.pack.pallets.get(t.id)
                and not (u.pack.layer and u.pack.layers))
        if n:
            out.append(_("{n} SKU(s) sem paletização cadastrada para o palete {code}: calculada pelas medidas.",
                         n=n, code=t.code))
    return out


# --------------------------------------------------------------------------- #
# Sugestões pelas medidas e conferências
# --------------------------------------------------------------------------- #
def fit_in_case(unit_dims: tuple, unit_weight: Optional[float], case: tuple) -> int:
    """Quantas unidades (ou embalagens de dentro) cabem na embalagem (arranjo geométrico, em qualquer orientação)."""
    from services.bin_optimizer import BinSpec, Params, ProductUnit, _cap_units

    if not all(unit_dims) or not all(case):
        return 0
    b = BinSpec(0, "caixa", *case)
    return _cap_units(ProductUnit("", 0, *unit_dims, None), b, Params(fill_limit=1.0, rotation="any",
                                                                        enforce_weight=False))


def suggest_pallet(case: tuple, weight: Optional[float], pallet) -> tuple[int, int]:
    """(caixas por camada, camadas) sugeridos para o palete (tipo de bin): o arranjo das caixas em pé na base do
    palete; camadas pela altura máxima de carga (máximo acima da borda do tipo ou 150 cm) e pelo peso máximo."""
    from services.bin_optimizer import BinSpec, Params, ProductUnit, _cap_units

    h, w, ln = case
    if not (h and w and ln) or pallet is None or not (pallet.width and pallet.length):
        return 0, 0
    base = BinSpec(0, "palete", h, float(pallet.width), float(pallet.length))
    layer = _cap_units(ProductUnit("", 0, h, w, ln, None), base, Params(fill_limit=1.0, rotation="upright",
                                                                         enforce_weight=False))
    max_h = _f(pallet.over_cm) or PALLET_MAX_H
    layers = int(math.floor(max_h / h + 1e-9))
    if weight and pallet.max_weight and layer:
        layers = min(layers, int(math.floor(float(pallet.max_weight) / (weight * layer) + 1e-9)))
    return max(0, layer), max(0, layers)


def fill(k, product) -> list[str]:
    """Completa o que ficou vazio pelas medidas (quantidade de dentro; caixas por camada e camadas de cada palete) e
    devolve as notas do que foi calculado."""
    from services.i18n import _

    notes = []
    case = dims_of(k)
    if not k.inner_qty and all(case):
        n = fit_in_case(inner_dims(k, product), None, case)
        if n:
            k.inner_qty = n
            notes.append(_("{name}: quantidade calculada pelas medidas: {n}.", name=k.name, n=n))
    for pp in getattr(k, "pallets", None) or []:
        if pp.layer and pp.layers:
            continue
        pal = pp.pallet_type
        layer, layers = suggest_pallet(case, weight_of(k, product), pal) if all(case) else (0, 0)
        if layer and layers:
            pp.layer, pp.layers = pp.layer or layer, pp.layers or layers
            notes.append(_("{name} no palete {code}: {a} por camada × {b} camadas (calculado pelas medidas).",
                           name=k.name, code=pal.code if pal is not None else "", a=pp.layer, b=pp.layers))
    return notes


def check(k, product) -> list[str]:
    """Avisos de uma embalagem: sem medidas, conteúdo que não cabe, lastro que não cabe na base do palete."""
    from services.i18n import _

    out = []
    case = dims_of(k)
    if not all(case):
        out.append(_("{name}: sem medidas — informe altura, largura e comprimento (ou um modelo de embalagem).",
                     name=k.name))
        return out
    if k.inner_qty:
        fit = fit_in_case(inner_dims(k, product), None, case)
        if fit and k.inner_qty > fit:
            out.append(_("{name}: {qty} não cabem pelas medidas; cabem até {fit}.", name=k.name, qty=k.inner_qty,
                         fit=fit))
    for pp in getattr(k, "pallets", None) or []:
        pal = pp.pallet_type
        if pal is None or not pp.layer:
            continue
        layer, _layers = suggest_pallet(case, weight_of(k, product), pal)
        if layer and pp.layer > layer:
            out.append(_("{name}: {n} por camada não cabem na base do palete {code}: cabem até {fit}.",
                         name=k.name, n=pp.layer, code=pal.code, fit=layer))
    return out


# --------------------------------------------------------------------------- #
# Embalagem padrão por colunas (importação de produtos e planilha do projeto)
# --------------------------------------------------------------------------- #
ITEM_PACK_KEYS = ("case_qty", "case_model", "case_h", "case_w", "case_l", "case_wt", "pallet", "pallet_layer",
                  "pallet_layers", "store_pref")


def item_has_pack(it: dict) -> bool:
    return any(it.get(k) not in (None, "") for k in ITEM_PACK_KEYS if k != "store_pref")


def catalogs(db: Session) -> tuple[dict, dict]:
    """({código: modelo de embalagem}, {código: tipo de bin de palete})."""
    from models import BinType, PackagingModel

    return ({m.code: m for m in db.scalars(select(PackagingModel))},
            {b.code: b for b in db.scalars(select(BinType).where(BinType.store_as == "pallet"))})


def model_for_dims(db: Session, models: dict, h, w, ln):
    """Modelo de embalagem com essas medidas (cm): o existente ou um novo "EMB-AxLxC" (colunas de medidas da caixa das
    importações, sem código de modelo)."""
    from decimal import Decimal

    from models import PackagingModel

    if not (h and w and ln):
        return None
    dims = (Decimal(str(h)), Decimal(str(w)), Decimal(str(ln)))
    for m in models.values():
        if (m.height, m.width, m.length) == dims:
            return m
    fmt = lambda v: f"{float(v):g}".replace(".", ",")  # noqa: E731
    m = PackagingModel(code=f"EMB-{fmt(h)}X{fmt(w)}X{fmt(ln)}"[:30], name=f"Caixa {fmt(h)} × {fmt(w)} × {fmt(ln)}",
                       height=dims[0], width=dims[1], length=dims[2], notes="")
    db.add(m)
    db.flush()
    models[m.code] = m
    return m


class _K:
    """Embalagem "virtual" (não gravada), com a mesma interface de ProductPackaging."""

    def __init__(self, **kw):
        self.inner, self.pallets, self.enabled, self.sort, self.id = None, [], True, 0, 0
        self.__dict__.update(kw)

    def units(self, _depth: int = 0):
        if not self.inner_qty:
            return None
        return int(self.inner_qty) * (self.inner.units(_depth + 1) if self.inner is not None else 1)


def _virtual_model(models: dict, code: str, h, w, ln):
    """O modelo pelo código; sem ele, as medidas informadas (embalagem com medidas próprias, só para o cálculo)."""
    from types import SimpleNamespace

    m = models.get(code or "")
    if m is not None:
        return m
    if h and w and ln:
        return SimpleNamespace(code="", name="", height=h, width=w, length=ln, tare=None)
    return None


def item_packaging(it: dict, product, models: dict, pallets: dict):
    """A embalagem do SKU do projeto pelas colunas da aba SKUs, sobre a 1ª habilitada do cadastro do produto.
    Retorna (embalagem, produto, sugeridos) — sugeridos ⊂ {"case_qty", "pallet"} (calculados pelas medidas)."""
    from types import SimpleNamespace

    base = default_of(product) if product is not None else None
    model = _virtual_model(models, it.get("case_model"), it.get("case_h"), it.get("case_w"), it.get("case_l")) or (
        base.model if base is not None else None)
    qty = it.get("case_qty") if it.get("case_qty") not in (None, "") else (base.inner_qty if base is not None else None)
    wt = it.get("case_wt") if it.get("case_wt") not in (None, "") else (base.weight if base is not None else None)
    k = _K(name=(getattr(model, "code", "") or "Caixa"), inner=base.inner if base is not None else None, inner_qty=qty,
           model=model, weight=wt)
    k.pallets = list(base.pallets) if base is not None else []
    pal = pallets.get(it.get("pallet") or "")
    if pal is not None:
        k.pallets = [pp for pp in k.pallets if pp.pallet_type_id != pal.id] + [SimpleNamespace(
            pallet_type_id=pal.id, pallet_type=pal, layer=it.get("pallet_layer"), layers=it.get("pallet_layers"))]
    prod = SimpleNamespace(height=it.get("h") if it.get("h") is not None else _f(getattr(product, "height", None)),
                           width=it.get("w") if it.get("w") is not None else _f(getattr(product, "width", None)),
                           length=it.get("l") if it.get("l") is not None else _f(getattr(product, "length", None)),
                           weight=it.get("wt") if it.get("wt") is not None else _f(getattr(product, "weight", None)))
    sug = set()
    before_q, before_p = k.inner_qty, [(pp.layer, pp.layers) for pp in k.pallets]
    fill(k, prod)
    if k.inner_qty and not before_q:
        sug.add("case_qty")
    if [(pp.layer, pp.layers) for pp in k.pallets] != before_p:
        sug.add("pallet")
    return k, prod, sug


def sheet_packagings(rows: list[dict], models: dict, pallets: dict) -> list:
    """Embalagens do SKU vindas da aba "Embalagens dos SKUs" do projeto: uma por modelo de embalagem, na ordem; "Feita
    de" é o código de outro modelo da lista do SKU."""
    from types import SimpleNamespace

    out, by_code = [], {}
    for r in sorted(rows, key=lambda r: (r.get("sort") or 0)):
        m = models.get(r["model"])
        if m is None:
            continue
        k = _K(name=m.code, inner=None, inner_qty=r.get("qty"), model=m, weight=r.get("wt"))
        k.enabled, k.sort, k._inner = r.get("enabled", True), r.get("sort") or 0, (r.get("inner") or "")
        k.pallets = [SimpleNamespace(pallet_type_id=pallets[c].id, pallet_type=pallets[c], layer=a, layers=b)
                     for c, a, b in r.get("pallets") or [] if c in pallets]
        out.append(k)
        by_code[m.code] = k
    for k in out:
        inner = by_code.get(k._inner)
        k.inner = inner if inner is not k else None
    return out


def item_packs(it: dict, product, models: dict, pallets: dict, mode: str, sheet: Optional[list] = None,
               branch=ALL):
    """(1ª embalagem, outras habilitadas, sugeridos) do SKU do projeto para o otimizador. Prioridade: a lista da aba
    Embalagens dos SKUs (`sheet`), depois as colunas de embalagem da aba SKUs, depois as embalagens do cadastro do
    produto habilitadas na filial de referência (`branch`; ALL = todas)."""
    from types import SimpleNamespace

    if mode == "off":
        return None, (), set()
    pref = it.get("store_pref") or (getattr(product, "store_pref", None) if product is not None else None) or "auto"
    unitization = it.get("unitization") or (getattr(product, "unitization", "branch") if product is not None else "branch")
    mixed = it.get("mixed_packs") if it.get("mixed_packs") is not None else (
        getattr(product, "mixed_packs", False) if product is not None else False)
    sug: set = set()
    if sheet:
        prod = SimpleNamespace(height=it.get("h"), width=it.get("w"), length=it.get("l"), weight=it.get("wt"),
                               store_pref=pref)
        ks = sheet_packagings(sheet, models, pallets)
        for k in ks:
            before = (k.inner_qty, [(pp.layer, pp.layers) for pp in k.pallets])
            fill(k, prod)
            if k.inner_qty and not before[0]:
                sug.add("case_qty")
            if [(pp.layer, pp.layers) for pp in k.pallets] != before[1]:
                sug.add("pallet")
        ks = [k for k in ks if k.enabled]
    elif item_has_pack(it):
        k, prod, sug = item_packaging(it, product, models, pallets)
        first = default_of(product) if product is not None else None
        ks = [k] + [x for x in ordered(product, branch) if x is not first] if product is not None else [k]
    elif product is not None and product.packagings:
        prod, ks = product, ordered(product, branch)
    else:
        return None, (), set()
    unit = SimpleNamespace(weight=_f(getattr(prod, "weight", None)), store_pref=pref)
    packs = [pk for pk in (pack_from(k, unit) for k in ks) if pk is not None and pk.ok]
    if not packs or unitization == "never":
        return None, (), sug
    if mode == "cases":
        packs = [replace(pk, no_pallet=True) for pk in packs]
    return packs[0], (tuple(packs[1:]) if mixed else ()), sug


def _all_branches(db: Session) -> list:
    from models import Branch

    return list(db.scalars(select(Branch)))


def apply_sheet_packs(db: Session, pr, rows: list[dict], models: dict, pallets: dict, branch=None) -> bool:
    """Grava no cadastro do produto a lista da aba Embalagens dos SKUs (cria/atualiza pelo modelo; as paletizações
    ficam iguais às da aba). As habilitadas na aba ficam habilitadas na filial `branch` (a criada pelo projeto)."""
    from decimal import Decimal

    from models import PackagingPallet, ProductPackaging

    by_model = {k.model_id: k for k in pr.packagings if k.model_id}
    for r in sorted(rows, key=lambda r: (r.get("sort") or 0)):
        m = models.get(r["model"])
        if m is None:
            continue
        k = by_model.get(m.id)
        if k is None:
            k = ProductPackaging(model=m, model_id=m.id, name=m.code[:40])
            pr.packagings.append(k)
            by_model[m.id] = k
        k.inner_qty, k.sort = r.get("qty"), r.get("sort") or 0
        k.weight = Decimal(str(r["wt"])) if r.get("wt") not in (None, "") else None
        k.pallets[:] = [PackagingPallet(pallet_type_id=pallets[c].id, pallet_type=pallets[c], layer=a, layers=b)
                        for c, a, b in r.get("pallets") or [] if c in pallets]
        if branch is not None:
            if r.get("enabled", True) and branch not in k.branches:
                k.branches.append(branch)
            elif not r.get("enabled", True) and branch in k.branches:
                k.branches.remove(branch)
    for r in rows:
        m, inner = models.get(r["model"]), models.get(r.get("inner") or "")
        if m is not None:
            k = by_model[m.id]
            k.inner = by_model.get(inner.id) if inner is not None and inner.id != m.id else None
    for k in pr.packagings:
        fill(k, pr)
    return bool(rows)


def apply_item_pack(db: Session, pr, it: dict, models: dict, pallets: dict, branches=None) -> bool:
    """Grava a embalagem das colunas (importação de produtos e planilha do projeto) no cadastro do produto: a do modelo
    informado (ou do modelo com as medidas da caixa, criado se preciso); nova = habilitada em `branches` (vazio =
    todas as filiais); só os campos preenchidos; a paletização do palete informado."""
    from decimal import Decimal

    from models import PackagingPallet, ProductPackaging

    changed = False
    if it.get("store_pref") and pr.store_pref != it["store_pref"]:
        pr.store_pref = it["store_pref"]
        changed = True
    if not item_has_pack(it):
        return changed
    m = models.get(it.get("case_model") or "") or model_for_dims(db, models, it.get("case_h"), it.get("case_w"),
                                                                  it.get("case_l"))
    k = next((x for x in pr.packagings if m is not None and x.model_id == m.id), None) if m is not None else None
    if k is None and m is None:
        k = default_of(pr)
    if k is None:
        if m is None:
            return changed  # sem modelo nem medidas: não há embalagem para gravar
        k = ProductPackaging(model=m, model_id=m.id, name=m.code[:40], sort=len(pr.packagings) + 1)
        k.branches = list(branches) if branches is not None else _all_branches(db)
        pr.packagings.append(k)
        changed = True
    for attr, key in (("inner_qty", "case_qty"), ("weight", "case_wt")):
        v = it.get(key)
        if v in (None, ""):
            continue
        v = int(v) if attr == "inner_qty" else Decimal(str(v))
        if getattr(k, attr) != v:
            setattr(k, attr, v)
            changed = True
    pal = pallets.get(it.get("pallet") or "")
    if pal is not None:
        pp = next((x for x in k.pallets if x.pallet_type_id == pal.id), None)
        if pp is None:
            pp = PackagingPallet(pallet_type_id=pal.id, pallet_type=pal)
            k.pallets.append(pp)
            changed = True
        for attr, key in (("layer", "pallet_layer"), ("layers", "pallet_layers")):
            v = it.get(key)
            if v not in (None, "") and getattr(pp, attr) != int(v):
                setattr(pp, attr, int(v))
                changed = True
    return changed
