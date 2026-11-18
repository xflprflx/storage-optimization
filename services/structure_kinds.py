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

PER = {"module": N_("por módulo"), "level": N_("por nível")}

# padrões da primeira inicialização (o usuário edita): peças típicas, sem custo
DEFAULT_PIECES = (("MONT-PP", N_("Quadro de montantes (porta-palete)")), ("LONG-PP", N_("Longarina (porta-palete)")),
                  ("COL-EST", N_("Coluna de estante")), ("PRAT-EST", N_("Prateleira de estante")),
                  ("COL-CANT", N_("Coluna de cantilever")), ("BRACO-CANT", N_("Braço de cantilever")),
                  ("PISTA-ROL", N_("Pista de roletes (flow rack)")))
DEFAULT_KINDS = (  # nome, paletes no piso, [(peça, qtd, por)]
    (N_("Estante"), False, [("COL-EST", 4, "module"), ("PRAT-EST", 1, "level")]),
    ("Porta-palete", False, [("MONT-PP", 2, "module"), ("LONG-PP", 2, "level")]),
    (N_("Palete no chão"), True, []),
    (N_("Flow rack"), False, [("COL-EST", 4, "module"), ("PISTA-ROL", 1, "level")]),
    (N_("Cantilever"), False, [("COL-CANT", 2, "module"), ("BRACO-CANT", 2, "level")]),
    (N_("Mezanino"), False, []),
    (N_("Outro"), False, []),
)


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
            k = StructureKind(name=_(name), floor=floor, notes="")
            k.pieces = [StructureKindPiece(piece_id=pieces[c].id, qty=Decimal(q), per=per) for c, q, per in comp]
            db.add(k)
        db.flush()
    by_name = {k.name.strip().casefold(): k for k in db.scalars(select(StructureKind))}
    changed = False
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
    """[(peça, qtd, "module"|"level")] do tipo."""
    return [(p.piece_id, float(p.qty), p.per) for p in (kind.pieces if kind is not None else [])]


def pieces_for(comp: list[tuple[int, float, str]], levels: int, modules: int = 1) -> dict[int, float]:
    """Peças de `modules` módulos com `levels` níveis."""
    out: dict[int, float] = defaultdict(float)
    for pid, q, per in comp:
        out[pid] += q * modules * (levels if per == "level" else 1)
    return dict(out)
