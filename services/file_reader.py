"""
Leitura de arquivos tabulares (CSV/TXT/XLSX) em modo streaming, com:
- detecção de encoding (UTF-8 / UTF-8 BOM / Latin-1) e delimitador (; , TAB |)
- leitura de XLSX via openpyxl em modo read_only (não carrega a planilha inteira)
- mapeamento dinâmico de colunas por índice OU por nome
- conversão de números no formato brasileiro (1.234,56) e internacional (1234.56)
"""
from __future__ import annotations

import csv
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from database import BASE_DIR

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".csv", ".txt", ".xlsx", ".xlsm"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}\.(csv|txt|xlsx|xlsm)$")


# --------------------------------------------------------------------------- #
# Upload temporário
# --------------------------------------------------------------------------- #
async def save_upload(upload) -> str:
    """Salva o UploadFile em /uploads e retorna um token (nome do arquivo)."""
    filename = upload.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Formato '{ext or 'desconhecido'}' não suportado. Use CSV, TXT ou XLSX "
            "(arquivos .xls antigos devem ser salvos como .xlsx)."
        )
    token = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / token
    size = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                fh.close()
                dest.unlink(missing_ok=True)
                raise ValueError("Arquivo excede o limite de 200 MB.")
            fh.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ValueError("Arquivo vazio.")
    return token


def token_path(token: str) -> Path:
    if not token or not _TOKEN_RE.match(token):
        raise ValueError("Token de arquivo inválido.")
    path = UPLOAD_DIR / token
    if not path.exists():
        raise ValueError("Arquivo enviado não encontrado (expirado). Envie novamente.")
    return path


def remove_upload(token: str) -> None:
    try:
        token_path(token).unlink(missing_ok=True)
    except ValueError:
        pass


def cleanup_old_uploads(max_age_hours: int = 24) -> None:
    limit = time.time() - max_age_hours * 3600
    for f in UPLOAD_DIR.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < limit:
                f.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Conversão de células
# --------------------------------------------------------------------------- #
def cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def parse_decimal(value) -> Optional[Decimal]:
    """Converte texto em Decimal. Vazio -> None. Inválido -> ValueError.

    Aceita: '10', '10,5', '1.234,56', '1,234.56', '1234.56', ' R$ 12,00 ', '-3'.
    Regra: se houver '.' e ',', o separador que aparecer por último é o decimal.
    Se houver somente ',', ela é decimal. Se houver vários '.', são de milhar.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-+]", "", text)
    if not text or text in {"-", "+"}:
        raise ValueError(f"Número inválido: {value!r}")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if text.count(",") > 1:
            raise ValueError(f"Número inválido: {value!r}")
        text = text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Número inválido: {value!r}") from exc


def normalize_header(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# --------------------------------------------------------------------------- #
# Leitura de arquivos
# --------------------------------------------------------------------------- #
@dataclass
class CsvDialectInfo:
    encoding: str
    delimiter: str


def detect_csv(path: Path) -> CsvDialectInfo:
    raw = path.read_bytes()[:64 * 1024]
    encoding = "utf-8-sig"
    try:
        sample = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        encoding = "latin-1"
        sample = raw.decode("latin-1")
    # Remove eventual linha cortada no fim do sample.
    if "\n" in sample:
        sample = sample[: sample.rfind("\n")]
    delimiter = ";"
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        counts = {d: first.count(d) for d in (";", ",", "\t", "|")}
        best = max(counts, key=counts.get)
        delimiter = best if counts[best] > 0 else ";"
    return CsvDialectInfo(encoding, delimiter)


def _iter_raw_rows(path: Path) -> Iterator[list[str]]:
    ext = path.suffix.lower()
    if ext in {".csv", ".txt"}:
        info = detect_csv(path)
        with path.open("r", encoding=info.encoding, newline="") as fh:
            for row in csv.reader(fh, delimiter=info.delimiter):
                yield [c.strip() for c in row]
    elif ext in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            for row in ws.iter_rows(values_only=True):
                yield [cell_to_str(c) for c in row]
        finally:
            wb.close()
    else:
        raise ValueError(f"Extensão não suportada: {ext}")


def iter_rows(path: Path, has_header: bool = True) -> Iterator[tuple[int, list[str]]]:
    """Gera (numero_da_linha_no_arquivo, valores) ignorando linhas totalmente vazias."""
    for line_no, row in enumerate(_iter_raw_rows(path), start=1):
        if has_header and line_no == 1:
            continue
        if not any(c for c in row):
            continue
        yield line_no, row


def read_preview(path: Path, has_header: bool = True, limit: int = 8) -> tuple[list[str], list[list[str]]]:
    """Retorna cabeçalhos e algumas linhas de amostra."""
    raw = list(islice(_iter_raw_rows(path), limit + 1))
    if not raw:
        raise ValueError("O arquivo não possui linhas.")
    width = max(len(r) for r in raw)
    if has_header:
        headers = [h or f"Coluna {i + 1}" for i, h in enumerate(raw[0] + [""] * (width - len(raw[0])))]
        rows = raw[1:]
    else:
        headers = [f"Coluna {i + 1}" for i in range(width)]
        rows = raw[:limit]
    rows = [r + [""] * (width - len(r)) for r in rows]
    return headers, rows


def chunked(iterable: Iterable, size: int) -> Iterator[list]:
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            return
        yield chunk


# --------------------------------------------------------------------------- #
# Mapeamento de colunas
# --------------------------------------------------------------------------- #
def resolve_column(spec, headers: Sequence[str]) -> Optional[int]:
    """Resolve uma coluna por índice (int / '3' / '#3' base 0) ou por nome.

    - int ou dígitos           -> índice base 0 (como enviado pelos <select> da tela)
    - '#N' ou 'col:N'          -> índice base 1 (conveniência para uso via API)
    - texto                    -> nome da coluna (comparação sem acento/caixa)
    """
    if spec is None:
        return None
    if isinstance(spec, int):
        idx = spec
    else:
        s = str(spec).strip()
        if s == "" or s == "-1":
            return None
        if s.isdigit():
            idx = int(s)
        elif s.startswith("#") and s[1:].isdigit():
            idx = int(s[1:]) - 1
        elif s.lower().startswith("col:") and s[4:].isdigit():
            idx = int(s[4:]) - 1
        else:
            target = normalize_header(s)
            for i, h in enumerate(headers):
                if normalize_header(h) == target:
                    return i
            raise ValueError(f"Coluna '{s}' não encontrada no arquivo.")
    if idx < 0 or (headers and idx >= len(headers)):
        raise ValueError(f"Índice de coluna fora do intervalo: {idx}")
    return idx


def auto_map(headers: Sequence[str], synonyms: dict[str, Sequence[str]]) -> dict[str, Optional[int]]:
    """Sugere o mapeamento de cada campo com base em sinônimos de cabeçalho."""
    normalized = [normalize_header(h) for h in headers]
    result: dict[str, Optional[int]] = {}
    used: set[int] = set()
    for field_name, names in synonyms.items():
        result[field_name] = None
        targets = [normalize_header(n) for n in names]
        for t in targets:
            for i, h in enumerate(normalized):
                if i not in used and h == t:
                    result[field_name] = i
                    used.add(i)
                    break
            if result[field_name] is not None:
                break
    return result


def get_cell(row: Sequence[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()
