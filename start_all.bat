@echo off
REM Starts the API (port 8000) and the dashboard (port 5173). Start the bot from the dashboard.
cd /d "%~dp0"
start "SMC API" cmd /k "python -m uvicorn api:app --port 8000"
start "SMC Dashboard" cmd /k "cd smc-dashboard && npm run dev"
timeout /t 6 >nul
start http://localhost:5173
