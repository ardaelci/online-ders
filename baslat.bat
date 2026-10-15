@echo off
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python kurulu degil. python.org adresinden kur, kurarken "Add to PATH" kutusunu isaretle.
  pause
  exit /b
)

echo Gerekli paketler kontrol ediliyor...
python -m pip install --quiet playwright
python -m playwright install chromium >nul 2>&1

echo.
echo Program basliyor...
echo.
python derse_katil.py

echo.
pause
