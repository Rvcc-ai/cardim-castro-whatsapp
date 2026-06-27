@echo off
chcp 65001 > nul
title Cristina — Bot WhatsApp Cardim & Castro

echo.
echo  =========================================
echo   Cardim & Castro Advocacia
echo   Bot Cristina — Atendimento WhatsApp
echo  =========================================
echo.

cd /d "%~dp0"

REM Verificar se .env tem credenciais preenchidas
findstr /C:"PREENCHER" .env > nul
if %errorlevel%==0 (
    echo  [ERRO] Preencha o arquivo .env com suas credenciais antes de iniciar.
    echo  Abra o arquivo .env e substitua os campos "PREENCHER" pelos valores reais.
    echo.
    pause
    exit /b 1
)

echo  [OK] Credenciais encontradas
echo  [..] Iniciando servidor na porta 8000...
echo.
echo  Painel de conversas: http://localhost:8000/conversas
echo  Health check:        http://localhost:8000/
echo.
echo  Para expor na internet (webhook Meta), abra outro terminal e execute:
echo    ngrok http 8000
echo.
echo  Pressione Ctrl+C para parar o bot.
echo.

.venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8000 --reload
