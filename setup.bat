@echo off
REM ─── One-time setup: build the venv, install deps, download models ───
cd /d "%~dp0"
echo ============================================
echo   J.A.R.V.I.S.  -  setup
echo ============================================

echo [1/4] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo Failed to create venv. Is Python 3.10+ installed and on PATH?
    pause & exit /b 1
)

echo [2/4] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip ^
    --trusted-host pypi.org --trusted-host files.pythonhosted.org

echo [3/4] Installing dependencies (this can take a few minutes)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt ^
    --trusted-host pypi.org --trusted-host files.pythonhosted.org
if errorlevel 1 (
    echo Dependency install failed. See the messages above.
    pause & exit /b 1
)

echo [4/4] Downloading voice models (whisper + hey_jarvis)...
".venv\Scripts\python.exe" setup_models.py

echo.
echo Done!  Double-click start.bat to launch JARVIS.
pause
