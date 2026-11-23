"""
Importação dos cadastros-base via CSV/XLSX (mesmo fluxo das importações de produtos e saldos):
  - Filiais                (código, nome)
  - Armazéns               (código, nome, filiais — lista "01, 02")
  - Tipos de bin           (código, nome, dimensões, peso máx., volume)
  - Endereços              (filial, armazém, endereço OU corredor/estante/nível/posição/compartimento, tipo de bin, qtd)
  - Capacidade dos níveis  (filial, armazém, corredor, estante, nível — aceitam faixas —, tipo de bin, máx., vão)
  - Estruturas (peças, tipos com composição, estruturas e estrutura das estantes): services/structure_importer

Regras comuns: linhas com erro são listadas e não interrompem o arquivo; campos vazios não apagam dados
existentes; códigos de filial/armazém são associados mesmo quando o Excel tira zeros (01 -> 1).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models import BinType, Branch, LocationBin, StorageLocation, Warehouse
from services.addressing import build_address, check_location_conflict, normalize_code, parse_address
from services.file_reader import get_cell, iter_rows, normalize_header, parse_decimal
from services.stock_service import MasterDataCache
from services.i18n import N_, _

CHUNK = 500
LIST_SPLIT = re.compile(r"[,;|/]+")
ALL_WORDS = {"", "*", "TODAS", "TODOS", N_("(TODAS)"), N_("(TODOS)"), "TUDO"}

# campo -> (rótulo, obrigatório, sinônimos)
BRANCH_FIELDS = {
    "code": (N_("Código da filial"), True, ("codigo", "código", "cod", "filial", "codfilial", "codigofilial", "code", "branch")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao", "descrição", "razaosocial", "nomefilial")),
    "unitization": (N_("Unitização (desligada/só caixas/caixas e paletes)"), False, ("unitizacao", "unitização",
                                                                                     "unitization")),
}
UNIT_WORDS = {"full": ("caixasepaletes", "caixaspaletes", "paletes", "full", "completa", "ligada", "total"),
              "cases": ("socaixas", "caixas", "cases", "soloboxes"),
              "off": ("desligada", "nao", "off", "nenhuma", "unidades")}


def parse_unitization(raw: str):
    """Texto da coluna Unitização (aceita o traduzido) → "off" | "cases" | "full"; vazio = None."""
    v = normalize_header(raw or "")
    if not v:
        return None
    for mode, words in UNIT_WORDS.items():
        if any(v.startswith(w) for w in words):
            return mode
    raise ValueError(_("Unitização inválida: {v} (use desligada, só caixas ou caixas e paletes)", v=raw))


WAREHOUSE_FIELDS = {
    "code": (N_("Código do armazém"), True, ("codigo", "código", "cod", "armazem", "armazém", "codarmazem", "deposito", "code", "warehouse")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao", "descrição", "nomearmazem")),
    "branches": (N_("Filiais (lista: 01, 02)"), False, ("filiais", "filial", "codfilial", "branches", "lojas", "empresas")),
}
BIN_TYPE_FIELDS = {
    "code": (N_("Código do tipo"), True, ("codigo", "código", "cod", "tipo", "tipodebin", "code", "bin")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao", "descrição")),
    "height": (N_("Altura (cm)"), False, ("altura", "alturacm", "height", "alt")),
    "width": (N_("Largura (cm)"), False, ("largura", "larguracm", "width", "larg")),
    "length": (N_("Comprimento (cm)"), False, ("comprimento", "comprimentocm", "length", "profundidade", "comp")),
    "max_weight": (N_("Peso máximo (kg)"), False, ("pesomax", "pesomaxkg", "pesomaximo", "pesomaximokg", "maxweight", "capacidadekg", "peso")),
    "max_volume": ("Volume (L)", False, ("volume", "volumel", "volumelitros", "maxvolume", "litros")),
    "cost": (N_("Custo por bin (R$)"), False, ("custo", "custor", "custobin", "custoporbin", "preco", "preço", "valor", "cost")),
    "open_top": (N_("Aberta em cima (sim/não)"), False, ("abertaemcima", "aberta", "opentop", "open", "semtampa")),
    "over_cm": (N_("Máximo acima da borda (cm)"), False, ("maximoacimadaborda", "maximoacimadabordacm", "acimadaborda", "maxacima", "overhang", "sobreborda")),
    "parent": (N_("Bin pai"), False, ("binpai", "pai", "parent", "tipopai", "bininteira")),
    "div_w": (N_("Divisões na largura"), False, ("divisoesnalargura", "divisoeslargura", "colunas", "nalargura", "divlargura", "divisoesl")),
    "div_l": (N_("Divisões no comprimento"), False, ("divisoesnocomprimento", "divisoescomprimento", "fileiras", "nocomprimento", "divcomprimento", "divisoesc")),
    "fraction": (N_("Fração (ex.: 1/2) — alternativa às divisões"), False, ("fracao", "fração", "fraction", "compartimento")),
    "split_axis": (N_("Divisão (largura/comprimento)"), False, ("divisao", "divisão", "eixo", "split", "dividirna")),
    "store_as": (N_("Armazena em (unidades/caixas/ambos/palete)"), False, ("armazenaem", "armazena", "unidadedearmazenagem", "storeas", "guarda")),
    "allow_split": (N_("Permite fracionar a bin pai (sim/não)"), False, ("permitefracionar", "permitefracionarabinpai", "fracionar", "fracionavel", "fracionável", "allowsplit", "split_parent")),
}
STORE_WORDS = {"both": ("ambos", "unidades ou", "unidade ou", "both", "mixed", "misto"),
               "pallet": ("palete", "pallet", "palet", "paleta"),
               "cases": ("caixa", "case", "box", "caja", "carton"),
               "units": ("unidade", "unit", "solta", "loose", "un")}
LOCATION_FIELDS = {
    "branch_code": (N_("Filial"), False, ("filial", "codfilial", "codigofilial", "branch", "loja", "empresa")),
    "warehouse_code": (N_("Armazém"), False, ("armazem", "armazém", "codarmazem", "deposito", "depósito", "warehouse")),
    "address": (N_("Endereço completo (A-01-02-03)"), False, ("endereco", "endereço", "enderecocompleto", "location", "localizacao")),
    "aisle": (N_("Corredor"), False, ("corredor", "rua", "aisle")),
    "bay": (N_("Estante"), False, ("estante", "predio", "prédio", "coluna", "bay", "modulo", "módulo")),
    "level": (N_("Nível"), False, ("nivel", "nível", "andar", "level")),
    "position": (N_("Posição"), False, ("posicao", "posição", "apto", "apartamento", "position", "pos")),
    "compartment": (N_("Compartimento"), False, ("compartimento", "comp", "divisao", "divisão", "compartment")),
    "generic": (N_("Genérico (sim/não)"), False, ("generico", "genérico", "enderecogenerico", "generic")),
    "bin_type": (N_("Tipo de bin"), False, ("tipodebin", "tipobin", "bin", "tipo", "bintype")),
    "bin_qty": (N_("Qtd de bins"), False, ("qtdbins", "qtddebins", "quantidadedebins", "qtdbin", "bins", "qtd", "quantidade")),
}
LEVEL_FIELDS = {
    "branch_code": (N_("Filial"), False, ("filial", "codfilial", "codigofilial", "branch")),
    "warehouse_code": (N_("Armazém"), False, ("armazem", "armazém", "codarmazem", "deposito", "warehouse")),
    "aisle": (N_("Corredor (ex.: A ou A,B ou A-C)"), True, ("corredor", "rua", "aisle")),
    "bay": (N_("Estante (vazio = todas; 01-04)"), False, ("estante", "predio", "prédio", "bay", "modulo")),
    "level": (N_("Nível (vazio = todos; 01,03)"), False, ("nivel", "nível", "andar", "level")),
    "bin_type": (N_("Tipo de bin"), False, ("tipodebin", "tipobin", "bin", "tipo", "bintype")),
    "max_bins": (N_("Cabem (máx. bins)"), False, ("cabem", "cabemmaxbins", "maxbins", "maximodebins", "capacidade", "qtdmax", "max")),
    "height": (N_("Vão (cm)"), False, ("vao", "vão", "vaocm", "altura", "alturadovao", "distanciaentreniveis", "height")),
}


@dataclass
class MasterResult:
    rows_read: int = 0
    rows_ok: int = 0
    errors: list = field(default_factory=list)
    error_count: int = 0
    notes: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)  # rótulo -> valor (cards da tela de resultado)
    elapsed_ms: int = 0
    chunks: int = 0
    MAX_LISTED = 500

    def error(self, line: int, msg: str) -> None:
        self.error_count += 1
        if len(self.errors) < self.MAX_LISTED:
            self.errors.append((line, msg))

    def inc(self, label: str, n: int = 1) -> None:
        self.counters[label] = self.counters.get(label, 0) + n


@dataclass
class MasterOptions:
    has_header: bool = True
    update_existing: bool = True
    default_branch_code: str = ""
    default_warehouse_code: str = ""
    replace_bins: bool = True  # endereços: as bins do arquivo substituem as do endereço
    sync: bool = False  # excluir o que não está no arquivo (só o que não está em uso)
    drop_levels: bool = False  # endereços + sync: excluir também a posição física (nível) que ficar vazia


def _sync(res: MasterResult, opts: MasterOptions, fn) -> None:
    """Roda a sincronização se o arquivo veio limpo; senão avisa e não exclui nada."""
    from services.import_sync import blocked_reason

    if not opts.sync:
        return
    why = blocked_reason(res.error_count, res.rows_ok)
    if why:
        res.notes.append(why)
        return
    fn()


def _dec(row, mapping, key, label, allow_zero=True) -> Optional[Decimal]:
    raw = get_cell(row, mapping.get(key))
    try:
        v = parse_decimal(raw)
    except ValueError:
        raise ValueError(_("{label} inválido(a): {raw}", label=label, raw=f"{raw!r}"))
    if v is not None and (v < 0 or (v == 0 and not allow_zero)):
        raise ValueError(_("{label} deve ser maior que zero: {raw}", label=label, raw=f"{raw!r}"))
    return v


def _run(db: Session, path: Path, opts: MasterOptions, result: MasterResult, handle) -> MasterResult:
    t0 = time.perf_counter()
    n = 0
    for line, row in iter_rows(path, opts.has_header):
        result.rows_read += 1
        try:
            handle(line, row)  # cada handler valida tudo antes de gravar
            result.rows_ok += 1
        except ValueError as exc:
            result.error(line, str(exc))
        n += 1
        if n % CHUNK == 0:
            db.commit()
            result.chunks += 1
    db.commit()
    result.chunks += 1
    result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return result


# --------------------------------------------------------------------------- #
# Filiais
# --------------------------------------------------------------------------- #
def import_branches(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    res = MasterResult(counters={N_("Filiais criadas"): 0, N_("Filiais atualizadas"): 0})
    master = MasterDataCache(db)
    seen: set[int] = set()

    def handle(line, row):
        code = normalize_code(get_cell(row, mapping.get("code")))
        name = get_cell(row, mapping.get("name"))[:150]
        if not code:
            raise ValueError(_("Código vazio"))
        if len(code) > 30:
            raise ValueError(_("Código com mais de 30 caracteres"))
        existing = master.branches.get(code)
        if existing is None and master._alias(code, master.branches, _("Filial")):
            existing = master.branches.get(code)
        if existing is not None:
            seen.add(existing)
        unit = parse_unitization(get_cell(row, mapping.get("unitization")))
        if existing is None:
            b = Branch(code=code, name=name or code, unitization=unit or "off")
            db.add(b)
            db.flush()
            master.branches[code] = b.id
            seen.add(b.id)
            res.inc(N_("Filiais criadas"))
        elif opts.update_existing and (name or unit):
            b = db.get(Branch, existing)
            if (name and b.name != name) or (unit and b.unitization != unit):
                b.name = name or b.name
                b.unitization = unit or b.unitization
                res.inc(N_("Filiais atualizadas"))

    _run(db, path, opts, res, handle)
    res.notes = master.notes

    def do():
        from services.import_sync import sync_branches
        n, notes = sync_branches(db, seen)
        res.counters["Filiais excluídas"] = n
        res.notes += notes
        db.commit()
    _sync(res, opts, do)
    return res


# --------------------------------------------------------------------------- #
# Armazéns
# --------------------------------------------------------------------------- #
def import_warehouses(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    res = MasterResult(counters={N_("Armazéns criados"): 0, N_("Armazéns atualizados"): 0, N_("Filiais criadas"): 0,
                                 N_("Filial×Armazém associados"): 0})
    master = MasterDataCache(db)
    seen: set[int] = set()
    links: dict[int, set[int]] = {}

    def handle(line, row):
        code = normalize_code(get_cell(row, mapping.get("code")))
        name = get_cell(row, mapping.get("name"))[:150]
        if not code:
            raise ValueError(_("Código vazio"))
        if len(code) > 30:
            raise ValueError(_("Código com mais de 30 caracteres"))
        branch_codes = [normalize_code(c) for c in LIST_SPLIT.split(get_cell(row, mapping.get("branches"))) if c.strip()]
        before_b = master.created_branches
        master.ensure_branches(branch_codes)
        res.inc(N_("Filiais criadas"), master.created_branches - before_b)
        existing = master.warehouses.get(code)
        if existing is None and master._alias(code, master.warehouses, _("Armazém")):
            existing = master.warehouses.get(code)
        if existing is None:
            w = Warehouse(code=code, name=name or code)
            db.add(w)
            db.flush()
            master.warehouses[code] = existing = w.id
            res.inc(N_("Armazéns criados"))
        elif opts.update_existing and name:
            w = db.get(Warehouse, existing)
            if w.name != name:
                w.name = name
                res.inc(N_("Armazéns atualizados"))
        before = master.created_links
        master.ensure_links([(master.branches[b], existing) for b in branch_codes])
        res.inc(N_("Filial×Armazém associados"), master.created_links - before)
        seen.add(existing)
        if branch_codes:
            links.setdefault(existing, set()).update(master.branches[b] for b in branch_codes)

    _run(db, path, opts, res, handle)
    res.notes = master.notes

    def do():
        from services.import_sync import sync_warehouses
        cnt, notes = sync_warehouses(db, seen, links if mapping.get("branches") is not None else None)
        res.counters.update(cnt)
        res.notes += notes
        db.commit()
    _sync(res, opts, do)
    return res


# --------------------------------------------------------------------------- #
# Tipos de bin
# --------------------------------------------------------------------------- #
def import_bin_types(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    from services.bin_type_service import derived_dims, parse_grid
    from services.level_service import fit_structure, structure_message

    res = MasterResult(counters={N_("Tipos criados"): 0, N_("Tipos atualizados"): 0})
    by_code = {bt.code: bt for bt in db.scalars(select(BinType))}
    labels = {k: v[0] for k, v in BIN_TYPE_FIELDS.items()}
    seen: set[str] = set()

    def handle(line, row):
        code = normalize_code(get_cell(row, mapping.get("code")))
        name = get_cell(row, mapping.get("name"))[:150]
        if not code:
            raise ValueError(_("Código vazio"))
        if len(code) > 30:
            raise ValueError(_("Código com mais de 30 caracteres"))
        vals = {k: _dec(row, mapping, k, labels[k], allow_zero=False) for k in ("height", "width", "length", "max_weight", "max_volume")}
        vals["cost"] = _dec(row, mapping, "cost", labels["cost"])
        oraw = get_cell(row, mapping.get("open_top")) if mapping.get("open_top") is not None else None
        if oraw is not None and str(oraw).strip():  # aberta em cima: a carga pode passar da borda
            vals["open_top"] = str(oraw).strip().casefold() in {"s", "sim", "1", "x", "true", "verdadeiro", "yes", "y", "si", "oui", "ja", "是"}
        vals["over_cm"] = _dec(row, mapping, "over_cm", labels["over_cm"], allow_zero=False)
        if vals["over_cm"] is not None and vals.get("open_top") is None:
            vals["open_top"] = True  # informou o máximo acima da borda: a bin é aberta em cima
        ax_txt = get_cell(row, mapping.get("split_axis")).strip().lower()
        axis = "length" if ax_txt.startswith(("comp", "prof", "len", "c")) else "width"
        frac, msg = parse_grid(get_cell(row, mapping.get("div_w")), get_cell(row, mapping.get("div_l")),
                               get_cell(row, mapping.get("fraction")), axis)
        if msg:
            raise ValueError(msg)
        pcode = normalize_code(get_cell(row, mapping.get("parent")))
        parent = by_code.get(pcode) if pcode else None
        if pcode and parent is None:
            raise ValueError(_("Bin pai {pcode} não cadastrada (cadastre-a antes ou numa linha anterior)", pcode=pcode))
        if parent is not None and parent.is_fraction:
            raise ValueError(_("Bin pai {pcode} é um compartimento", pcode=pcode))
        if bool(frac) != bool(parent):
            raise ValueError(_("Bin pai e divisão (ou fração) precisam ser informadas juntas"))
        if parent is not None:
            vals.update(parent_id=parent.id, div_w=frac[0], div_l=frac[1], fraction_num=1, fraction_den=frac[0] * frac[1],
                        split_axis="grid")
            if all(vals.get(k) is None for k in ("height", "width", "length")):
                d = derived_dims(parent, *frac)
                vals.update(dims_auto=True, height=d["height"], width=d["width"], length=d["length"])
                for k in ("max_weight", "max_volume"):
                    if vals.get(k) is None:
                        vals[k] = d[k]
        if vals.get("open_top") is None:
            vals.pop("open_top", None)  # coluna vazia: não mexe (tipo novo: fechada)
        aw = str(get_cell(row, mapping.get("store_as")) or "").strip().casefold() if mapping.get("store_as") is not None else ""
        if aw:  # unitização: o que a bin guarda
            mode = next((m for m, words in STORE_WORDS.items() if any(aw.startswith(w) for w in words)), None)
            if mode is None:
                raise ValueError(_("Armazena em inválido: {v} (use unidades, caixas, ambos ou palete)", v=aw))
            vals["store_as"] = mode
        sraw = get_cell(row, mapping.get("allow_split")) if mapping.get("allow_split") is not None else None
        if sraw is not None and str(sraw).strip():  # compartimento: a otimização pode fracionar a bin pai
            on = str(sraw).strip().casefold() in {"s", "sim", "1", "x", "true", "verdadeiro", "yes", "y", "si", "oui", "ja", "是"}
            if on and parent is None:
                raise ValueError(_("Permite fracionar a bin pai: só vale para compartimentos (informe a bin pai)"))
            vals["allow_split"] = on
        bt = by_code.get(code)
        seen.add(code)
        if bt is None:
            bt = BinType(code=code, name=name or code, **vals)
            db.add(bt)
            db.flush()
            by_code[code] = bt
            res.inc(N_("Tipos criados"))
        elif opts.update_existing:
            changed = False
            if name and bt.name != name:
                bt.name, changed = name, True
            for k, v in vals.items():
                if v is not None and getattr(bt, k) != v:
                    setattr(bt, k, v)
                    changed = True
            if changed:
                res.inc(N_("Tipos atualizados"))

    _run(db, path, opts, res, handle)
    adj = structure_message(fit_structure(db))  # bin mais alta que o vão: a estrutura sobe
    from services.structure_service import recalc_capacities

    if recalc_capacities(db) or adj:  # tipos novos ou medidas novas: a capacidade calculada acompanha
        db.commit()
    if adj:
        res.notes.append(adj)

    def do():
        from services.import_sync import sync_bin_types
        n, notes = sync_bin_types(db, seen)
        res.counters["Tipos excluídos"] = n
        res.notes += notes
        db.commit()
    _sync(res, opts, do)
    return res


# --------------------------------------------------------------------------- #
# Endereços (+ bins)
# --------------------------------------------------------------------------- #
def import_locations(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    from services.bin_service import set_location_bins
    from services.level_service import bays_of_locations, fit_structure, spill_message, spill_overflow, structure_message

    res = MasterResult(counters={N_("Endereços criados"): 0, N_("Endereços já existentes"): 0, N_("Endereços com bins gravadas"): 0,
                                 N_("Filiais criadas"): 0, N_("Armazéns criados"): 0})
    master = MasterDataCache(db)
    types = {bt.code: bt.id for bt in db.scalars(select(BinType))}
    use_segments = mapping.get("aisle") is not None
    if not use_segments and mapping.get("address") is None:
        raise ValueError(_("Mapeie a coluna Endereço completo ou, separadamente, Corredor (e Estante, Nível, Posição, Compartimento)."))
    known: dict[tuple, dict[str, int]] = {}  # (filial, armazém) -> endereço -> id
    bins_by_loc: dict[int, dict[int, int]] = {}  # bins do arquivo, somadas por endereço e tipo
    scopes: set[tuple] = set()

    def scope_index(b, w):
        key = (b, w)
        if key not in known:
            stmt = select(StorageLocation.address, StorageLocation.id).where(StorageLocation.branch_id == b)
            stmt = stmt.where(StorageLocation.warehouse_id.is_(None) if w is None else StorageLocation.warehouse_id == w)
            known[key] = dict(db.execute(stmt).all())
        return known[key]

    def handle(line, row):
        if use_segments:
            parts = tuple(normalize_code(get_cell(row, mapping.get(k))) for k in ("aisle", "bay", "level", "position", "compartment"))
            if not parts[0]:
                raise ValueError(_("Corredor vazio"))
            for i in range(1, 5):
                if parts[i] and not parts[i - 1]:
                    raise ValueError(_("Hierarquia incompleta: um nível foi preenchido sem o anterior"))
            for p in parts:
                if len(p) > 20:
                    raise ValueError(_("Segmento muito longo (máx. 20): {p}", p=f"{p!r}"))
        else:
            parts = parse_address(get_cell(row, mapping.get("address")))
        t_code = normalize_code(get_cell(row, mapping.get("bin_type")))
        q = 0
        if t_code:
            if t_code not in types:
                raise ValueError(_("Tipo de bin {t_code} não cadastrado (importe os tipos de bin antes)", t_code=t_code))
            raw = get_cell(row, mapping.get("bin_qty"))
            try:
                q = int(parse_decimal(raw)) if raw else 1
            except (ValueError, TypeError):
                raise ValueError(_("Quantidade de bins inválida: {raw}", raw=f"{raw!r}"))
            if q < 0 or q > 10_000:
                raise ValueError(_("Quantidade de bins fora do intervalo: {q}", q=q))
        b_code = normalize_code(get_cell(row, mapping.get("branch_code"))) or normalize_code(opts.default_branch_code)
        w_code = normalize_code(get_cell(row, mapping.get("warehouse_code"))) or normalize_code(opts.default_warehouse_code)
        if not b_code:
            raise ValueError(_("Filial vazia (mapeie a coluna ou informe a filial padrão)"))
        cb, cw = master.created_branches, master.created_warehouses
        master.ensure_branches([b_code])
        bid = master.branches[b_code]
        wid = None
        if w_code:
            master.ensure_warehouses([w_code])
            wid = master.warehouses[w_code]
            master.ensure_links([(bid, wid)])
        res.inc(N_("Filiais criadas"), master.created_branches - cb)
        res.inc(N_("Armazéns criados"), master.created_warehouses - cw)
        address = build_address(*parts)
        idx = scope_index(bid, wid)
        scopes.add((bid, wid))
        lid = idx.get(address)
        graw = get_cell(row, mapping.get("generic")) if mapping.get("generic") is not None else None
        gen = None if graw is None or not str(graw).strip() else \
            str(graw).strip().casefold() in {"s", "sim", "1", "x", "true", "verdadeiro", "yes", "y"}
        if lid is None:
            conflict = check_location_conflict(db, bid, wid, parts)
            if conflict:
                raise ValueError(f"{address}: {conflict}")
            loc = StorageLocation(branch_id=bid, warehouse_id=wid, aisle=parts[0], bay=parts[1], level=parts[2],
                                  position=parts[3], compartment=parts[4], address=address, is_generic=bool(gen))
            db.add(loc)
            db.flush()
            lid = idx[address] = loc.id
            res.inc(N_("Endereços criados"))
        elif gen is not None:  # coluna Genérico informada: atualiza a marca do endereço existente
            db.execute(update(StorageLocation).where(StorageLocation.id == lid).values(is_generic=gen))
        elif lid not in bins_by_loc:
            res.inc(N_("Endereços já existentes"))
        if t_code:
            cur = bins_by_loc.setdefault(lid, {})
            cur[types[t_code]] = cur.get(types[t_code], 0) + q
        else:
            bins_by_loc.setdefault(lid, {})

    _run(db, path, opts, res, handle)
    # bins: gravadas ao final (várias linhas do mesmo endereço somam)
    with_bins = {lid: cfg for lid, cfg in bins_by_loc.items() if cfg}
    for lid, cfg in with_bins.items():
        if opts.replace_bins:
            set_location_bins(db, lid, [(t, n) for t, n in cfg.items()])
        else:
            for t, n in cfg.items():
                lb = db.scalar(select(LocationBin).where(LocationBin.location_id == lid, LocationBin.bin_type_id == t,
                                                         LocationBin.shelf_level_id.is_(None)))
                if lb is None:
                    db.add(LocationBin(location_id=lid, bin_type_id=t, quantity=n))
                else:
                    lb.quantity += n
    res.inc(N_("Endereços com bins gravadas"), len(with_bins))
    db.flush()
    from services.bin_content_service import refresh_contents

    refresh_contents(db, with_bins)
    if with_bins:
        spilled = spill_message(db, spill_overflow(db, bays_of_locations(db, with_bins)))
        adj = structure_message(fit_structure(db))
        res.notes += [m for m in (spilled, adj) if m]
    db.commit()
    res.notes = master.notes + res.notes

    def do():
        from services.import_sync import sync_locations
        cnt, notes = sync_locations(db, scopes, set(bins_by_loc), opts.drop_levels)
        res.counters.update(cnt)
        res.notes += notes
        db.commit()
    _sync(res, opts, do)
    return res


# --------------------------------------------------------------------------- #
# Capacidade dos níveis (+ vão)
# --------------------------------------------------------------------------- #
def import_levels(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    from services.level_service import (fit_structure, get_level, matching_levels, set_capacity, set_height,
                                        spill_message, spill_overflow, structure_message)

    res = MasterResult(counters={N_("Linhas aplicadas"): 0, N_("Níveis com capacidade"): 0, N_("Níveis com vão"): 0})
    master = MasterDataCache(db)
    types = {bt.code: bt.id for bt in db.scalars(select(BinType))}
    touched_bays: set[tuple] = set()
    scopes: set[tuple] = set()
    cap_seen: set[tuple] = set()
    height_seen: set[int] = set()

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
        bid = master.branches[b_code]
        wid = None
        if w_code:
            if w_code not in master.warehouses and not master._alias(w_code, master.warehouses, _("Armazém")):
                raise ValueError(_("Armazém {w_code} não cadastrado", w_code=w_code))
            wid = master.warehouses[w_code]
        aisle = cond(row, "aisle")
        if not aisle:
            raise ValueError(_("Corredor vazio"))
        t_code = normalize_code(get_cell(row, mapping.get("bin_type")))
        mx_raw = get_cell(row, mapping.get("max_bins"))
        height = _dec(row, mapping, "height", _("Vão"), allow_zero=False)
        if height is not None and height > 5000:
            raise ValueError(_("Vão acima de 5000 cm"))
        if not t_code and height is None:
            raise ValueError(_("Informe o tipo de bin com a quantidade e/ou o vão"))
        keys = matching_levels(db, bid, wid, aisle, cond(row, "bay"), cond(row, "level"))
        if not keys:
            raise ValueError(_("Nenhum nível corresponde a {aisle}/{v}/{v2} (gere os endereços antes)", aisle=aisle, v=cond(row, 'bay') or '*', v2=cond(row, 'level') or '*'))
        if t_code:
            if t_code not in types:
                raise ValueError(_("Tipo de bin {t_code} não cadastrado", t_code=t_code))
            try:
                mx = int(parse_decimal(mx_raw)) if mx_raw else None
            except (ValueError, TypeError):
                raise ValueError(_("Quantidade máxima inválida: {mx_raw}", mx_raw=f"{mx_raw!r}"))
            if mx is None:
                raise ValueError(_("Informe quantas bins cabem (0 remove a capacidade do tipo)"))
            if mx < 0 or mx > 100_000:
                raise ValueError(_("Quantidade máxima fora do intervalo: {mx}", mx=mx))
            res.inc(N_("Níveis com capacidade"), set_capacity(db, keys, types[t_code], mx or None))
            if opts.sync:
                db.flush()
                cap_seen.update((sl.id, types[t_code]) for sl in (get_level(db, k) for k in keys) if sl)
        if height is not None:
            res.inc(N_("Níveis com vão"), set_height(db, keys, height))
            if opts.sync:
                db.flush()
                height_seen.update(sl.id for sl in (get_level(db, k) for k in keys) if sl)
        scopes.add((bid, wid))
        touched_bays.update(k[:4] for k in keys)
        res.inc(N_("Linhas aplicadas"))

    _run(db, path, opts, res, handle)
    if touched_bays:
        adj = structure_message(fit_structure(db))
        from services.structure_service import recalc_capacities

        recalc_capacities(db, touched_bays)  # vãos importados: a capacidade calculada acompanha
        spilled = spill_message(db, spill_overflow(db, touched_bays))
        res.notes += [m for m in (adj, spilled) if m]
        db.commit()
    res.notes = master.notes + res.notes

    def do():
        from services.import_sync import sync_levels
        cnt, notes = sync_levels(db, scopes, cap_seen, height_seen if mapping.get("height") is not None else None)
        from services.structure_service import recalc_capacities

        recalc_capacities(db, touched_bays)  # vãos removidos: a capacidade calculada acompanha
        res.counters.update(cnt)
        res.notes += notes
        db.commit()
    _sync(res, opts, do)
    return res


# --------------------------------------------------------------------------- #
# Embalagens do produto (hierarquia) e paletizações
# --------------------------------------------------------------------------- #
PACKAGING_MODEL_FIELDS = {
    "code": (N_("Código"), True, ("codigo", "cod", "code", "modelo")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao")),
    "height": (N_("Altura (cm)"), False, ("altura", "alturacm", "height")),
    "width": (N_("Largura (cm)"), False, ("largura", "larguracm", "width")),
    "length": (N_("Comprimento (cm)"), False, ("comprimento", "comprimentocm", "length")),
    "tare": (N_("Tara (kg)"), False, ("tara", "tarakg", "pesovazio", "tare")),
    "inner": (N_("Contém (modelo)"), False, ("contem", "contém", "feitade", "inner")),
    "inner_qty": (N_("Quantidade"), False, ("quantidade", "qtd", "qtde", "qty")),
    "notes": (N_("Observações"), False, ("observacoes", "obs", "notas", "notes")),
}


def import_packaging_models(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    """Modelos de embalagem: código, nome, medidas, tara e a hierarquia (contém N de outro modelo — em linha anterior
    ou já cadastrado; quantidade vazia = quantas cabem pelas medidas)."""
    from models import PackagingModel
    from services.packaging import fit_in_case

    res = MasterResult(counters={N_("Modelos criados"): 0, N_("Modelos atualizados"): 0})
    models = {m.code: m for m in db.scalars(select(PackagingModel))}

    def num(row, key):
        raw = get_cell(row, mapping.get(key)).strip()
        if not raw:
            return None
        v = parse_decimal(raw)
        if v is None or v < 0 or (v == 0 and key != "tare"):
            raise ValueError(_("{label}: número maior que zero", label=_(PACKAGING_MODEL_FIELDS[key][0])))
        return Decimal(str(v))

    def handle(line, row):
        code = normalize_code(get_cell(row, mapping.get("code")))
        if not code:
            raise ValueError(_("Código vazio"))
        if len(code) > 30:
            raise ValueError(_("Código com mais de 30 caracteres"))
        vals = {k: num(row, k) for k in ("height", "width", "length", "tare")}
        m = models.get(code)
        if m is None and not all(vals[k] for k in ("height", "width", "length")):
            raise ValueError(_("Informe altura, largura e comprimento da caixa."))
        icode = normalize_code(get_cell(row, mapping.get("inner")))
        inner = None
        if icode:
            inner = models.get(icode)
            if inner is None:
                raise ValueError(_("Modelo de embalagem {code} não cadastrado", code=icode))
            if inner.code == code:
                raise ValueError(_("A embalagem não pode ser feita dela mesma"))
        qraw = get_cell(row, mapping.get("inner_qty")).strip()
        qty = None
        if qraw:
            q = parse_decimal(qraw)
            if q is None or q <= 0 or q != int(q):
                raise ValueError(_("{v}: número inteiro maior que zero", v=_("Quantidade")))
            qty = int(q)
        if m is None:
            m = PackagingModel(code=code, name="", notes="")
            db.add(m)
            models[code] = m
            res.inc(N_("Modelos criados"))
        elif opts.update_existing:
            res.inc(N_("Modelos atualizados"))
        else:
            return
        m.name = (get_cell(row, mapping.get("name")).strip() or m.name or code)[:150]
        notes = get_cell(row, mapping.get("notes")).strip()
        if notes:
            m.notes = notes[:200]
        for k, v in vals.items():
            if v is not None:
                setattr(m, k, v)
        if inner is not None:
            m.inner_model = inner
            if not qty:
                qty = fit_in_case((float(inner.height), float(inner.width), float(inner.length)), None,
                                  (float(m.height), float(m.width), float(m.length))) or None
                if not qty:
                    raise ValueError(_("O modelo {code} não cabe neste pelas medidas.", code=inner.code))
            m.inner_qty = qty
        db.flush()

    _run(db, path, opts, res, handle)
    db.commit()
    return res


PACKAGING_FIELDS = {
    "sku": (N_("SKU"), True, ("sku", "codigo", "codproduto", "produto", "item")),
    "model": (N_("Modelo de embalagem"), True, ("modelodeembalagem", "modelo", "embalagem", "caixapadrao", "model")),
    "qty": (N_("Quantidade"), False, ("quantidade", "qtd", "qtde", "unidades", "qty")),
    "inner": (N_("Feita de (modelo; vazio = unidades)"), False, ("feitade", "de", "dentro", "contem", "inner")),
    "weight": (N_("Peso cheia (kg)"), False, ("pesocheia", "peso", "pesokg", "weight")),
    "branches": (N_("Filiais habilitadas (todas, nenhuma ou códigos)"), False, ("filiaishabilitadas", "filiais",
                                                                               "habilitadanasfiliais", "branches")),
    "order": (N_("Ordem"), False, ("ordem", "sequencia", "order", "prioridade")),
    "pallet": (N_("Palete (tipo de bin)"), False, ("palete", "pallet", "tipodepalete")),
    "layer": (N_("Caixas por camada"), False, ("caixasporcamada", "lastro", "porcamada", "ti")),
    "layers": (N_("Camadas"), False, ("camadas", "layers", "hi")),
}
YES = {"s", "sim", "1", "x", "true", "verdadeiro", "yes", "y", "si", "oui", "ja", "是"}


def import_packagings(db: Session, path: Path, mapping: dict, opts: MasterOptions) -> MasterResult:
    """Uma linha por produto × modelo de embalagem (× palete): cria/atualiza a embalagem do produto pelo modelo; "Feita
    de" é outro modelo da lista do produto (em linha anterior ou já cadastrado); o palete acrescenta/atualiza a
    paletização; Filiais habilitadas = todas (padrão de uma embalagem nova), nenhuma ou códigos (01, 02)."""
    from decimal import Decimal

    from models import PackagingModel, PackagingPallet, Product, ProductPackaging
    from services.packaging import check, fill_chain

    res = MasterResult(counters={N_("Embalagens criadas"): 0, N_("Embalagens atualizadas"): 0, N_("Paletizações"): 0})
    models = {m.code: m for m in db.scalars(select(PackagingModel))}
    pallets = {b.code: b for b in db.scalars(select(BinType).where(BinType.store_as == "pallet"))}
    branches = {b.code: b for b in db.scalars(select(Branch))}
    touched: dict[int, set] = {}

    def num(row, key, integer=False):
        raw = get_cell(row, mapping.get(key)).strip()
        if not raw:
            return None
        v = parse_decimal(raw)
        if v is None or v <= 0 or (integer and v != int(v)):
            label = _(PACKAGING_FIELDS[key][0])
            raise ValueError(_("{v}: número inteiro maior que zero", v=label) if integer
                             else _("{label}: número maior que zero", label=label))
        return int(v) if integer else Decimal(str(v))

    def handle(line, row):
        sku = get_cell(row, mapping.get("sku")).strip()
        mcode = normalize_code(get_cell(row, mapping.get("model")))
        if not sku or not mcode:
            raise ValueError(_("SKU e modelo de embalagem são obrigatórios"))
        prod = db.scalar(select(Product).where(Product.sku == sku))
        if prod is None:
            raise ValueError(_("SKU {sku} não cadastrado", sku=sku))
        if mcode not in models:
            raise ValueError(_("Modelo de embalagem {code} não cadastrado", code=mcode))
        m = models[mcode]
        qty, layer, layers = num(row, "qty", True), num(row, "layer", True), num(row, "layers", True)
        weight = num(row, "weight")
        by_model = {k.model_id: k for k in prod.packagings if k.model_id}
        icode = normalize_code(get_cell(row, mapping.get("inner")))
        inner = None
        if icode and icode.casefold() not in ("unidade", "unidades", "un", "units", "unit"):
            inner = by_model.get(models[icode].id) if icode in models else None
            if inner is None:
                raise ValueError(_("Embalagem de dentro {name} não encontrada no produto (importe-a antes)", name=icode))
        if inner is not None and (inner.model_id == m.id or (inner.inner is not None and inner.inner.model_id == m.id)):
            raise ValueError(_("A embalagem não pode ser feita dela mesma"))
        braw = get_cell(row, mapping.get("branches")).strip()
        wanted = None
        if braw:
            low = normalize_header(braw)
            if low in ("todas", "all", "todos"):
                wanted = list(branches.values())
            elif low in ("nenhuma", "none", "nao", "não"):
                wanted = []
            else:
                codes = [normalize_code(c) for c in re.split(r"[,;/|]+", braw) if c.strip()]
                bad = [c for c in codes if c not in branches]
                if bad:
                    raise ValueError(_("Filial {code} não cadastrada", code=", ".join(bad)))
                wanted = [branches[c] for c in codes]
        k = by_model.get(m.id)
        if k is None:
            k = ProductPackaging(model=m, model_id=m.id, name=m.code[:40], sort=len(prod.packagings) + 1)
            k.branches = list(branches.values()) if wanted is None else wanted
            prod.packagings.append(k)
            res.inc(N_("Embalagens criadas"))
            upd = True
        else:
            upd = opts.update_existing
            if upd:
                res.inc(N_("Embalagens atualizadas"))
                if wanted is not None:
                    k.branches = wanted
        if upd:
            if inner is not None:
                k.inner = inner
            if qty:
                k.inner_qty = qty
            if weight is not None:
                k.weight = weight
            order = num(row, "order", True)
            if order:
                k.sort = order
        if pcode := normalize_code(get_cell(row, mapping.get("pallet"))):
            if pcode not in pallets:
                raise ValueError(_("Palete {code} não é um tipo de bin com Armazena em = palete fechado", code=pcode))
            pal = pallets[pcode]
            pp = next((x for x in k.pallets if x.pallet_type_id == pal.id and (x.layers == layers or not layers)), None)
            if pp is None:
                pp = PackagingPallet(pallet_type=pal, pallet_type_id=pal.id)
                k.pallets.append(pp)
            pp.layer, pp.layers = layer or pp.layer, layers or pp.layers
            res.inc(N_("Paletizações"))
        if k.inner is None:
            from services.packaging import ensure_chain

            for x in ensure_chain(prod, k):  # o modelo contém outro: a embalagem de dentro é criada no produto
                res.inc(N_("Embalagens criadas"))
        db.flush()
        touched.setdefault(prod.id, set()).add(k.id)

    _run(db, path, opts, res, handle)
    for pid, kids in touched.items():  # o que dá para calcular pelas medidas + avisos
        prod = db.get(Product, pid)
        for k in prod.packagings:
            if k.id in kids:
                res.notes.extend(f"{prod.sku} · {n_}" for n_ in fill_chain(k, prod) + check(k, prod))
    res.notes = res.notes[:200]
    db.commit()
    return res


MASTER_KINDS = {
    "branches": {"sync_help": N_("Exclui as filiais que não estão no arquivo. Filial com endereços ou níveis é mantida."),"title": N_("Importação de Filiais"), "fields": BRANCH_FIELDS, "run": import_branches,
                 "back": ("/branches", "filiais"), "scope": False, "bins_option": False,
                 "help": [N_("Só o código é obrigatório; sem nome, o nome fica igual ao código."),
                          N_("Filiais existentes (mesmo código) têm o nome atualizado, se a opção estiver marcada."),
                          N_("Unitização (opcional): desligada (padrão), só caixas ou caixas e paletes — se as otimizações da filial usam as embalagens dos produtos e os paletes fechados.")],
                 "sample": "Codigo;Nome;Unitizacao\n01;Matriz São Paulo;caixas e paletes\n02;Filial Rio de Janeiro;desligada\n"},
    "warehouses": {"sync_help": N_("Exclui os armazéns que não estão no arquivo; com a coluna Filiais mapeada, as associações filial×armazém ficam iguais às do arquivo. Armazém (ou associação) com endereços ou níveis é mantido."),"title": N_("Importação de Armazéns"), "fields": WAREHOUSE_FIELDS, "run": import_warehouses,
                   "back": ("/warehouses", "armazéns"), "scope": False, "bins_option": False,
                   "help": [N_("Só o código é obrigatório."),
                            N_("Coluna Filiais: uma ou mais filiais separadas por vírgula (01, 02). Filiais inexistentes são criadas "
                            "com o código; as associações existentes são mantidas (só acrescenta).")],
                   "sample": "Codigo;Nome;Filiais\nCD1;Centro de Distribuição SP;01\nCD2;Centro de Distribuição RJ;02\nEXP;Expedição compartilhada;01, 02\n"},
    "bin_types": {"sync_help": N_("Exclui os tipos de bin que não estão no arquivo (com as capacidades de nível deles). Tipo usado em algum endereço, ou bin pai de um compartimento que fica, é mantido."),"title": N_("Importação de Tipos de Bin"), "fields": BIN_TYPE_FIELDS, "run": import_bin_types,
                  "back": ("/bin-types", "tipos de bin"), "scope": False, "bins_option": False,
                  "help": [N_("Só o código é obrigatório. Dimensões internas em cm, peso máximo em kg, volume em litros "
                           "(vazio = calculado pelas dimensões)."),
                           N_("Aberta em cima (sim/não): palete, bandeja, caixa sem tampa — a carga pode passar da borda, até o vão do "
                           "nível ou até o Máximo acima da borda (cm). Informar só o máximo marca a bin como aberta."),
                           N_("Compartimentos: Bin pai (código de uma bin inteira, cadastrada antes ou em linha anterior) + "
                           "Divisões na largura × no comprimento (ex.: 2 e 3 = 6 compartimentos). Alternativa: Fração (1/2…) + "
                           "Divisão (largura ou comprimento). Sem dimensões, elas vêm da bin pai."),
                           N_("Permite fracionar a bin pai (sim/não), nos compartimentos: na otimização com bins existentes, uma bin "
                           "pai sobrando pode virar compartimentos deste tipo (colocando a divisória)."),
                           N_("Armazena em (unitização): unidades (soltas), caixas (caixas inteiras do produto), ambos (caixas "
                           "inteiras + o resto solto) ou palete (palete fechado: caixas por camada × camadas). Vazio = unidades."),
                           N_("Tipos existentes são atualizados só nos campos preenchidos.")],
                  "sample": "Codigo;Nome;Altura (cm);Largura (cm);Comprimento (cm);Peso max (kg);Volume (L);Bin pai;Divisoes largura;Divisoes comprimento;Aberta em cima;Maximo acima da borda (cm);Permite fracionar a bin pai;Armazena em\n"
                            "CX-P;Caixa pequena;10;20;30;8;;;;;nao;;;unidades\nCX-M;Caixa média;20;30;40;15;;;;;nao;;;ambos\nPAL;Palete PBR;15;100;120;1000;;;;;sim;135;;palete\n"
                            "1/2 CX-P;Meia caixa pequena;;;;;;CX-P;2;1;;;sim;\n1/6 CX-M;Sexto de caixa média;;;;;;CX-M;2;3;;;nao;\n"},
    "packaging_models": {"title": N_("Importação de Modelos de embalagem"), "fields": PACKAGING_MODEL_FIELDS,
                         "run": import_packaging_models, "back": ("/packaging-models", "modelos de embalagem"),
                         "scope": False, "bins_option": False,
                         "help": [N_("Código e medidas externas (cm) da caixa; tara (kg) opcional. Modelos existentes (mesmo código) são atualizados só nos campos preenchidos."),
                                  N_("Hierarquia: Contém = o código de outro modelo (em linha anterior ou já cadastrado) e a Quantidade dele; vazia = quantas cabem pelas medidas. Ex.: o MASTER contém 4 CAIXA — ao aplicar o MASTER num produto, a CAIXA dele é criada automaticamente.")],
                         "sample": "Codigo;Nome;Altura (cm);Largura (cm);Comprimento (cm);Tara (kg);Contem;Quantidade\n"
                                   "DSP-6;Display 6 un.;10;15;20;0,05;;\nCX-P;Caixa pequena;20;30;30;0,3;DSP-6;6\n"
                                   "MST-40;Master 40;42;62;62;1;CX-P;8\n"},
    "packagings": {"title": N_("Importação de Embalagens do produto"), "fields": PACKAGING_FIELDS, "run": import_packagings,
                   "back": ("/products", "produtos"), "scope": False, "bins_option": False,
                   "help": [N_("Uma linha por produto × modelo de embalagem; para mais de um palete na mesma embalagem, repita a linha com outro palete. Os produtos e os modelos de embalagem precisam estar cadastrados (as medidas e a tara vêm do modelo)."),
                            N_("Quantidade: quantas unidades vão no modelo, ou quantas embalagens de dentro com Feita de = o código de outro modelo do mesmo produto (em linha anterior ou já cadastrado): display de 6 un. → caixa com 4 displays → master com 2 caixas. Vazia = calculada pelas medidas."),
                            N_("Filiais habilitadas: todas (padrão de uma embalagem nova), nenhuma ou os códigos (01, 02) — a otimização de uma filial só usa as embalagens habilitadas nela. Ordem: a 1ª é a primeira opção em toda bin."),
                            N_("Palete = tipo de bin com Armazena em = palete fechado; caixas por camada e camadas vazias são calculadas pelas medidas."),
                            N_("Embalagens existentes (mesmo produto e modelo) são atualizadas só nos campos preenchidos.")],
                   "sample": "SKU;Modelo de embalagem;Quantidade;Feita de;Peso cheia (kg);Filiais habilitadas;Ordem;Palete;Caixas por camada;Camadas\n"
                             "P0001;DSP-6;6;;;nenhuma;3;;;\nP0001;CX-P;4;DSP-6;;todas;1;PAL;8;5\n"
                             "P0001;CX-P;4;DSP-6;;todas;1;PAL;8;7\nP0001;MST-40;2;CX-P;;01;2;PAL;4;3\n"},
    "locations": {"sync_help": N_("Nas filiais/armazéns que aparecem no arquivo, exclui os endereços que não estão nele. Endereço ocupado ou com item atrelado é mantido."),"title": N_("Importação de Endereços"), "fields": LOCATION_FIELDS, "run": import_locations,
                  "back": ("/locations", "endereços"), "scope": True, "bins_option": True,
                  "help": [N_("Informe o endereço completo (A-01-02-03) OU as colunas Corredor, Estante, Nível, Posição e "
                           "Compartimento (só o corredor é obrigatório). Se as duas formas forem mapeadas, valem as colunas separadas."),
                           N_("Filiais e armazéns inexistentes são criados com o código. Endereços existentes não são duplicados; "
                           "conflitos de hierarquia (posição genérica × compartimentos) são recusados por linha."),
                           N_("Tipo de bin + Qtd (vazio = 1): várias linhas do mesmo endereço somam. Os tipos de bin precisam estar cadastrados."),
                           N_("Depois de gravar as bins são aplicadas as regras de níveis (níveis dedicados e vão).")],
                  "sample": "Filial;Armazem;Corredor;Estante;Nivel;Posicao;Compartimento;Tipo de bin;Qtd bins\n"
                            "01;CD1;A;01;01;01;;CX-P;2\n01;CD1;A;01;01;02;;CX-P;2\n01;CD1;B;01;01;01;A;CX-M;1\n01;CD1;P;01;01;01;;PAL;1\n"},
    "levels": {"sync_help": N_("Nos níveis das filiais/armazéns do arquivo, remove os limites (nível × tipo de bin) que o arquivo não informa e — com a coluna Vão mapeada — o vão dos níveis que ele não informa. A capacidade calculada pela estrutura e os níveis dedicados (automáticos) ficam."),"title": N_("Importação de Capacidade dos Níveis"), "fields": LEVEL_FIELDS, "run": import_levels,
               "back": ("/levels", "níveis"), "scope": True, "bins_option": False,
               "help": [N_("Cada linha aplica capacidade e/ou vão a um conjunto de níveis: Corredor (obrigatório), Estante e Nível "
                        "aceitam valor único, lista (01,03) ou faixa (01-04); vazio ou \"(todas)\" = todos."),
                        N_("Tipo de bin + Cabem = \"neste nível cabem X bins do tipo\" (0 remove a capacidade do tipo). Vão em cm."),
                        N_("Nas estantes com estrutura, a capacidade já é calculada pelas medidas: o número importado vira um limite manual e vale quando é menor que o calculado."),
                        N_("Filiais, armazéns, tipos de bin e endereços precisam existir (gere os endereços antes).")],
               "sample": "Filial;Armazem;Corredor;Estante;Nivel;Tipo de bin;Cabem;Vao (cm)\n"
                         "01;CD1;A;;;CX-P;8;25\n01;CD1;B;01-03;02-03;CX-M;3;30\n01;CD1;P;;;PAL;2;150\n"},
}


# estruturas (peças, tipos com composição, estruturas e estrutura das estantes): services/structure_importer
from services.structure_importer import STRUCTURE_KINDS  # noqa: E402

MASTER_KINDS.update(STRUCTURE_KINDS)
