@echo off
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
py -3 browndust2_bargain_bot.py %*
pause
