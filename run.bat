@echo off
REM ---------------------------------------------------------------
REM  PinClicks Mining Tool - one-click setup and launch.
REM  First run installs everything; later runs just start the server.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv || goto :fail
)

set PY=.venv\Scripts\python.exe

if not exist ".venv\.deps-installed" (
    echo [setup] Installing dependencies...
    "%PY%" -m pip install --upgrade pip --quiet || goto :fail
    "%PY%" -m pip install -r requirements.txt --quiet || goto :fail
    echo [setup] Downloading Chromium for Playwright...
    "%PY%" -m playwright install chromium || goto :fail
    echo done > ".venv\.deps-installed"
)

echo [run] Starting PinClicks Mining Tool...
"%PY%" -m app.main
goto :eof

:fail
echo.
echo [error] Setup failed. See the messages above.
pause
exit /b 1
