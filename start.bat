@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск монітора арбітражу Binance-OKX...
echo Для зупинки натисніть Ctrl+C або закрийте це вікно.
echo.
venv\Scripts\python.exe main.py
pause
