@echo off
REM ─── Launch J.A.R.V.I.S. (no console window, cinematic) ───
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" run.py
