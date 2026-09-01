@echo off
title Autonomous Dev Agent Dashboard
echo ========================================================
echo   Starting Autonomous Dev Agent on http://localhost:6969
echo ========================================================

cd /d "C:\Users\shoai\autonomous_dev_agent"

:: Launch the default browser in the background after a 2-second delay
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:6969"

:: Start the Uvicorn server explicitly with Python 3.13
py -3.13 -m uvicorn server:app --reload --port 6969

pause