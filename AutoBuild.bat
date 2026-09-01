@echo off
title Autonomous Dev Agent Dashboard
echo ========================================================
echo   Starting Autonomous Dev Agent on http://localhost:6969
echo ========================================================

:: Navigate to the folder containing this batch script
cd /d "%~dp0"

:: Launch the browser in a background process after a 2-second buffer
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:6969"

:: Start the Uvicorn server using Python 3.13
py -3.13 -m uvicorn server:app --reload --port 6969

pause