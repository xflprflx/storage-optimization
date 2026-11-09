"""
Mapa do estoque: monta a árvore física de um depósito para desenho proporcional.

Hierarquia: Corredor > Estante > Nível > Posição > Compartimento.
Cada endereço é uma FOLHA da árvore (pela regra de conflitos, um endereço genérico
nunca tem sub-endereços). O tamanho da folha vem das bins atribuídas a ela:

    vista frontal (elevação):  largura = soma das larguras das bins (lado a lado)
                               altura  = maior altura entre as bins
                               profundidade = maior comprimento entre as bins

Endereços sem bin (ou com bins sem dimensões) recebem um tamanho padrão e são
marcados como "sem dimensão" para o desenho diferenciá-los.

Também calcula, por endereço, o % de enchimento:
    volume = Σ(qtd × volume unitário do produto) ÷ Σ(volume das bins)
    peso   = Σ(qtd × peso unitário)               ÷ Σ(peso máximo das bins)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.bin_type_service import footprint_width
from models import BinType, LevelCapacity, LocationBin, Product, ShelfLevel, StockItem, StorageLocation
from services.addressing import scope_filter, segments

MAX_LAYOUT_LOCATIONS = 20_000
DEFAULT_SIZE_CM = (40.0, 30.0, 40.0)  # largura, altura, profundidade p/ endereço sem bin dimensionada
LEVEL_NAMES = ("aisle", "bay", "level", "position", "compartment")

# Paleta categórica para os tipos de bin (cores distinguíveis em fundo claro)
BIN_PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#edc948", "#76b7b2",
               "#ff9da7", "#9c755f", "#e15759", "#bab0ac", "#86bcb6", "#d37295"]


def scope_filter_level(stmt, branch_id: int, warehouse_id: Optional[int]):
    stmt = stmt.where(ShelfLevel.branch_id == branch_id)
    if warehouse_id is None:
        return stmt.where(ShelfLevel.warehouse_id.is_(None))
    return stmt.where(ShelfLevel.warehouse_id == warehouse_id)


def _aisle_eq(col, value):
    from services.app_settings import canon, get

    return func.nk_canon(col) == canon(value) if get("addr_zero_eq") else col == value


def natural_key(value: str):
    """Ordena '2' antes de '10' e 'Z' antes de 'AA' (como na geração em lote). Com a configuração de endereços
    "ordem alfabética", ordena pelo texto ('10' antes de '2'); com "07 = 7", os zeros à esquerda não contam."""
    from services.app_settings import canon, get

    value = value or ""
    if get("addr_sort") == "alpha":
        return (3, 0, canon(value))
    if get("addr_zero_eq") and value[:1] == "0" and value.isdigit():
        value = canon(value)
    if value.isdigit():
        return (0, int(value), value)
    if value.isalpha() and value.isascii():
        n = 0
        for ch in value.upper():
            n = n * 26 + (ord(ch) - 64)
        return (1, n, value)
    # Nível dedicado criado acima de outro: '01D1', '01D2' ficam logo depois de '01' (antes de '02')
    import re
    m = re.fullmatch(r"(\d+)D(\d+)", value)
    if m:
        return (0, int(m.group(1)) + int(m.group(2)) / 1000.0, value)
    return (2, 0, value)


def _f(value) -> Optional[float]:
    return float(value) if value is not None else None


def unit_volume_l(height, width, length) -> Optional[float]:
    if height is None or width is None or length is None:
        return None
    return float(height) * float(width) * float(length) / 1000.0


@dataclass
class LeafData:
    id: int
    address: str
    seg: tuple
    occupied: bool
    bins: list = field(default_factory=list)  # [(bin_type_id, qty, shelf_level_id|None)]
    items: list = field(default_factory=list)  # [(sku, name, qty, uvol_l, uweight)]
    mark: str = ""  # pré-visualização do lote: "new" ou o motivo do conflito
    generic: bool = False  # endereço genérico: as bins não ficam no próprio nível


# Pré-visualizações do cadastro em lote (em memória): token -> dados do lote
_PREVIEWS: dict[str, dict] = {}
_PREVIEW_MAX = 30


def store_preview(branch_id: int, warehouse_id: Optional[int], new_paths: list, removed_ids: set, conflicts: dict,
                  bins: list) -> str:
    """Guarda a simulação do lote para o mapa desenhar (endereços novos, removidos e em conflito)."""
    import uuid

    token = uuid.uuid4().hex
    _PREVIEWS[token] = {"branch_id": branch_id, "warehouse_id": warehouse_id, "new": list(new_paths),
                        "removed": set(removed_ids), "conflicts": dict(conflicts), "bins": list(bins)}
    while len(_PREVIEWS) > _PREVIEW_MAX:
        _PREVIEWS.pop(next(iter(_PREVIEWS)))
    return token


def get_preview(token: str) -> Optional[dict]:
    return _PREVIEWS.get(token or "")


def load_layout(db: Session, branch_id: int, warehouse_id: Optional[int], aisle: str = "",
                preview: Optional[dict] = None) -> dict:
    """Carrega o depósito e devolve um dicionário pronto para JSON.
    `preview` (cadastro em lote): desenha os endereços novos (marcados), sem os que seriam removidos, e destaca os
    existentes em conflito."""
    L = StorageLocation
    stmt = scope_filter(select(L.id, L.address, L.aisle, L.bay, L.level, L.position, L.compartment, L.is_occupied,
                               L.is_generic),
                        branch_id, warehouse_id)
    if aisle:
        stmt = stmt.where(_aisle_eq(L.aisle, aisle))
    count_stmt = scope_filter(select(func.count()).select_from(L), branch_id, warehouse_id)
    if aisle:
        count_stmt = count_stmt.where(_aisle_eq(L.aisle, aisle))
    total = db.scalar(count_stmt) or 0
    from services.app_settings import canon as _canon

    _first: dict[str, str] = {}
    for a in db.scalars(scope_filter(select(L.aisle).distinct(), branch_id, warehouse_id)).all():
        _first.setdefault(_canon(a), a)  # corredores equivalentes ("07 = 7") aparecem uma vez
    aisles_all = sorted(_first.values(), key=natural_key)
    pv_new = []
    if preview:
        pv_new = [(seg, addr) for seg, addr in preview["new"] if not aisle or seg[0] == aisle]
        total += len(pv_new) - len(preview["removed"])
        aisles_all = sorted(set(aisles_all) | {seg[0] for seg, _ in preview["new"]}, key=natural_key)
    if total > MAX_LAYOUT_LOCATIONS:
        return {"too_many": True, "total": total, "limit": MAX_LAYOUT_LOCATIONS, "aisles_all": aisles_all,
                "aisles": [], "bin_types": [], "stats": {}}

    leaves: dict[int, LeafData] = {}
    for r in db.execute(stmt):
        if preview and r.id in preview["removed"]:
            continue
        leaves[r.id] = LeafData(r.id, r.address, segments((r.aisle, r.bay, r.level, r.position, r.compartment)),
                                bool(r.is_occupied), generic=bool(r.is_generic))
    ids = list(leaves)

    # Tipos de bin (todos, para legenda e cores estáveis)
    bin_types = list(db.scalars(select(BinType).order_by(BinType.code)))
    bt_info = {}
    for i, bt in enumerate(bin_types):
        bt_info[bt.id] = {
            "id": bt.id, "code": bt.code, "name": bt.name, "color": BIN_PALETTE[i % len(BIN_PALETTE)],
            "w": _f(bt.width), "h": _f(bt.height), "d": _f(bt.length),
            "vol": _f(bt.volume), "max_weight": _f(bt.max_weight), "fw": footprint_width(bt),
            # compartimento: desenhado dentro da bin pai, com as divisórias
            "frac": ({"nx": bt.grid[0], "nz": bt.grid[1], "k": bt.per_parent, "grid": bt.grid_label,
                      "label": bt.fraction_label, "parent": bt.parent.code if bt.parent else "",
                      "pw": _f(bt.parent.width) if bt.parent else None, "ph": _f(bt.parent.height) if bt.parent else None}
                     if bt.is_fraction else None),
        }

    def phys_width(info: dict, qty: int) -> float:
        """Largura física de `qty` bins do tipo: compartimentos ocupam bins pai inteiras (2× 1/2 P = 1 P)."""
        fr = info.get("frac")
        if fr and fr.get("pw"):
            return -(-qty // fr["k"]) * fr["pw"]
        return (info.get("w") or 0) * qty

    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for lid, tid, qty, sl_id in db.execute(
            select(LocationBin.location_id, LocationBin.bin_type_id, LocationBin.quantity, LocationBin.shelf_level_id)
            .where(LocationBin.location_id.in_(chunk)).order_by(LocationBin.location_id, LocationBin.id)
        ):
            leaves[lid].bins.append((tid, qty, sl_id))
        for lid, sku, name, qty, h, w, ln, wt in db.execute(
            select(StockItem.location_id, Product.sku, Product.name, StockItem.quantity,
                   Product.height, Product.width, Product.length, Product.weight)
            .join(Product, Product.id == StockItem.product_id)
            .where(StockItem.location_id.in_(chunk)).order_by(StockItem.location_id, Product.sku)
        ):
            leaves[lid].items.append((sku, name, float(qty), unit_volume_l(h, w, ln), _f(wt)))
        # Produtos com este endereço como padrão (política de estoque da filial), sem registro de saldo
        from models import StockPolicy

        for lid, sku, name in db.execute(
            select(StockPolicy.default_location_id, Product.sku, Product.name)
            .join(Product, Product.id == StockPolicy.product_id).where(StockPolicy.default_location_id.in_(chunk))
        ):
            if not any(it[0] == sku for it in leaves[lid].items):
                leaves[lid].items.append((sku, name, None, None, None))

    if preview:
        for lf in leaves.values():
            if lf.address in preview["conflicts"]:
                lf.mark = preview["conflicts"][lf.address]
        for i, (seg, addr) in enumerate(pv_new):
            leaves[-(i + 1)] = LeafData(-(i + 1), addr, tuple(seg), False,
                                        bins=[(t, q, None) for t, q in preview["bins"]], mark="new")

    # ---- Níveis físicos (capacidade, reservados) ----
    sl_stmt = scope_filter_level(select(ShelfLevel), branch_id, warehouse_id)
    if aisle:
        sl_stmt = sl_stmt.where(_aisle_eq(ShelfLevel.aisle, aisle))
    shelf_levels = list(db.scalars(sl_stmt))
    # "07 = 7" (Configurações): códigos equivalentes viram o mesmo nó (com o 1º código visto como rótulo)
    from services.app_settings import canon, get

    seen: dict[tuple, str] = {}

    def norm(seg) -> tuple:
        if not get("addr_zero_eq"):
            return tuple(seg)
        out, path = [], ()
        for v in seg:
            path = path + (canon(v),)
            out.append(seen.setdefault(path, v))
        return tuple(out)

    for lf in leaves.values():
        lf.seg = norm(lf.seg)
    sl_key = {sl.id: norm((sl.aisle, sl.bay, sl.level)) for sl in shelf_levels}
    levels_info: dict[tuple, dict] = {}

    # ---- Estruturas de armazenagem das estantes ----
    from services.structure_service import bay_structures

    bay_st = {k: {"code": st.code, "name": st.name, "width": float(st.width), "depth": float(st.depth),
                  "height": float(st.height), "levels": st.levels, "level_h": float(st.level_height)}
              for k, st in ((norm(k), st) for k, st in bay_structures(db, branch_id, warehouse_id).items())
              if not aisle or k[0] == aisle}

    def linfo(key: tuple) -> dict:
        return levels_info.setdefault(key, {"sl_id": None, "notes": "", "caps": {}, "used": {}, "extra": [],
                                            "height": None})

    for sl in shelf_levels:
        li = linfo(sl_key[sl.id])
        li["sl_id"], li["notes"] = sl.id, sl.notes
        li["height"] = float(sl.height_cm) if sl.height_cm is not None else None
    if sl_key:
        for lid, tid, mx in db.execute(select(LevelCapacity.shelf_level_id, LevelCapacity.bin_type_id,
                                              LevelCapacity.max_bins)
                                       .where(LevelCapacity.shelf_level_id.in_(list(sl_key)))):
            linfo(sl_key[lid])["caps"][tid] = mx
    # Bins de níveis extras que pertencem a endereços FORA do filtro de corredor não ocorrem
    # (nível extra é sempre da mesma estante, logo do mesmo corredor).

    # ---- Monta a árvore ----
    root: dict = {"children": {}}
    for leaf in leaves.values():
        node = root
        for depth, s in enumerate(leaf.seg):
            node = node["children"].setdefault(s, {"code": s, "depth": depth, "children": {}})
        node["leaf"] = leaf
        own_key = leaf.seg[:3] if len(leaf.seg) >= 3 and not leaf.generic else None  # genérico: não usa o próprio nível
        for tid, qty, sl_id in leaf.bins:
            target = sl_key.get(sl_id) if sl_id else own_key
            if target is None:
                continue  # endereço acima do nível (corredor/estante): não ocupa nível
            li = linfo(target)
            li["used"][tid] = li["used"].get(tid, 0) + qty
            if sl_id and target != own_key:
                info = bt_info.get(tid) or {}
                li["extra"].append({"owner_id": leaf.id, "owner": leaf.address, "type": tid, "qty": qty,
                                    "w": phys_width(info, qty) or DEFAULT_SIZE_CM[0] / 2,
                                    "h": info.get("h") or DEFAULT_SIZE_CM[1], "dims": bool(info.get("w"))})
    # Níveis reservados (sem endereços) também entram na árvore
    for key in levels_info:
        node = root
        for depth, s in enumerate(key):
            created = s not in node["children"]
            node = node["children"].setdefault(s, {"code": s, "depth": depth, "children": {}})
            if depth == 2 and created:
                node["reserved"] = True

    stats = {"locations": len(leaves), "occupied": 0, "no_dims": 0, "with_bins": 0,
             "bin_volume_l": 0.0, "used_volume_l": 0.0, "over": 0, "levels_over": 0, "extra_blocks": 0,
             "levels_tall": 0}

    def leaf_json(leaf: LeafData) -> dict:
        bins = []
        width = height = depth = 0.0
        known_w = True
        cap_vol = 0.0
        cap_w = 0.0
        cap_w_known = True
        n_bins = 0
        extra = []
        own_level = leaf.seg[2] if len(leaf.seg) >= 3 and not leaf.generic else None
        for tid, qty, sl_id in leaf.bins:
            info = bt_info.get(tid)
            if not info:
                continue
            n_bins += qty
            target = sl_key.get(sl_id)
            if sl_id and target and target[2] != own_level:
                extra.append({"type": tid, "qty": qty, "level": target[2]})
            else:
                bins.append({"type": tid, "qty": qty})
                if info["w"] is None or info["h"] is None:
                    known_w = False
                else:
                    width += phys_width(info, qty)
                    height = max(height, (info["frac"] or {}).get("ph") or info["h"])
                    depth = max(depth, info["d"] or 0.0)
            if info["vol"] is not None:
                cap_vol += info["vol"] * qty
            if info["max_weight"] is not None:
                cap_w += info["max_weight"] * qty
            else:
                cap_w_known = False
        has_dims = bool(bins) and known_w and width > 0 and height > 0
        if not has_dims:
            width, height, depth = DEFAULT_SIZE_CM
            if not extra:
                stats["no_dims"] += 1
            else:
                width = DEFAULT_SIZE_CM[0] / 2  # todas as bins estão em outro nível: marcador estreito
        if bins or extra:
            stats["with_bins"] += 1
        if leaf.occupied:
            stats["occupied"] += 1

        # Enchimento
        used_vol = 0.0
        used_w = 0.0
        vol_complete = True
        w_complete = True
        items = []
        for sku, name, qty, uvol, uw in leaf.items:
            items.append({"sku": sku, "name": name, "qty": qty})
            if qty is None or qty <= 0:
                continue
            if uvol is None:
                vol_complete = False
            else:
                used_vol += qty * uvol
            if uw is None:
                w_complete = False
            else:
                used_w += qty * uw
        fill_vol = (used_vol / cap_vol * 100.0) if cap_vol > 0 and used_vol > 0 else (0.0 if cap_vol > 0 else None)
        fill_w = (used_w / cap_w * 100.0) if cap_w > 0 and cap_w_known else None
        fill = None
        if fill_vol is not None or fill_w is not None:
            fill = max(v for v in (fill_vol, fill_w) if v is not None)
        if cap_vol > 0:
            stats["bin_volume_l"] += cap_vol
            stats["used_volume_l"] += used_vol
        if fill is not None and fill > 100:
            stats["over"] += 1
        return {
            "id": leaf.id, "address": leaf.address, "occupied": leaf.occupied,
            "w": round(width, 2), "h": round(height, 2), "d": round(depth, 2), "dims": has_dims,
            "bins": bins, "extra": extra, "n_bins": n_bins, "items": items[:30], "n_items": len(items),
            "fill": None if fill is None else round(fill, 1),
            "fill_vol": None if fill_vol is None else round(fill_vol, 1),
            "fill_w": None if fill_w is None else round(fill_w, 1),
            "fill_partial": not (vol_complete and w_complete),
            "cap_vol": round(cap_vol, 2), "used_vol": round(used_vol, 2), "mark": leaf.mark,
        }

    def level_json(key: tuple, reserved: bool) -> dict:
        li = levels_info.get(key) or {"sl_id": None, "notes": "", "caps": {}, "used": {}, "extra": [], "height": None}
        caps, used = dict(li["caps"]), li["used"]
        for t, info in bt_info.items():  # compartimentos: capacidade da bin pai × compartimentos por pai
            fr = info.get("frac")
            if fr and t not in caps:
                pid = next((p for p, pi in bt_info.items() if pi["code"] == fr["parent"]), None)
                if pid in caps:
                    caps[t] = caps[pid] * fr["k"]
        unknown = [t for t, n in used.items() if n and t not in caps]
        fraction = None
        if caps and not unknown:
            fraction = sum(n / caps[t] for t, n in used.items() if n)
        widths = [caps[t] * bt_info[t]["fw"] for t in caps if t in bt_info and bt_info[t]["fw"]]
        st = bay_st.get(key[:2])
        if st:  # a frente útil do módulo é a largura do nível
            widths = [st["width"]]
        heights = [bt_info[t]["h"] for t in caps if t in bt_info and bt_info[t]["h"]]
        if fraction is not None and fraction > 1.0 + 1e-9:
            stats["levels_over"] += 1
        if li["extra"]:
            stats["extra_blocks"] += len(li["extra"])
        tall = []
        if li["height"] is not None:
            tall = [bt_info[t]["code"] for t, n in used.items()
                    if n and t in bt_info and (bt_info[t]["h"] or 0) > li["height"] + 1e-9]
            if tall:
                stats["levels_tall"] += 1
        return {"sl_id": li["sl_id"], "notes": li["notes"], "reserved": reserved,
                "caps": {str(t): m for t, m in caps.items()}, "used": {str(t): n for t, n in used.items() if n},
                "fraction": None if fraction is None else round(fraction, 4), "unknown": unknown,
                "usable_w": max(widths) if widths else None, "usable_h": max(heights) if heights else None,
                "height": li["height"] if li["height"] is not None else (st["level_h"] if st else None),
                "tall": tall, "extra": li["extra"]}

    def to_json(node: dict, path: tuple = ()) -> dict:
        out = {"code": node["code"], "depth": node["depth"]}
        path = path + (node["code"],)
        if node["depth"] == 2:
            out["lvl"] = level_json(path, bool(node.get("reserved")))
        if node["depth"] == 1 and path[:2] in bay_st:
            out["struct"] = bay_st[path[:2]]
        if "leaf" in node:
            out["leaf"] = leaf_json(node["leaf"])
        kids = sorted(node["children"].values(), key=lambda n: natural_key(n["code"]))
        if kids:
            out["children"] = [to_json(k, path) for k in kids]
        return out

    aisles = [to_json(n) for n in sorted(root["children"].values(), key=lambda n: natural_key(n["code"]))]
    stats["free"] = stats["locations"] - stats["occupied"]
    stats["fill_avg"] = (round(stats["used_volume_l"] / stats["bin_volume_l"] * 100.0, 1)
                         if stats["bin_volume_l"] else None)
    stats["bin_volume_l"] = round(stats["bin_volume_l"], 1)
    stats["used_volume_l"] = round(stats["used_volume_l"], 1)
    return {"too_many": False, "total": total, "aisles_all": aisles_all, "aisles": aisles,
            "bin_types": list(bt_info.values()), "stats": stats, "default_size": DEFAULT_SIZE_CM,
            "level_names": LEVEL_NAMES}

