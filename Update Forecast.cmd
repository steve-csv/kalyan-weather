@echo off
REM Refresh the forecast and publish it to the website.
REM Double-click this file - nothing to type.
REM
REM cd /d "%~dp0" moves to the folder this file lives in, so the script works
REM no matter where it is launched from (double-click, shortcut, or Task
REM Scheduler, which starts in C:\Windows\System32 by default).

cd /d "%~dp0"
title Update Kalyan West forecast

echo.
echo  ============================================
echo   Refreshing the Kalyan West forecast
echo  ============================================
echo.
echo  Fetching the models. This takes under a minute.
echo.

python -m wxagent daily --no-notify
if errorlevel 1 goto failed

echo.
echo  Updating the Mumbai MMR week...
echo.

python -m wxagent weekly
if errorlevel 1 goto failed

echo.
echo  Publishing to the website...
echo.

python -m wxagent gh-publish
if errorlevel 1 goto failed

echo.
echo  ============================================
echo   Done.
echo  ============================================
echo.
echo  Your page:  https://steve-csv.github.io/kalyan-weather/
echo.
echo  GitHub takes a couple of minutes to serve the new copy, and your
echo  phone may hold the old one a little longer. If it still looks old,
echo  pull down to refresh or add ?v=2 to the end of the address.
echo.
pause
exit /b 0

:failed
echo.
echo  ============================================
echo   Something went wrong - see the message above
echo  ============================================
echo.
echo  Nothing was published, so the website still shows the last good
echo  forecast rather than a broken one.
echo.
echo  The usual causes:
echo    - no internet connection
echo    - Open-Meteo temporarily unavailable (try again in a few minutes)
echo.
pause
exit /b 1
