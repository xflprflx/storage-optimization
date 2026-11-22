"""Unitização: a embalagem do produto (caixa e palete) e as sugestões calculadas pelas medidas.

- caixa: unidades por caixa (cadastradas — o fornecedor decide) + medidas externas e peso da caixa cheia; sem medidas,
  vêm do modelo de embalagem (Cadastros → Modelos de embalagem). O sistema confere se as unidades cabem na caixa e,
  sem a quantidade, sugere o máximo que cabe;
- palete: caixas por camada (lastro) × camadas, no tipo de bin do palete (store_as = "pallet"). Sem o padrão
  cadastrado, o sistema sugere pelo arranjo das caixas na base do palete e pela altura/peso máximos.
"""
from __future__ import annotations

import math
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

PALLET_MAX_H = 150.0  # cm de carga acima da base do palete, quando o tipo não diz (máximo acima da borda)


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def case_dims(p, model=None) -> tuple:
    """(altura, largura, comprimento) externas da caixa: as do produto ou, sem elas, as do modelo de embalagem."""
    own = (_f(p.case_height), _f(p.case_width), _f(p.case_length))
    if all(v for v in own):
        return own
    m = model if model is not None else getattr(p, "case_model", None)
    if m is not None:
        return (_f(m.height), _f(m.width), _f(m.length))
    return own


def case_weight(p, model=None) -> Optional[float]:
    """Peso da caixa cheia: o informado, ou unidades × peso da unidade + a tara do modelo."""
    if p.case_weight is not None:
        return float(p.case_weight)
    if p.weight is None or not p.case_qty:
        return None
    m = model if model is not None else getattr(p, "case_model", None)
    return float(p.weight) * int(p.case_qty) + (_f(m.tare) or 0.0 if m is not None else 0.0)


def pack_of(p, model=None):
    """Pack do otimizador (bin_optimizer.Pack) a partir do produto; None sem embalagem."""
    from services.bin_optimizer import Pack

    if not p.case_qty:
        return None
    h, w, ln = case_dims(p, model)
    return Pack(int(p.case_qty), h, w, ln, case_weight(p, model), p.pallet_layer, p.pallet_layers,
                p.store_pref or "auto")


def load_packs(db: Session, product_ids) -> dict:
    """{produto: Pack} dos produtos com embalagem."""
    from models import PackagingModel, Product

    ids = list({i for i in product_ids if i})
    out = {}
    models = {}
    for i in range(0, len(ids), 900):
        for p in db.scalars(select(Product).where(Product.id.in_(ids[i:i + 900]), Product.case_qty.is_not(None))):
            m = None
            if p.case_model_id:
                m = models.get(p.case_model_id) or db.get(PackagingModel, p.case_model_id)
                models[p.case_model_id] = m
            pk = pack_of(p, m)
            if pk is not None:
                out[p.id] = pk
    return out


def unit_of(p, qty: float = 0, pack=None):
    """ProductUnit do otimizador com a embalagem."""
    from services.bin_optimizer import ProductUnit

    return ProductUnit(p.sku, qty, _f(p.height), _f(p.width), _f(p.length), _f(p.weight),
                       pack if pack is not None else pack_of(p))


def fit_in_case(unit_dims: tuple, unit_weight: Optional[float], case: tuple) -> int:
    """Quantas unidades cabem na caixa (arranjo geométrico, em qualquer orientação)."""
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


def check(p, model=None, pallet=None) -> list[str]:
    """Avisos da embalagem do produto (cadastro e importação): unidades que não cabem na caixa, caixa sem medidas,
    padrão de palete que não cabe na base."""
    from services.i18n import _

    out = []
    if not p.case_qty:
        return out
    case = case_dims(p, model)
    if not all(case):
        out.append(_("Caixa sem medidas: informe altura, largura e comprimento da caixa (ou um modelo de embalagem)."))
        return out
    unit = (_f(p.height), _f(p.width), _f(p.length))
    if all(unit):
        fit = fit_in_case(unit, _f(p.weight), case)
        if p.case_qty > fit:
            out.append(_("{qty} unidades não cabem na caixa: pelas medidas, cabem até {fit}.", qty=p.case_qty, fit=fit))
    if pallet is not None and p.pallet_layer:
        layer, _layers = suggest_pallet(case, case_weight(p, model), pallet)
        if layer and p.pallet_layer > layer:
            out.append(_("{n} caixas por camada não cabem na base do palete {code}: cabem até {fit}.",
                         n=p.pallet_layer, code=pallet.code, fit=layer))
    return out


# --------------------------------------------------------------------------- #
# Embalagem vinda da planilha do projeto de filial nova (aba SKUs), sobre o cadastro do produto
# --------------------------------------------------------------------------- #
ITEM_PACK_KEYS = ("case_qty", "case_model", "case_h", "case_w", "case_l", "case_wt", "pallet", "pallet_layer",
                  "pallet_layers", "store_pref")


def item_has_pack(it: dict) -> bool:
    return any(it.get(k) not in (None, "") for k in ITEM_PACK_KEYS)


def catalogs(db: Session) -> tuple[dict, dict]:
    """({código: modelo de embalagem}, {código: tipo de bin de palete})."""
    from models import BinType, PackagingModel

    return ({m.code: m for m in db.scalars(select(PackagingModel))},
            {b.code: b for b in db.scalars(select(BinType).where(BinType.store_as == "pallet"))})


def item_product(it: dict, p, models: dict, pallets: dict):
    """Produto "virtual" (o que vale para o projeto): a embalagem da planilha, completada pelo cadastro do produto.
    Sem unidades por caixa ou sem o padrão do palete, sugere pelas medidas. Retorna (produto, modelo, palete,
    sugeridos), com sugeridos ⊂ {"case_qty", "pallet"}."""
    from types import SimpleNamespace

    def pick(key, attr):
        v = it.get(key)
        return v if v not in (None, "") else (getattr(p, attr) if p is not None else None)

    model = models.get(it.get("case_model") or "") or (getattr(p, "case_model", None) if p is not None else None)
    pallet = pallets.get(it.get("pallet") or "")
    if pallet is None and p is not None and getattr(p, "pallet_type_id", None):
        pallet = next((b for b in pallets.values() if b.id == p.pallet_type_id), None)
    ns = SimpleNamespace(
        sku=it.get("sku"), height=pick("h", "height"), width=pick("w", "width"), length=pick("l", "length"),
        weight=pick("wt", "weight"), case_qty=pick("case_qty", "case_qty"), case_height=pick("case_h", "case_height"),
        case_width=pick("case_w", "case_width"), case_length=pick("case_l", "case_length"),
        case_weight=pick("case_wt", "case_weight"), pallet_layer=pick("pallet_layer", "pallet_layer"),
        pallet_layers=pick("pallet_layers", "pallet_layers"), store_pref=pick("store_pref", "store_pref") or "auto",
        case_model=model)
    for k in ("case_qty", "pallet_layer", "pallet_layers"):
        if getattr(ns, k) is not None:
            setattr(ns, k, int(getattr(ns, k)))
    suggested = set()
    case = case_dims(ns, model)
    if not ns.case_qty and all(case):
        n = fit_in_case((_f(ns.height), _f(ns.width), _f(ns.length)), None, case)
        if n:
            ns.case_qty = n
            suggested.add("case_qty")
    if ns.case_qty and pallet is not None and all(case) and not (ns.pallet_layer and ns.pallet_layers):
        layer, layers = suggest_pallet(case, case_weight(ns, model), pallet)
        if layer and layers:
            ns.pallet_layer, ns.pallet_layers = ns.pallet_layer or layer, ns.pallet_layers or layers
            suggested.add("pallet")
    return ns, model, pallet, suggested


def apply_item_pack(pr, it: dict, models: dict, pallets: dict) -> bool:
    """Grava no cadastro do produto a embalagem informada na planilha do projeto (só os campos preenchidos)."""
    from decimal import Decimal

    changed = False
    simple = {"case_qty": "case_qty", "pallet_layer": "pallet_layer", "pallet_layers": "pallet_layers",
              "store_pref": "store_pref"}
    dec = {"case_h": "case_height", "case_w": "case_width", "case_l": "case_length", "case_wt": "case_weight"}
    for k, attr in simple.items():
        v = it.get(k)
        if v not in (None, ""):
            v = int(v) if k != "store_pref" else v
            if getattr(pr, attr) != v:
                setattr(pr, attr, v)
                changed = True
    for k, attr in dec.items():
        v = it.get(k)
        if v not in (None, ""):
            v = Decimal(str(v))
            if getattr(pr, attr) != v:
                setattr(pr, attr, v)
                changed = True
    m = models.get(it.get("case_model") or "")
    if m is not None and pr.case_model_id != m.id:
        pr.case_model_id = m.id
        changed = True
    pal = pallets.get(it.get("pallet") or "")
    if pal is not None and pr.pallet_type_id != pal.id:
        pr.pallet_type_id = pal.id
        changed = True
    return changed
