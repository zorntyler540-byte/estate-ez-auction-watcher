@echo off
cd /d "%~dp0"
echo Estate-Ez Auction Watcher - Live Watch + Reports
echo.
python -c "import customtkinter, requests" 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install -r requirements.txt
  python -m playwright install chromium
)
python suite_app.py
if errorlevel 1 pause
