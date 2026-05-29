@echo off
:: ── Build BallchasingUploader.exe ────────────────────────────────────────────
:: Run this from the project root to produce a new exe.
:: After building, upload with: python admin/deploy_server.py --launcher-only
:: and manually scp the exe to the server.
::
:: Requirements: Python 3.14 + PyInstaller installed
::   pip install pyinstaller
::
:: IMPORTANT: bump LAUNCHER_VERSION in launcher.py before building.
:: ─────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0.."

echo Building BallchasingUploader.exe...
python -m PyInstaller --onefile --noconsole ^
  --icon="src\logo.ico" ^
  --name="BallchasingUploader" ^
  --collect-submodules=tkinter ^
  --hidden-import=watchdog ^
  --hidden-import=watchdog.observers ^
  --hidden-import=watchdog.events ^
  launcher.py

if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

copy /y "dist\BallchasingUploader.exe" "BallchasingUploader.exe"
echo.
echo Done. BallchasingUploader.exe is ready.
echo Next steps:
echo   1. Test the exe locally
echo   2. python admin/deploy_server.py --launcher-only
echo   3. scp BallchasingUploader.exe root@46.101.184.78:/opt/app-server/BallchasingUploader.exe
echo.
pause
