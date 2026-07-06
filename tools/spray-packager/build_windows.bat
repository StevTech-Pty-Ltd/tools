@echo off
REM Builds dist\SprayPackager.exe -- a single-file app with GDAL bundled.
REM Prerequisite: Python 3.10+ from python.org (tick "Add python.exe to PATH").
REM Run this from the spray-packager folder on a Windows machine.

setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher not found. Install Python from python.org first.
    pause
    exit /b 1
)

if not exist .venv (
    py -3 -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip || goto :fail
pip install -r requirements.txt pyinstaller || goto :fail

pyinstaller --noconfirm --clean --onefile --windowed ^
    --name SprayPackager ^
    --collect-all rasterio ^
    spray_packager.py || goto :fail

echo.
echo Build complete: dist\SprayPackager.exe
echo Give that single file to the operators -- nothing else to install.
pause
exit /b 0

:fail
echo.
echo Build FAILED -- see the messages above.
pause
exit /b 1
