@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  Build Estate-Ez Auction Watcher for easy sharing (no Python needed).
REM
REM  Output:
REM    dist\Estate-Ez Auction Watcher\     <- give this whole folder
REM    dist\Estate-Ez-Auction-Watcher.zip  <- or send this zip
REM ============================================================

set "APP_NAME=Estate-Ez Auction Watcher"
set "DIST_DIR=dist\%APP_NAME%"
set "ZIP_PATH=dist\Estate-Ez-Auction-Watcher.zip"

echo.
echo === Closing any running copy ===
taskkill /F /IM "%APP_NAME%.exe" >nul 2>&1
ping -n 2 127.0.0.1 >nul

echo.
echo === Installing build dependencies ===
python -m pip install -r requirements.txt || goto :error

echo.
echo === Ensuring Playwright Chromium is installed (for Sign in) ===
python -m playwright install chromium || goto :error

echo.
echo === Building onedir app with PyInstaller ===
python -m PyInstaller --noconfirm --clean ^
  --onedir --windowed ^
  --name "%APP_NAME%" ^
  --paths "." ^
  --paths "sale_watcher" ^
  --collect-all customtkinter ^
  --collect-all tkinterdnd2 ^
  --collect-all playwright ^
  --hidden-import brand ^
  --hidden-import pandas ^
  --hidden-import openpyxl ^
  --hidden-import requests ^
  --hidden-import sale_watcher.app ^
  --hidden-import sale_watcher.watcher ^
  --hidden-import sale_watcher.auth ^
  --hidden-import sale_watcher.estate_ez ^
  --hidden-import sale_watcher.engagement_sync ^
  --hidden-import sale_watcher.ctbids_client ^
  --hidden-import sale_watcher.seller_client ^
  --hidden-import sale_watcher.export_report ^
  --hidden-import report_engine ^
  --hidden-import build_active_sale_report ^
  --hidden-import build_end_of_sale_report ^
  --hidden-import build_sku_rollup_report ^
  --hidden-import extract_sku_prefixes ^
  --hidden-import runtime_paths ^
  suite_app.py || goto :error

if not exist "%DIST_DIR%\%APP_NAME%.exe" (
  echo Build finished but exe not found at "%DIST_DIR%\%APP_NAME%.exe"
  goto :error
)

echo.
echo === Copying franchise-ready config + instructions ===
REM Generic template for other franchise owners (not local Coins Ale config)
if exist "config.franchise.json" (
  copy /Y config.franchise.json "%DIST_DIR%\config.json" >nul
) else (
  copy /Y config.json "%DIST_DIR%\config.json" >nul
)
copy /Y "HOW TO INSTALL.txt" "%DIST_DIR%\HOW TO INSTALL.txt" >nul
copy /Y "HOW TO USE SUITE.txt" "%DIST_DIR%\HOW TO USE.txt" >nul

echo.
echo === Bundling Chromium for Buyer / Seller Sign in ===
set "PW_SRC=%LOCALAPPDATA%\ms-playwright"
if not exist "%PW_SRC%" (
  echo Playwright browser folder not found at:
  echo   %PW_SRC%
  echo Sign-in may not work until Chromium is present.
) else (
  echo Copying "%PW_SRC%" ...
  if exist "%DIST_DIR%\ms-playwright" rmdir /S /Q "%DIST_DIR%\ms-playwright"
  xcopy /E /I /Y "%PW_SRC%" "%DIST_DIR%\ms-playwright" >nul || goto :error
)

echo.
echo === Creating zip for download ===
if exist "%ZIP_PATH%" del /F /Q "%ZIP_PATH%"
powershell -NoProfile -Command ^
  "Compress-Archive -Path '%DIST_DIR%\*' -DestinationPath '%ZIP_PATH%' -Force" || goto :error

echo.
echo ============================================================
echo  DONE — share either:
echo.
echo    Folder:  %CD%\%DIST_DIR%\
echo    Zip:     %CD%\%ZIP_PATH%
echo.
echo  Recipients: extract zip → double-click "%APP_NAME%.exe"
echo  See HOW TO INSTALL.txt inside the package.
echo ============================================================
pause
exit /b 0

:error
echo.
echo Build failed. Fix the error above, then run build_suite.bat again.
pause
exit /b 1
