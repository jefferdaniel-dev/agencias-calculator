@echo off
rem Abre la Calculadora de Agencias con doble clic.
chcp 65001 >nul
title Calculadora de Agencias
cd /d "%~dp0src"
python -c "import requests, colorama, pyperclip" 2>nul
if errorlevel 1 (
  echo Faltan Python o sus dependencias. Ejecuta una vez en la carpeta del proyecto:
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)
python calculosAgencias.py
if errorlevel 1 pause
