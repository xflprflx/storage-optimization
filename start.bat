@echo off
REM Neokioski - inicia o servidor web local na porta 8000
REM Pre-requisito: pip install -r requirements.txt
cd /d "%~dp0"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
