@echo off
:: JARVIS test runner — offline suite + HUD runtime smoke + live integration.
setlocal
cd /d "%~dp0"

echo ============================================================
echo  1) OFFLINE SUITE  (brain fallback, skills, speech, clap...)
echo ============================================================
call .venv\Scripts\python.exe tests\test_offline.py || goto :fail

echo.
echo ============================================================
echo  2) HUD RUNTIME SMOKE  (loads ui\jarvis.html headlessly)
echo ============================================================
where node >nul 2>nul && ( node tests\hud_smoke.mjs || goto :fail ) || echo   (node not found — skipping HUD smoke)

echo.
echo ============================================================
echo  3) LIVE INTEGRATION  (real Claude brain — needs network)
echo ============================================================
call .venv\Scripts\python.exe tests\smoke.py || goto :fail

echo.
echo ALL TEST SUITES PASSED
exit /b 0

:fail
echo.
echo TESTS FAILED
exit /b 1
