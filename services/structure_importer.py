"""
Importação das estruturas via CSV/XLSX (mesmo fluxo dos outros cadastros-base, services/master_importer):
  - Peças de estrutura      (código, nome, custo)
  - Tipos de estrutura      (nome, paletes no piso, furação, espessura, condição, porta, modelo 3D) + composição:
                            cada linha com Peça/Qtd/Por acrescenta uma peça ao tipo (várias linhas do mesmo tipo)
  - Estruturas              (código, nome, tipo, medidas, níveis, vãos, 1º nível, carga, custo, zona de ouro)
  - Estrutura das estantes  (filial, armazém, corredor, estante — aceitam faixas —, estrutura, folga, 1º nível)

Regras comuns: linhas com erro são listadas e não interrompem o arquivo; campos vazios não apagam dados.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import BayStructure, StructureKind, StructureKindPiece, StructurePiece, StructureType
from services.addressing import normalize_code
from services.file_reader import get_cell
from services.i18n import N_, _
from services.master_importer import ALL_WORDS, MasterOptions, MasterResult, _dec, _run, _sync

YES = {"s", "sim", "1", "x", "true", "verdadeiro", "yes", "y", "si", "sí", "oui", "ja", "是"}
NO = {"n", "nao", "não", "0", "false", "falso", "no", "non", "nein", "否"}


def _plain(text) -> str:
    t = unicodedata.normalize("NFD", str(text or "").strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _yes(row, mapping, key) -> Optional[bool]:
    """sim/não da planilha (vazio = não informado)."""
    raw = str(get_cell(row, mapping.get(key)) or "").strip().casefold()
    if not raw:
        return None
    if raw in YES or _plain(raw) in YES:
        return True
    if raw in NO or _plain(raw) in NO:
        return False
    raise ValueError(_("Valor sim/não inválido: {raw}", raw=f"{raw!r}"))


def _int(row, mapping, key, label) -> Optional[int]:
    v = _dec(row, mapping, key, label, allow_zero=False)
    if v is None:
        return None
    if v != int(v):
        raise ValueError(_("{label}: informe um número inteiro ({raw}).", label=label, raw=f"{v:g}"))
    return int(v)


# --------------------------------------------------------------------------- #
# Peças de estrutura
# --------------------------------------------------------------------------- #
PIECE_FIELDS = {
    "code": (N_("Código da peça"), True, ("codigo", "código", "cod", "peca", "peça", "codigodapeca", "code", "piece")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao", "descrição")),
    "cost": (N_("Custo por peça (R$)"), False, ("custo", "custor", "custoporpeca", "custoporpecar", "preco", "preço", "valor", "cost")),
    "notes": (N_("Observação"), False, ("observacao", "observação", "obs", "notas", "notes")),
}


def import_pieces(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    res = MasterResult(counters={N_("Peças criadas"): 0, N_("Peças atualizadas"): 0})
    by_code = {p.code: p for p in db.scalars(select(StructurePiece))}
    seen: set[str] = set()

    def handle(line, row):
        code = normalize_code(get_cell(row, mapping.get("code")))
        if not code:
            raise ValueError(_("Código vazio"))
        if len(code) > 30:
            raise ValueError(_("Código com mais de 30 caracteres"))
        name = str(get_cell(row, mapping.get("name")) or "").strip()[:150]
        cost = _dec(row, mapping, "cost", PIECE_FIELDS["cost"][0])
        notes = str(get_cell(row, mapping.get("notes")) or "").strip()[:200]
        seen.add(code)
        pc = by_code.get(code)
        if pc is None:
            pc = StructurePiece(code=code, name=name or code, cost=cost, notes=notes)
            db.add(pc)
            db.flush()
            by_code[code] = pc
            res.inc(N_("Peças criadas"))
        elif opts.update_existing:
            changed = False
            for k, v in (("name", name), ("cost", cost), ("notes", notes)):
                if v not in (None, "") and getattr(pc, k) != v:
                    setattr(pc, k, v)
                    changed = True
            if changed:
                res.inc(N_("Peças atualizadas"))

    _run(db, path, opts, res, handle)

    def do():
        used = {pid for (pid,) in db.execute(select(StructureKindPiece.piece_id).distinct())}
        out = kept = 0
        for code, pc in list(by_code.items()):
            if code in seen:
                continue
            if pc.id in used:
                kept += 1
                continue
            db.delete(pc)
            out += 1
        res.counters[N_("Peças excluídas")] = out
        if kept:
            res.notes.append(_("{n} peça(s) fora do arquivo mantida(s): estão na composição de algum tipo de estrutura.", n=kept))
        db.commit()
    _sync(res, opts, do)
    return res


# --------------------------------------------------------------------------- #
# Tipos de estrutura (+ composição de peças)
# --------------------------------------------------------------------------- #
KIND_FIELDS = {
    "name": (N_("Tipo de estrutura"), True, ("tipodeestrutura", "tipo", "nome", "nomedotipo", "name", "kind")),
    "floor": (N_("Paletes no piso (sim/não)"), False, ("paletesnopiso", "nopiso", "blocado", "floor")),
    "pitch_cm": (N_("Passo da furação (cm)"), False, ("passodafuracao", "passodafuraçao", "passo", "furacao", "pitch")),
    "beam_cm": (N_("Espessura do nível (cm)"), False, ("espessuradonivel", "espessura", "beam")),
    "condition": (N_("Condição de armazenagem"), False, ("condicaodearmazenagem", "condicao", "condição", "temperatura", "condition")),
    "door": (N_("Porta (sem / de giro / de correr)"), False, ("porta", "tipodeporta", "door")),
    "door_leaves": (N_("Folhas da porta"), False, ("folhas", "folhasdaporta", "leaves")),
    "door_hinge": (N_("Dobradiça (esquerda / direita)"), False, ("dobradica", "dobradiça", "hinge")),
    "door_angle": (N_("Abertura da porta (°)"), False, ("abertura", "aberturadaporta", "angulo", "ângulo", "angle")),
    "door_time": (N_("Tempo da porta (s)"), False, ("tempodaporta", "tempodaportas", "tempoporta", "doortime")),
    "look": (N_("Modelo 3D"), False, ("modelo3d", "modelo", "aparencia", "aparência", "look")),
    "piece": (N_("Peça (código)"), False, ("peca", "peça", "codigodapeca", "piece")),
    "qty": (N_("Qtd da peça"), False, ("qtd", "quantidade", "qtddapeca", "qty")),
    "per": (N_("Por (módulo / nível)"), False, ("por", "pormodulonivel", "per")),
    "color": (N_("Cor no 3D (#rrggbb)"), False, ("cor", "cor3d", "corno3d", "cordapeca", "color")),
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _look_code(text: str) -> Optional[str]:
    """Modelo 3D da planilha: "" (automático), "simple" ou uma chave/nome da biblioteca de estruturas."""
    from services.appearance import LIBRARY_STRUCTS
    from services.i18n import lookup

    t = _plain(text)
    if not t:
        return None
    if t in ("auto", "automatico", "automatic"):
        return ""
    if t in ("simples", "simple", "desenhosimples"):
        return "simple"
    for key, (label, _fn) in LIBRARY_STRUCTS.items():
        if t in (key, _plain(label), _plain(lookup(label))) or _plain(label).startswith(t):
            return key
    raise ValueError(_("Modelo 3D desconhecido: {raw} (use automático, simples ou: {keys})", raw=f"{text!r}",
                       keys=", ".join(LIBRARY_STRUCTS)))


def import_kinds(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    from services.site_import import condition_code, door_code
    from services.structure_kinds import refresh_floor

    res = MasterResult(counters={N_("Tipos criados"): 0, N_("Tipos atualizados"): 0, N_("Peças na composição"): 0})
    by_name = {k.name.strip().casefold(): k for k in db.scalars(select(StructureKind))}
    pieces = {p.code: p for p in db.scalars(select(StructurePiece))}
    comp: dict[int, list] = {}  # tipo -> [(peça, qtd, por, cor)] do arquivo (substitui a composição)
    seen: set[int] = set()  # tipos já contados como atualizados
    created: set[int] = set()
    labels = {k: v[0] for k, v in KIND_FIELDS.items()}

    def handle(line, row):
        name = str(get_cell(row, mapping.get("name")) or "").strip()
        if not name:
            raise ValueError(_("Tipo de estrutura vazio"))
        if len(name) > 40:
            raise ValueError(_("Nome do tipo com mais de 40 caracteres"))
        vals: dict = {}
        fl = _yes(row, mapping, "floor")
        if fl is not None:
            vals["floor"] = fl
        for k in ("pitch_cm", "beam_cm"):
            v = _dec(row, mapping, k, labels[k], allow_zero=False)
            if v is not None:
                vals[k] = v
        cond = str(get_cell(row, mapping.get("condition")) or "").strip()
        if cond:
            vals["condition"] = condition_code(cond)
        door = door_code(str(get_cell(row, mapping.get("door")) or ""))
        if door is not None:
            vals["door_type"] = door
        lv = _int(row, mapping, "door_leaves", labels["door_leaves"])
        if lv is not None:
            if lv not in (1, 2):
                raise ValueError(_("Folhas da porta: 1 ou 2."))
            vals["door_leaves"] = lv
        hg = _plain(get_cell(row, mapping.get("door_hinge")))
        if hg:
            vals["door_hinge"] = "right" if hg.startswith(("dir", "right", "dr", "d")) else "left"
        ang = _int(row, mapping, "door_angle", labels["door_angle"])
        if ang is not None:
            if ang not in (90, 180):
                raise ValueError(_("Abertura da porta: 90 ou 180°."))
            vals["door_angle"] = ang
        tm = _dec(row, mapping, "door_time", labels["door_time"])
        if tm is not None:
            vals["door_time_s"] = tm
        look = _look_code(str(get_cell(row, mapping.get("look")) or ""))
        if look is not None:
            vals["look_model"] = look
        # composição: a linha com peça acrescenta a peça ao tipo
        pcode = normalize_code(get_cell(row, mapping.get("piece")))
        line_comp = None
        if pcode:
            qty = _dec(row, mapping, "qty", labels["qty"], allow_zero=False)
            if qty is None:
                raise ValueError(_("Informe a quantidade da peça {code}.", code=pcode))
            per = "level" if _plain(get_cell(row, mapping.get("per"))).startswith(("niv", "lev", "ebe", "nive", "层")) else "module"
            color = str(get_cell(row, mapping.get("color")) or "").strip() or None
            if color and not HEX.match(color):
                raise ValueError(_("Cor no 3D inválida: {raw} (use #rrggbb).", raw=f"{color!r}"))
            line_comp = (pcode, qty, per, color.lower() if color else None)
        k = by_name.get(name.casefold())
        if k is None:
            k = StructureKind(name=name, notes="", **vals)
            db.add(k)
            db.flush()
            by_name[name.casefold()] = k
            created.add(k.id)
            res.inc(N_("Tipos criados"))
        elif opts.update_existing:  # as linhas seguintes do mesmo tipo também podem completar campos
            changed = [f for f, v in vals.items() if getattr(k, f) != v]
            for f in changed:
                setattr(k, f, vals[f])
            if changed and k.id not in seen and k.id not in created:
                res.inc(N_("Tipos atualizados"))
                seen.add(k.id)
        elif k.id not in created:
            return  # tipo existente sem "atualizar": nem os campos nem a composição mudam
        if line_comp is not None:
            pc = pieces.get(line_comp[0])
            if pc is None:  # peça nova: cadastrada com o código (complete o nome em Peças de estrutura)
                pc = StructurePiece(code=line_comp[0], name=line_comp[0], notes="")
                db.add(pc)
                db.flush()
                pieces[pc.code] = pc
                res.notes.append(_("Peça {code} cadastrada com o código como nome.", code=pc.code))
            comp.setdefault(k.id, []).append((pc.id, line_comp[1], line_comp[2], line_comp[3]))

    _run(db, path, opts, res, handle)
    if comp:
        for kid, lines in comp.items():
            k = db.get(StructureKind, kid)
            if k is None:
                continue
            merged: dict[tuple, list] = {}
            for pid, qty, per, color in lines:  # mesma peça e mesmo "por" em várias linhas: soma
                m = merged.setdefault((pid, per), [Decimal(0), color])
                m[0] += qty
                m[1] = m[1] or color
            k.pieces = [StructureKindPiece(piece_id=pid, qty=q, per=per, color=c) for (pid, per), (q, c) in merged.items()]
            res.inc(N_("Peças na composição"), len(merged))
        db.commit()
    refresh_floor(db)
    return res


# --------------------------------------------------------------------------- #
# Estruturas de armazenagem
# --------------------------------------------------------------------------- #
STRUCT_FIELDS = {
    "code": (N_("Código da estrutura"), True, ("codigo", "código", "cod", "estrutura", "codigodaestrutura", "code")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao", "descrição")),
    "kind": (N_("Tipo de estrutura"), False, ("tipo", "tipodeestrutura", "kind")),
    "width": (N_("Frente (cm)"), False, ("frente", "frentecm", "frentedomodulo", "largura", "width")),
    "depth": (N_("Profundidade (cm)"), False, ("profundidade", "profundidadecm", "depth")),
    "height": (N_("Altura (cm)"), False, ("altura", "alturacm", "alturatotal", "height")),
    "levels": (N_("Níveis"), False, ("niveis", "níveis", "numerodeniveis", "levels")),
    "level_height": (N_("Vão útil (cm)"), False, ("vaoutil", "vãoútil", "vaoutilcm", "vao", "vão", "levelheight")),
    "level_heights": (N_("Vãos por nível (cm)"), False, ("vaospornivel", "vãospornível", "vaospornivelcm", "vaos", "levelheights")),
    "first_level_cm": (N_("Altura do 1º nível (cm)"), False, ("alturado1onivel", "alturado1nivel", "1onivel", "primeironivel", "firstlevel")),
    "max_load": (N_("Carga por nível (kg)"), False, ("cargapornivel", "cargapornivelkg", "carga", "maxload")),
    "cost": (N_("Custo por módulo (R$)"), False, ("custopormodulo", "custopormodulor", "custo", "cost")),
    "golden_from": (N_("Zona de ouro início (cm)"), False, ("zonadeouroinicio", "zonadeouroiniciocm", "ouroinicio", "goldenfrom")),
    "golden_to": (N_("Zona de ouro fim (cm)"), False, ("zonadeourofim", "zonadeourofimcm", "ourofim", "goldento")),
}


def import_structures(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    from services.structure_kinds import attach_kind
    from services.structure_service import recalc_capacities

    res = MasterResult(counters={N_("Estruturas criadas"): 0, N_("Estruturas atualizadas"): 0})
    by_code = {s.code: s for s in db.scalars(select(StructureType))}
    labels = {k: v[0] for k, v in STRUCT_FIELDS.items()}
    touched: set[int] = set()

    def handle(line, row):
        code = normalize_code(get_cell(row, mapping.get("code")))
        if not code:
            raise ValueError(_("Código vazio"))
        if len(code) > 30:
            raise ValueError(_("Código com mais de 30 caracteres"))
        name = str(get_cell(row, mapping.get("name")) or "").strip()[:150]
        kind = str(get_cell(row, mapping.get("kind")) or "").strip()[:40]
        vals = {k: _dec(row, mapping, k, labels[k], allow_zero=k in ("cost", "golden_from", "first_level_cm"))
                for k in ("width", "depth", "height", "level_height", "max_load", "cost", "golden_from", "golden_to",
                          "first_level_cm")}
        vals["levels"] = _int(row, mapping, "levels", labels["levels"])
        raw_hs = str(get_cell(row, mapping.get("level_heights")) or "").strip()
        hs = None
        if raw_hs:
            try:
                hs = [float(Decimal(p.strip().replace(",", "."))) for p in re.split(r"[;|/ ]+", raw_hs) if p.strip()]
            except ArithmeticError:
                raise ValueError(_("Vãos por nível inválidos: {raw}", raw=f"{raw_hs!r}"))
            if any(h <= 0 for h in hs):
                raise ValueError(_("Vão por nível: os valores devem ser maiores que zero."))
        st = by_code.get(code)
        new = st is None
        if new:
            miss = [labels[k] for k in ("width", "depth", "height", "levels", "level_height") if vals.get(k) is None]
            if miss:
                raise ValueError(_("Estrutura nova: informe {v}.", v=", ".join(_(m) for m in miss)))
            if not kind:
                raise ValueError(_("Estrutura nova: informe o tipo de estrutura."))
        elif not opts.update_existing:
            return
        # valores finais (os vazios mantêm o cadastro) para validar o conjunto
        cur = (lambda k: vals[k] if vals.get(k) is not None else (getattr(st, k) if st is not None else None))
        levels, height = cur("levels"), cur("height")
        if hs is not None and levels and len(hs) > levels:
            raise ValueError(_("Foram informados {len} vãos para {levels} níveis.", len=len(hs), levels=levels))
        gf, gt = cur("golden_from"), cur("golden_to")
        if (gf is None) != (gt is None):
            raise ValueError(_("Zona de ouro: informe início e fim (ou deixe os dois vazios)."))
        if gf is not None and gt is not None and gt <= gf:
            raise ValueError(_("Zona de ouro: o fim deve ser maior que o início."))
        heights = hs if hs is not None else (st.level_heights if st is not None else None) or []
        vh = cur("level_height")
        if levels and vh and height:
            vs = [heights[i] if i < len(heights) else float(vh) for i in range(levels)]
            base = float(cur("first_level_cm") or 0)
            if base + sum(vs) > float(height) + 1e-6:
                raise ValueError(_("1º nível + vãos (= {v} cm) passam da altura total ({height} cm).",
                                   v=f"{base + sum(vs):g}", height=f"{float(height):g}"))
        if new:
            st = StructureType(code=code, name=name or code, kind=kind, notes="",
                               **{k: v for k, v in vals.items() if v is not None}, level_heights=hs)
            db.add(st)
            by_code[code] = st
            res.inc(N_("Estruturas criadas"))
        else:
            changed = False
            for k, v in [*vals.items(), ("name", name or None)]:
                if v is not None and getattr(st, k) != v:
                    setattr(st, k, v)
                    changed = True
            if hs is not None and st.level_heights != hs:
                st.level_heights, changed = hs, True
            if kind and (st.kind or "").casefold() != kind.casefold():
                st.kind, changed = kind, True
            if changed:
                res.inc(N_("Estruturas atualizadas"))
        if kind or new:
            attach_kind(db, st)  # o tipo é ligado pelo nome (tipo novo é criado)
        db.flush()
        touched.add(st.id)

    _run(db, path, opts, res, handle)
    if touched:
        n = recalc_capacities(db, structure_ids=touched)  # medidas novas: a capacidade das estantes acompanha
        if n:
            res.notes.append(_("Capacidade dos níveis recalculada nas estantes com estas estruturas ({n} alteração(ões)).", n=n))
        db.commit()
    return res


# --------------------------------------------------------------------------- #
# Estrutura das estantes (aplicar a estrutura às estantes de um depósito existente)
# --------------------------------------------------------------------------- #
BAY_FIELDS = {
    "branch_code": (N_("Filial"), False, ("filial", "codfilial", "codigofilial", "branch")),
    "warehouse_code": (N_("Armazém"), False, ("armazem", "armazém", "codarmazem", "deposito", "warehouse")),
    "aisle": (N_("Corredor (ex.: A ou A,B ou A-C)"), True, ("corredor", "rua", "aisle")),
    "bay": (N_("Estante (vazio = todas; 01-04)"), False, ("estante", "predio", "prédio", "bay", "modulo")),
    "structure": (N_("Estrutura (código; \"remover\" tira a estrutura)"), True, ("estrutura", "codigodaestrutura", "structure")),
    "top_gap": (N_("Folga acima da bin (cm)"), False, ("folga", "folgaacimadabin", "folgaacimadabincm", "topgap")),
    "first_level": (N_("Altura do 1º nível (cm)"), False, ("alturado1onivel", "alturado1nivel", "1onivel", "primeironivel", "firstlevel")),
    "create_levels": (N_("Criar níveis que faltam (sim/não)"), False, ("criarniveis", "criarniveisquefaltam", "createlevels")),
    "clear_manual": (N_("Remover limites manuais (sim/não)"), False, ("removerlimites", "removerlimitesmanuais", "clearmanual")),
}
REMOVE_WORDS = {"remover", "remove", "-", "nenhuma", "sem", "none", "quitar", "retirer", "entfernen"}


def import_bay_structures(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    from services.level_service import spill_message, structure_message
    from services.stock_service import MasterDataCache
    from services.structure_service import apply_structure, matching_bays, remove_structure

    res = MasterResult(counters={N_("Estantes com estrutura"): 0, N_("Estantes sem estrutura"): 0})
    master = MasterDataCache(db)
    structs = {s.code: s for s in db.scalars(select(StructureType))}
    labels = {k: v[0] for k, v in BAY_FIELDS.items()}
    adjust: list[str] = []

    def cond(row, key):
        v = normalize_code(get_cell(row, mapping.get(key)))
        return "" if v in ALL_WORDS else v

    def handle(line, row):
        b_code = normalize_code(get_cell(row, mapping.get("branch_code"))) or normalize_code(opts.default_branch_code)
        w_code = normalize_code(get_cell(row, mapping.get("warehouse_code"))) or normalize_code(opts.default_warehouse_code)
        if not b_code:
            raise ValueError(_("Filial vazia (mapeie a coluna ou informe a filial padrão)"))
        if b_code not in master.branches and not master._alias(b_code, master.branches, _("Filial")):
            raise ValueError(_("Filial {b_code} não cadastrada", b_code=b_code))
        bid, wid = master.branches[b_code], None
        if w_code:
            if w_code not in master.warehouses and not master._alias(w_code, master.warehouses, _("Armazém")):
                raise ValueError(_("Armazém {w_code} não cadastrado", w_code=w_code))
            wid = master.warehouses[w_code]
        aisle = cond(row, "aisle")
        if not aisle:
            raise ValueError(_("Corredor vazio"))
        s_raw = str(get_cell(row, mapping.get("structure")) or "").strip()
        if not s_raw:
            raise ValueError(_("Informe a estrutura (ou \"remover\")."))
        bays = matching_bays(db, bid, wid, aisle, cond(row, "bay"))
        if not bays:
            raise ValueError(_("Nenhuma estante corresponde a {aisle}/{v} (gere os endereços antes)", aisle=aisle, v=cond(row, "bay") or "*"))
        if _plain(s_raw) in REMOVE_WORDS:
            res.inc(N_("Estantes sem estrutura"), remove_structure(db, bays))
            return
        st = structs.get(normalize_code(s_raw))
        if st is None:
            raise ValueError(_("Estrutura {code} não cadastrada", code=s_raw))
        gap = _dec(row, mapping, "top_gap", labels["top_gap"])
        fl = _dec(row, mapping, "first_level", labels["first_level"])
        stats = apply_structure(db, bays, st, create_levels=bool(_yes(row, mapping, "create_levels")), set_heights=True,
                                clear_manual=bool(_yes(row, mapping, "clear_manual")),
                                top_gap_cm=float(gap) if gap is not None else 5.0,
                                first_level_cm=fl if fl is not None else "keep")
        res.inc(N_("Estantes com estrutura"), stats["bays"])
        for msg in (structure_message(stats["fit"]), spill_message(db, stats["spill"])):
            if msg:
                adjust.append(msg)

    _run(db, path, opts, res, handle)
    res.notes += master.notes + adjust[:10]
    return res


def _with_labels(fields: dict) -> dict:
    """Sinônimos de cabeçalho + o rótulo do campo nos 6 idiomas (a sugestão do mapeamento compara cabeçalhos)."""
    from services.i18n import LANGUAGES, lookup

    out = {}
    for key, (label, req, syn) in fields.items():
        extra = [label] + [lookup(label, lang) for lang in LANGUAGES]
        out[key] = (label, req, tuple(dict.fromkeys((*syn, *extra))))
    return out


PIECE_FIELDS, KIND_FIELDS, STRUCT_FIELDS, BAY_FIELDS = (_with_labels(f) for f in (PIECE_FIELDS, KIND_FIELDS, STRUCT_FIELDS, BAY_FIELDS))

STRUCTURE_KINDS = {
    "structure_pieces": {
        "sync_help": N_("Exclui as peças que não estão no arquivo. Peça que está na composição de algum tipo de estrutura é mantida."),
        "title": N_("Importação de Peças de estrutura"), "fields": PIECE_FIELDS, "run": import_pieces,
        "back": ("/structure-pieces", "peças de estrutura"), "scope": False, "bins_option": False,
        "help": [N_("Só o código é obrigatório; sem nome, o nome fica igual ao código. Custo por peça opcional (usado na recomposição das estruturas)."),
                 N_("Peças existentes (mesmo código) são atualizadas só nos campos preenchidos.")],
        "sample": "Codigo;Nome;Custo por peca (R$)\nMONT-PP;Quadro de montantes (porta-palete);450\n"
                  "LONG-PP;Longarina (porta-palete);120\nCOL-EST;Coluna de estante;85\nPRAT-EST;Prateleira de estante;60\n"},
    "structure_kinds": {
        "title": N_("Importação de Tipos de estrutura"), "fields": KIND_FIELDS, "run": import_kinds,
        "back": ("/structure-kinds", "tipos de estrutura"), "scope": False, "bins_option": False,
        "help": [N_("Uma linha por tipo, ou várias linhas do mesmo tipo para a composição: cada linha com Peça + Qtd + Por "
                 "(módulo ou nível) acrescenta a peça. Com peças no arquivo, a composição do tipo passa a ser a do arquivo."),
                 N_("Condição: ambiente, refrigerado, congelado ou controlado. Porta: sem, de giro ou de correr; folhas 1 ou 2; "
                 "dobradiça esquerda ou direita; abertura 90 ou 180°; tempo para abrir e fechar (s)."),
                 N_("Modelo 3D: automático, simples ou um da biblioteca (rack, shelving, cantilever, flow, cold_room ou o nome). "
                 "Peças que não existem são cadastradas com o código. Campos vazios não apagam dados.")],
        "sample": "Tipo de estrutura;Paletes no piso;Condicao;Porta;Folhas;Abertura;Tempo da porta (s);Modelo 3D;Peca;Qtd;Por;Cor no 3D\n"
                  "Porta-palete;nao;;;;;;rack;MONT-PP;2;modulo;#1f4e8c\nPorta-palete;;;;;;;;LONG-PP;2;nivel;#f28c28\n"
                  "Estante;nao;;;;;;shelving;COL-EST;4;modulo;#8a96a0\nEstante;;;;;;;;PRAT-EST;1;nivel;#c9d1d6\n"
                  "Camara refrigerada;nao;refrigerado;de giro;2;90;6;cold_room;COL-EST;4;modulo;\n"},
    "structures": {
        "title": N_("Importação de Estruturas de armazenagem"), "fields": STRUCT_FIELDS, "run": import_structures,
        "back": ("/structures", "estruturas"), "scope": False, "bins_option": False,
        "help": [N_("Estrutura nova: código, tipo, frente, profundidade, altura (cm), nº de níveis e vão útil. Existentes são "
                 "atualizadas só nos campos preenchidos."),
                 N_("Vãos por nível (cm), de baixo para cima, separados por / (ex.: 60/40/35; numa planilha Excel também 60; 40; 35). Tipo inexistente é criado com o nome."),
                 N_("A capacidade dos níveis das estantes com a estrutura é recalculada pelas medidas novas.")],
        "sample": "Codigo;Nome;Tipo;Frente (cm);Profundidade (cm);Altura (cm);Niveis;Vao util (cm);Vaos por nivel (cm);"
                  "Altura do 1o nivel (cm);Carga por nivel (kg);Custo por modulo (R$);Zona de ouro inicio (cm);Zona de ouro fim (cm)\n"
                  "EST-100;Estante 100 x 60;Estante;100;60;200;5;38;;0;100;350;75;150\n"
                  "PP-270;Porta-palete 270 x 110;Porta-palete;270;110;600;3;160;;;2000;2400;0;160\n"},
    "bay_structures": {
        "title": N_("Importação da Estrutura das estantes"), "fields": BAY_FIELDS, "run": import_bay_structures,
        "back": ("/levels", "níveis"), "scope": True, "bins_option": False,
        "help": [N_("Cada linha aplica uma estrutura às estantes: Corredor (obrigatório) e Estante aceitam valor, lista (01,03) "
                 "ou faixa (01-04); vazio = todas. \"remover\" na coluna Estrutura tira a estrutura das estantes."),
                 N_("Aplicar grava o vão de cada nível e a capacidade passa a ser calculada pelas medidas (com a folga "
                 "acima da bin informada; vazio = 5 cm). Os limites manuais ficam, a não ser com Remover limites manuais = sim."),
                 N_("Filiais, armazéns, endereços e estruturas precisam existir (importe os endereços e as estruturas antes).")],
        "sample": "Filial;Armazem;Corredor;Estante;Estrutura;Folga acima da bin (cm);Altura do 1o nivel (cm);Criar niveis que faltam\n"
                  "01;CD1;A;01-10;EST-100;5;;nao\n01;CD1;P;;PP-270;10;;sim\n"},
}
