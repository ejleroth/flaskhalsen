@echo off
rem Run from source: creates the venv on first start, then launches the app.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Forsta start: installerar beroenden...
  python -m venv .venv || (echo Python 3.11+ kravs: https://www.python.org/downloads/ & pause & exit /b 1)
  .venv\Scripts\pip install -q -r requirements.txt
)
.venv\Scripts\python run.py
pause
