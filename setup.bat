@echo off
cd /d "%~dp0"
echo Installing Python packages...
python -m pip install -r requirements.txt
echo Installing dashboard packages...
cd smc-dashboard
call npm install
cd ..
echo.
echo Done. Run start_all.bat to launch the API + dashboard, or start_bot.bat for the bot alone.
pause
