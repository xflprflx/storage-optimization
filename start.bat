@echo off
REM Neokioski - inicia o servidor web local na porta 8000
cd /d "%~dp0"

REM Verifica se o ambiente virtual existe antes de rodar
if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Ambiente virtual .venv nao encontrado na pasta do projeto!
    echo Crie o ambiente com: py -3.11 -m venv .venv
    pause
    exit /b 1
)

REM Executa o uvicorn diretamente pelo Python do ambiente virtual
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
pause