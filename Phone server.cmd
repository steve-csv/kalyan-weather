@echo off
REM Serves the forecast dashboard on your home WiFi so your phone can open it,
REM with a Refresh button that rebuilds today's forecast on this PC.
REM
REM Leave this window open while you want the phone to reach it.
REM Close the window (or press Ctrl+C) to stop.

cd /d "%~dp0"

if not exist "forecasts\index.html" (
    echo No dashboard built yet - building one first, takes about a minute...
    python -m wxagent daily --no-notify
)

echo.
python -m wxagent serve --port 8000

REM If the window vanished instantly, something failed - keep it open to read.
pause
