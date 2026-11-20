"""Tipos de estrutura e peças: cadastro, composição e quantidades de peças por estrutura.

O tipo (Estante, Porta-palete…) é cadastrado pelo usuário com a composição de peças: `qty` por módulo (montantes,
colunas) ou por nível (longarinas, prateleiras, braços). Uma estrutura com N níveis usa
    peças = Σ qty(por módulo) + Σ qty(por nível) × N.
A mesma peça pode estar em vários tipos: na recomposição, as peças liberadas de uma estrutura desmontada servem para
montar outra.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import StructureKind, StructureKindPiece, StructurePiece, StructureType
from services.i18n import N_, _

PER = {"module": N_("por módulo"), "side": N_("por lateral"), "level": N_("por nível")}
CONDITIONS = {"": N_("Ambiente"), "refrigerado": N_("Refrigerado"), "congelado": N_("Congelado"),
              "controlado": N_("Controlado (seguro)")}
DOOR_TYPES = {"": N_("Sem porta"), "swing": N_("De giro (abre para o corredor)"), "sliding": N_("De correr")}
HINGES = {"left": N_("Dobradiça à esquerda"), "right": N_("Dobradiça à direita")}
DOOR_PASSAGE_CM = 80.0  # passagem mínima no corredor com a porta aberta (cm)

# padrões da primeira inicialização (o usuário edita): peças típicas, sem custo
DEFAULT_PIECES = (("MONT-PP", N_("Quadro de montantes (porta-palete)")), ("LONG-PP", N_("Longarina (porta-palete)")),
                  ("COL-EST", N_("Coluna de estante")), ("PRAT-EST", N_("Prateleira de estante")),
                  ("COL-CANT", N_("Coluna de cantilever")), ("BRACO-CANT", N_("Braço de cantilever")),
                  ("PISTA-ROL", N_("Pista de roletes (flow rack)")))
DEFAULT_KINDS = (  # nome, paletes no piso, [(peça, qtd, por)] — "side" = por lateral (vizinhos compartilham)
    (N_("Estante"), False, [("COL-EST", 2, "side"), ("PRAT-EST", 1, "level")]),
    ("Porta-palete", False, [("MONT-PP", 1, "side"), ("LONG-PP", 2, "level")]),
    (N_("Palete no chão"), True, []),
    (N_("Flow rack"), False, [("COL-EST", 2, "side"), ("PISTA-ROL", 1, "level")]),
    (N_("Cantilever"), False, [("COL-CANT", 1, "side"), ("BRACO-CANT", 2, "level")]),
    (N_("Mezanino"), False, []),
    (N_("Outro"), False, []),
)
SHARED_DEFAULT = {"Estante", "Porta-palete", "Flow rack", "Cantilever"}  # tipos padrão com a lateral compartilhada
# composição padrão de versões anteriores (lateral contada por módulo): migrada para "por lateral" se não foi mexida
OLD_DEFAULTS = {(("COL-EST", 4.0, "module"), ("PRAT-EST", 1.0, "level")): ("COL-EST", 2),
                (("MONT-PP", 2.0, "module"), ("LONG-PP", 2.0, "level")): ("MONT-PP", 1),
                (("COL-EST", 4.0, "module"), ("PISTA-ROL", 1.0, "level")): ("COL-EST", 2),
                (("COL-CANT", 2.0, "module"), ("BRACO-CANT", 2.0, "level")): ("COL-CANT", 1)}


def refresh_floor(db: Session) -> None:
    """Nomes dos tipos "paletes no piso" para o desenho e o cálculo (site_planner.is_floor)."""
    from services import site_planner

    site_planner.FLOOR_NAMES = {n.strip().casefold() for n in db.scalars(select(StructureKind.name)
                                                                        .where(StructureKind.floor.is_(True)))}


def ensure_kinds(db: Session) -> None:
    """Primeira inicialização: peças e tipos padrão. Sempre: um tipo para cada nome de tipo usado pelas estruturas,
    o vínculo estrutura -> tipo pelo nome e a aparência 3D antiga (da estrutura) levada para o tipo."""
    if not db.scalar(select(func.count()).select_from(StructureKind)):
        pieces = {}
        for code, name in DEFAULT_PIECES:
            pc = db.scalar(select(StructurePiece).where(StructurePiece.code == code))
            if pc is None:
                pc = StructurePiece(code=code, name=_(name), notes="")
                db.add(pc)
                db.flush()
            pieces[code] = pc
        for name, floor, comp in DEFAULT_KINDS:
            k = StructureKind(name=_(name), floor=floor, notes="", shared_sides=name in SHARED_DEFAULT)
            k.pieces = [StructureKindPiece(piece_id=pieces[c].id, qty=Decimal(q), per=per) for c, q, per in comp]
            db.add(k)
        db.flush()
    by_name = {k.name.strip().casefold(): k for k in db.scalars(select(StructureKind))}
    changed = _migrate_sides(db)
    for st in db.scalars(select(StructureType)):
        key = (st.kind or "").strip().casefold()
        if not key:
            continue
        k = by_name.get(key)
        if k is None:  # tipo escrito nas estruturas antigas e ainda sem cadastro
            from services.site_planner import FLOOR_KINDS

            k = StructureKind(name=st.kind.strip()[:40], floor=any(key.startswith(f) for f in FLOOR_KINDS), notes="")
            db.add(k)
            db.flush()
            by_name[key] = k
        if st.kind_id is None:
            st.kind_id, changed = k.id, True
        if st.look_model and not k.look_model:  # aparência 3D era da estrutura: vai para o tipo
            k.look_model, k.appearance, changed = st.look_model, st.appearance, True
    if changed or db.new:
        db.commit()
    refresh_floor(db)


def _migrate_sides(db: Session) -> bool:
    """Tipos padrão de versões anteriores, com a composição intacta: as colunas / quadros de montantes passam a ser
    "por lateral" (metade da quantidade por módulo) e os vizinhos compartilham a lateral."""
    codes = {p.id: p.code for p in db.scalars(select(StructurePiece))}
    changed = False
    for k in db.scalars(select(StructureKind).where(StructureKind.shared_sides.is_(False))):
        sig = tuple(sorted((codes.get(p.piece_id, ""), float(p.qty), p.per) for p in k.pieces))
        hit = next((v for old, v in OLD_DEFAULTS.items() if tuple(sorted(old)) == sig), None)
        if hit is None:
            continue
        for p in k.pieces:
            if codes.get(p.piece_id) == hit[0] and p.per == "module":
                p.per, p.qty = "side", Decimal(hit[1])
        k.shared_sides = changed = True
    return changed


def attach_kind(db: Session, st: StructureType) -> None:
    """Liga a estrutura ao tipo cadastrado pelo nome escrito em `st.kind` (importações): cria o tipo se não existir."""
    name = (st.kind or "").strip()[:40]
    if not name:
        st.kind_id = None
        return
    k = db.scalar(select(StructureKind).where(func.lower(StructureKind.name) == name.lower()))
    if k is None:
        from services.site_planner import FLOOR_KINDS

        k = StructureKind(name=name, floor=any(name.casefold().startswith(f) for f in FLOOR_KINDS), notes="")
        db.add(k)
        db.flush()
        refresh_floor(db)
    st.kind_id, st.kind = k.id, k.name


def kinds(db: Session) -> list[StructureKind]:
    return list(db.scalars(select(StructureKind).order_by(StructureKind.name)))


def kind_of(db: Session, st: StructureType) -> Optional[StructureKind]:
    if st.kind_id:
        return db.get(StructureKind, st.kind_id)
    if st.kind:
        return db.scalar(select(StructureKind).where(func.lower(StructureKind.name) == st.kind.strip().lower()))
    return None


def composition(kind: Optional[StructureKind]) -> list[tuple[int, float, str]]:
    """[(peça, qtd, "module"|"side"|"level")] do tipo."""
    return [(p.piece_id, float(p.qty), p.per) for p in (kind.pieces if kind is not None else [])]


def pieces_for(comp: list[tuple[int, float, str]], levels: int, modules: int = 1, sides: Optional[int] = None) -> dict[int, float]:
    """Peças de `modules` módulos com `levels` níveis. As peças "por lateral" contam `sides` laterais (padrão: 2 por
    módulo, sem vizinho; numa sequência de módulos encostados que compartilham a lateral, módulos + 1)."""
    out: dict[int, float] = defaultdict(float)
    sides = 2 * modules if sides is None else sides
    for pid, q, per in comp:
        out[pid] += q * (levels * modules if per == "level" else sides if per == "side" else modules)
    return dict(out)


def side_pieces(comp: list[tuple[int, float, str]], sides: float = 1) -> dict[int, float]:
    """Só as peças "por lateral" de `sides` laterais."""
    return {pid: q * sides for pid, q, per in comp if per == "side"}


def own_pieces(comp: list[tuple[int, float, str]], levels: int) -> dict[int, float]:
    """As peças de um módulo sem as laterais (por módulo + por nível)."""
    return {pid: v for pid, v in pieces_for([c for c in comp if c[2] != "side"], levels).items()}


def location_conditions(db: Session, location_ids) -> dict[int, str]:
    """Endereço -> condição de armazenagem da estrutura da estante dele ("" = ambiente ou sem estrutura)."""
    from models import BayStructure, StorageLocation

    ids = sorted(set(location_ids))
    if not ids:
        return {}
    conds = {k.id: (k.condition or "") for k in db.scalars(select(StructureKind)) if k.condition}
    if not conds:
        return {i: "" for i in ids}
    out: dict[int, str] = {i: "" for i in ids}
    for i in range(0, len(ids), 900):
        rows = db.execute(select(StorageLocation.id, StructureType.kind_id)
                          .join(BayStructure, (BayStructure.branch_id == StorageLocation.branch_id)
                                & (BayStructure.aisle == StorageLocation.aisle) & (BayStructure.bay == StorageLocation.bay)
                                & ((BayStructure.warehouse_id == StorageLocation.warehouse_id)
                                   | (BayStructure.warehouse_id.is_(None) & StorageLocation.warehouse_id.is_(None))))
                          .join(StructureType, StructureType.id == BayStructure.structure_type_id)
                          .where(StorageLocation.id.in_(ids[i:i + 900])))
        for lid, kid in rows:
            out[lid] = conds.get(kid, "")
    return out


def product_conditions(db: Session, product_ids) -> dict[int, str]:
    from models import Product

    ids = sorted(set(product_ids))
    out: dict[int, str] = {}
    for i in range(0, len(ids), 900):
        out.update({pid: c for pid, c in db.execute(select(Product.id, Product.storage_condition)
                                                     .where(Product.id.in_(ids[i:i + 900]))).all()})
    return {k: (v or "") for k, v in out.items()}
