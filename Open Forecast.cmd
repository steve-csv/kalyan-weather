@echo off
setlocal enabledelayedexpansion
title Kalyan Forecast

REM One-click entry point.
REM   1. starts the local server if it is not already running
REM   2. waits until it actually answers
REM   3. opens the forecast in your browser
REM   4. prints the phone address
REM
REM The waiting matters: opening the browser immediately after launching the
REM server races it, and the browser shows "connection refused" on a server
REM that is about to be perfectly fine a second later.

cd /d "%~dp0"

set PORT=8000
set URL=http://localhost:%PORT%/

echo.
echo   Kalyan West forecast
echo   --------------------
echo.

REM --- is something already listening on the port? --------------------------
set RUNNING=
for /f "tokens=*" %%A in ('netstat -ano -p TCP ^| findstr /r /c:":%PORT% .*LISTENING"') do set RUNNING=1

if defined RUNNING (
    echo   Server already running.
) else (
    echo   Starting server...
    if not exist "forecasts\index.html" (
        echo   No forecast built yet - building one first, takes about a minute.
        python -m wxagent daily --no-notify
    )
    start "Kalyan weather server" cmd /c "python -m wxagent serve --port %PORT% & pause"
)

REM --- wait for it to actually answer, up to ~20 seconds --------------------
echo   Waiting for the server to respond...
set READY=
for /l %%i in (1,1,20) do (
    if not defined READY (
        powershell -NoProfile -Command "try{ (Invoke-WebRequest 'http://localhost:%PORT%/api/status' -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
        if !errorlevel! equ 0 set READY=1
        if not defined READY timeout /t 1 /nobreak >nul
    )
)

if not defined READY (
    echo.
    echo   The server did not come up. Check the "Kalyan weather server"
    echo   window for an error - the usual cause is another program already
    echo   using port %PORT%.
    echo.
    pause
    exit /b 1
)

REM --- find the LAN address for the phone -----------------------------------
for /f "usebackq tokens=*" %%B in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } ^| Select-Object -First 1).IPAddress"`) do set LANIP=%%B

echo   Ready.
echo.
echo   On this PC : %URL%
if defined LANIP echo   On phone   : http://%LANIP%:%PORT%/
echo.
echo   The phone address works on your home WiFi only, and only while the
echo   "Kalyan weather server" window stays open.
echo.

start "" "%URL%"

timeout /t 8 /nobreak >nul
