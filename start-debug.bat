@echo off
REM ─── Launch J.A.R.V.I.S. with a visible console (see logs/errors) ───
cd /d "%~dp0"
".venv\Scripts\python.exe" run.py
echo.
echo JARVIS exited. Press any key to close.
pause >nul
