@echo off
chcp 65001 >nul
title FundedNext EURUSD Trading Bot
echo ============================================================
echo   FundedNext EURUSD Trading Bot - Inicio
echo ============================================================
echo.

:: Verificar Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python no esta instalado o no esta en PATH.
    echo Descargalo de https://www.python.org/downloads/
    echo IMPORTANTE: Marca "Add Python to PATH" al instalar.
    pause
    exit /b 1
)

:: Instalar dependencias
echo [1/3] Instalando dependencias...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [ERROR] Fallo al instalar dependencias.
    pause
    exit /b 1
)

:: Instalar MetaTrader5 (Windows only)
pip install MetaTrader5 -q
if %errorlevel% neq 0 (
    echo [ERROR] Fallo al instalar MetaTrader5.
    echo Asegurate de tener MT5 instalado en tu PC.
    pause
    exit /b 1
)

:: Verificar que config tiene credenciales
echo.
echo [2/3] Verificando configuracion...
echo Asegurate de que:
echo   - MT5 esta abierto y conectado
echo   - EURUSD esta en Market Watch
echo   - Completaste el password en config.yaml o env var MT5_PASSWORD
echo   - Completaste bot_token/chat_id de Telegram (o env vars)
echo.

:: Cargar variables de entorno si existe set_env.bat
if exist set_env.bat (
    echo Cargando variables de entorno desde set_env.bat...
    call set_env.bat
)

:: Iniciar el bot
echo [3/3] Iniciando bot en modo LIVE...
echo ============================================================
echo.
python main.py --mode live

:: Si el bot se cierra, no cerrar la ventana
echo.
echo [!] El bot se detuvo. Revisa los logs en la carpeta logs/
pause
