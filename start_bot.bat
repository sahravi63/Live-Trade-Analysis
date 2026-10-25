@echo off
REM Runs the bot in this window (Ctrl+C to stop). Keep MetaTrader 5 open + logged in for MT5 data.
cd /d "%~dp0"
title SMC Market Bot
python bot.py run
pause
