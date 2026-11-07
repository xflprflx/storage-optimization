@echo off
REM ---------------------------------------------------------------------------
REM Neokioski - inicia o servidor web local (http://127.0.0.1:8000)
REM
REM Funciona com qualquer Python 3.11 ou mais novo instalado:
REM   - sem .venv: cria o ambiente virtual com o Python mais novo encontrado
REM     (py -3, depois python / python3 do PATH);
REM   - .venv criada com Python anterior ao 3.11: recria;
REM   - requirements.txt mudou desde a ultima instalacao: atualiza as dependencias.
REM Para forcar um Python especifico:  set NK_PYTHON=C:\caminho\python.exe  (ou "py -3.13")
REM Para mudar a porta:                 set NK_PORT=8080
REM ---------------------------------------------------------------------------
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "STAMP=.venv\requirements.installed"
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if not defined NK_PORT set "NK_PORT=8000"

REM 1) .venv existente feita com Python antigo (ou quebrada): recria
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "%CHECK%" >nul 2>&1
    if errorlevel 1 (
        echo [AVISO] A .venv atual usa Python anterior ao 3.11 ou esta corrompida. Recriando...
        rmdir /s /q .venv
    )
)

REM 2) cria a .venv com o Python mais novo disponivel (3.11+)
if not exist "%VENV_PY%" (
    call :find_python
    if not defined PY_CMD (
        echo [ERRO] Nenhum Python 3.11 ou superior encontrado.
        echo Instale em https://www.python.org/downloads/ ^(marque "Add python.exe to PATH"^)
        echo ou informe o caminho:  set NK_PYTHON=C:\caminho\python.exe
        pause
        exit /b 1
    )
    for /f "delims=" %%V in ('call !PY_CMD! -c "import platform; print(platform.python_version())"') do set "PY_VER=%%V"
    echo Criando o ambiente virtual .venv com Python !PY_VER! ^(!PY_CMD!^)...
    !PY_CMD! -m venv .venv
    if errorlevel 1 (
        echo [ERRO] Nao foi possivel criar o ambiente virtual.
        pause
        exit /b 1
    )
    if exist "%STAMP%" del "%STAMP%"
)

REM 3) instala/atualiza as dependencias quando o requirements.txt muda
fc /b requirements.txt "%STAMP%" >nul 2>&1
if errorlevel 1 (
    echo Instalando as dependencias ^(requirements.txt^)...
    "%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
    "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo [ERRO] Falha ao instalar as dependencias. Verifique a conexao e tente de novo.
        pause
        exit /b 1
    )
    copy /y requirements.txt "%STAMP%" >nul
)

for /f "delims=" %%V in ('call %VENV_PY% -c "import platform; print(platform.python_version())"') do set "PY_VER=%%V"
echo Neokioski com Python !PY_VER! em http://127.0.0.1:%NK_PORT%  ^(Ctrl+C para encerrar^)
"%VENV_PY%" -m uvicorn main:app --host 127.0.0.1 --port %NK_PORT%
pause
exit /b 0

REM ---------------------------------------------------------------------------
REM Procura um Python 3.11+: NK_PYTHON, py -3 (o mais novo instalado), python, python3
REM ---------------------------------------------------------------------------
:find_python
set "PY_CMD="
if defined NK_PYTHON (
    !NK_PYTHON! -c "%CHECK%" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=!NK_PYTHON!"
    if not defined PY_CMD echo [AVISO] NK_PYTHON=!NK_PYTHON! nao e um Python 3.11 ou superior; procurando outro...
)
if not defined PY_CMD (
    py -3 -c "%CHECK%" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    python -c "%CHECK%" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
    python3 -c "%CHECK%" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python3"
)
exit /b 0
