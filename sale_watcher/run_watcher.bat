@echo off
REM Install deps + Chromium for the Sale Watcher, then launch it.
cd /d "%~dp0"

echo Installing Python packages...
python -m pip install -r requirements.txt || goto :error

echo.
echo Installing Playwright Chromium browser (one-time)...
python -m playwright install chromium || goto :error

echo.
echo Starting Sale Watcher...
python app.py
exit /b 0

:error
echo.
echo Setup failed. Make sure Python is on your PATH.
pause
exit /b 1
