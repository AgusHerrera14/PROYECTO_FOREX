@echo off
echo ============================================
echo   Descargando datos historicos de EURUSD
echo   Fuente: Dukascopy (gratis, 2018-2026)
echo   Esto tarda entre 15-30 minutos
echo ============================================
echo.

cd /d "%~dp0"

pip install requests pandas numpy PyYAML 2>nul

python data_export.py --source dukascopy --start 2018-01-01 --end 2026-03-01

echo.
echo ============================================
echo   LISTO! Datos guardados en data\EURUSD_H1.csv
echo   Ahora podes correr el backtest:
echo   python main.py --mode backtest
echo ============================================
echo.
pause
