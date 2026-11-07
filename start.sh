#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Neokioski - inicia o servidor web local (http://127.0.0.1:8000) no Linux/macOS
#
# Funciona com qualquer Python 3.11 ou mais novo instalado:
#   - sem .venv: cria o ambiente virtual com o Python mais novo encontrado (python3.X, python3, python);
#   - .venv criada com Python anterior ao 3.11: recria;
#   - requirements.txt mudou desde a última instalação: atualiza as dependências.
# Forçar um Python:  NK_PYTHON=/caminho/python3.13 ./start.sh      Mudar a porta:  NK_PORT=8080 ./start.sh
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

VENV_PY=".venv/bin/python"
STAMP=".venv/requirements.installed"
CHECK='import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
PORT="${NK_PORT:-8000}"

ok() { "$@" -c "$CHECK" >/dev/null 2>&1; }

find_python() {
  if [ -n "${NK_PYTHON:-}" ]; then
    if ok "$NK_PYTHON"; then echo "$NK_PYTHON"; return; fi
    echo "[AVISO] NK_PYTHON=$NK_PYTHON não é um Python 3.11 ou superior; procurando outro..." >&2
  fi
  # python3.X do PATH, do mais novo para o mais antigo; depois python3 e python
  local cands
  cands=$(compgen -c python3. 2>/dev/null | grep -E '^python3\.[0-9]+$' | sort -t. -k2,2nr -u || true)
  for c in $cands python3 python; do
    if command -v "$c" >/dev/null 2>&1 && ok "$c"; then echo "$c"; return; fi
  done
}

# 1) .venv feita com Python antigo (ou quebrada): recria
if [ -x "$VENV_PY" ] && ! ok "$VENV_PY"; then
  echo "[AVISO] A .venv atual usa Python anterior ao 3.11 ou está corrompida. Recriando..."
  rm -rf .venv
fi

# 2) cria a .venv com o Python mais novo disponível (3.11+)
if [ ! -x "$VENV_PY" ]; then
  PY="$(find_python)"
  if [ -z "$PY" ]; then
    echo "[ERRO] Nenhum Python 3.11 ou superior encontrado. Instale (ex.: sudo apt install python3.12 python3.12-venv)" >&2
    echo "       ou informe o caminho: NK_PYTHON=/caminho/python3 ./start.sh" >&2
    exit 1
  fi
  echo "Criando o ambiente virtual .venv com Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)..."
  "$PY" -m venv .venv
  rm -f "$STAMP"
fi

# 3) instala/atualiza as dependências quando o requirements.txt muda
if ! cmp -s requirements.txt "$STAMP"; then
  echo "Instalando as dependências (requirements.txt)..."
  "$VENV_PY" -m pip install --disable-pip-version-check -q --upgrade pip
  "$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt
  cp requirements.txt "$STAMP"
fi

echo "Neokioski com Python $("$VENV_PY" -c 'import platform; print(platform.python_version())') em http://127.0.0.1:$PORT  (Ctrl+C para encerrar)"
exec "$VENV_PY" -m uvicorn main:app --host 127.0.0.1 --port "$PORT"
