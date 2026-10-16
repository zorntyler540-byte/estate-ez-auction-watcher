@echo off
REM ============================================================
REM  Builds the "Sale Report Generator" into a single .exe.
REM  Run this once on a Windows PC that has Python installed.
REM  The finished app lands in the dist\ folder.
REM ============================================================

REM Close any running copy so PyInstaller can overwrite the .exe
taskkill /F /IM "Sale Report Generator.exe" >nul 2>&1
if exist "dist\Sale Report Generator.exe" (
  echo Making sure the old .exe is not locked...
  ping -n 2 127.0.0.1 >nul
)

echo Installing required packages...
python -m pip install -r requirements.txt || goto :error

echo.
echo Building the app (this can take a minute or two)...
python -m PyInstaller --noconfirm --onefile --windowed --name "Sale Report Generator" app.py || goto :error

echo.
echo Copying config.json next to the app so prefixes can be edited...
copy /Y config.json "dist\config.json" >nul

echo.
echo ============================================================
echo  Done!  Give your aunt the whole "dist" folder:
echo    dist\Sale Report Generator.exe
echo    dist\config.json   (edit this to change client names)
echo ============================================================
pause
exit /b 0

:error
echo.
echo Build failed. Make sure Python is installed and on your PATH.
pause
exit /b 1
