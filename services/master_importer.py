"""
Importação dos cadastros-base via CSV/XLSX (mesmo fluxo das importações de produtos e saldos):
  - Filiais                (código, nome)
  - Armazéns               (código, nome, filiais — lista "01, 02")
  - Tipos de bin           (código, nome, dimensões, peso máx., volume)
  - Endereços              (filial, armazém, endereço OU corredor/estante/nível/posição/compartimento, tipo de bin, qtd)
  - Capacidade dos níveis  (filial, armazém, corredor, estante, nível — aceitam faixas —, tipo de bin, máx., vão)

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
from services.file_reader import get_cell, iter_rows, parse_decimal
from services.stock_service import MasterDataCache
from services.i18n import N_, _

CHUNK = 500
LIST_SPLIT = re.compile(r"[,;|/]+")
ALL_WORDS = {"", "*", "TODAS", "TODOS", N_("(TODAS)"), N_("(TODOS)"), "TUDO"}

# campo -> (rótulo, obrigatório, sinônimos)
BRANCH_FIELDS = {
    "code": (N_("Código da filial"), True, ("codigo", "código", "cod", "filial", "codfilial", "codigofilial", "code", "branch")),
    "name": (N_("Nome"), False, ("nome", "name", "descricao", "descrição", "razaosocial", "nomefilial")),
}
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
    "parent": (N_("Bin pai"), False, ("binpai", "pai", "parent", "tipopai", "bininteira")),
    "div_w": (N_("Divisões na largura"), False, ("divisoesnalargura", "divisoeslargura", "colunas", "nalargura", "divlargura", "divisoesl")),
    "div_l": (N_("Divisões no comprimento"), False, ("divisoesnocomprimento", "divisoescomprimento", "fileiras", "nocomprimento", "divcomprimento", "divisoesc")),
    "fraction": (N_("Fração (ex.: 1/2) — alternativa às divisões"), False, ("fracao", "fração", "fraction", "compartimento")),
    "split_axis": (N_("Divisão (largura/comprimento)"), False, ("divisao", "divisão", "eixo", "split", "dividirna")),
}
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


def _sync(res: N_("MasterResult"), opts: MasterOptions, fn) -> None:
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
        if existing is None:
            b = Branch(code=code, name=name or code)
            db.add(b)
            db.flush()
            master.branches[code] = b.id
            seen.add(b.id)
            res.inc(N_("Filiais criadas"))
        elif opts.update_existing and name:
            b = db.get(Branch, existing)
            if b.name != name:
                b.name = name
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
    if adj:
        db.commit()
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
        spilled = spill_message(db, spill_overflow(db, touched_bays))
        res.notes += [m for m in (adj, spilled) if m]
        db.commit()
    res.notes = master.notes + res.notes

    def do():
        from services.import_sync import sync_levels
        cnt, notes = sync_levels(db, scopes, cap_seen, height_seen if mapping.get("height") is not None else None)
        res.counters.update(cnt)
        res.notes += notes
        db.commit()
    _sync(res, opts, do)
    return res


MASTER_KINDS = {
    "branches": {"sync_help": N_("Exclui as filiais que não estão no arquivo. Filial com endereços ou níveis é mantida."),"title": N_("Importação de Filiais"), "fields": BRANCH_FIELDS, "run": import_branches,
                 "back": ("/branches", "filiais"), "scope": False, "bins_option": False,
                 "help": [N_("Só o código é obrigatório; sem nome, o nome fica igual ao código."),
                          N_("Filiais existentes (mesmo código) têm o nome atualizado, se a opção estiver marcada.")],
                 "sample": "Codigo;Nome\n01;Matriz São Paulo\n02;Filial Rio de Janeiro\n"},
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
                           N_("Compartimentos: Bin pai (código de uma bin inteira, cadastrada antes ou em linha anterior) + "
                           "Divisões na largura × no comprimento (ex.: 2 e 3 = 6 compartimentos). Alternativa: Fração (1/2…) + "
                           "Divisão (largura ou comprimento). Sem dimensões, elas vêm da bin pai."),
                           N_("Tipos existentes são atualizados só nos campos preenchidos.")],
                  "sample": "Codigo;Nome;Altura (cm);Largura (cm);Comprimento (cm);Peso max (kg);Volume (L);Bin pai;Divisoes largura;Divisoes comprimento\n"
                            "CX-P;Caixa pequena;10;20;30;8;;;;\nCX-M;Caixa média;20;30;40;15;;;;\nPAL;Palete PBR;120;100;120;1000;;;;\n"
                            "1/2 CX-P;Meia caixa pequena;;;;;;CX-P;2;1\n1/6 CX-M;Sexto de caixa média;;;;;;CX-M;2;3\n"},
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
    "levels": {"sync_help": N_("Nos níveis das filiais/armazéns do arquivo, remove as capacidades (nível × tipo de bin) que o arquivo não informa e — com a coluna Vão mapeada — o vão dos níveis que ele não informa. Níveis dedicados (automáticos) ficam."),"title": N_("Importação de Capacidade dos Níveis"), "fields": LEVEL_FIELDS, "run": import_levels,
               "back": ("/levels", "níveis"), "scope": True, "bins_option": False,
               "help": [N_("Cada linha aplica capacidade e/ou vão a um conjunto de níveis: Corredor (obrigatório), Estante e Nível "
                        "aceitam valor único, lista (01,03) ou faixa (01-04); vazio ou \"(todas)\" = todos."),
                        N_("Tipo de bin + Cabem = \"neste nível cabem X bins do tipo\" (0 remove a capacidade do tipo). Vão em cm."),
                        N_("Filiais, armazéns, tipos de bin e endereços precisam existir (gere os endereços antes).")],
               "sample": "Filial;Armazem;Corredor;Estante;Nivel;Tipo de bin;Cabem;Vao (cm)\n"
                         "01;CD1;A;;;CX-P;8;25\n01;CD1;B;01-03;02-03;CX-M;3;30\n01;CD1;P;;;PAL;2;150\n"},
}
