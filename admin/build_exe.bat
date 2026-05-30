@echo off
:: ── Build BallchasingUploader.exe ────────────────────────────────────────────
:: Run this from the project root to produce a new exe.
:: After building, upload with: python admin/deploy_server.py --launcher-only
:: and manually scp the exe to the server.
::
:: Requirements: Python 3.12 + PyInstaller installed
::   pip install pyinstaller  (using py -3.12)
::
:: IMPORTANT: bump LAUNCHER_VERSION in launcher.py before building.
:: WARNING: never use ctypes.windll.* in launcher.py code that runs at startup
::          (including _create_shortcut). It triggers a libffi crash in frozen exes.
::          Use Path.home(), subprocess, or pure Python alternatives instead.
:: ─────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0.."

echo Cleaning previous build artifacts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "BallchasingUploader.spec" del /f "BallchasingUploader.spec"
echo.
echo Building BallchasingUploader.exe...
py -3.12 -m PyInstaller --onefile --noconsole ^
  --icon="src\logo.ico" ^
  --name="BallchasingUploader" ^
  --runtime-tmpdir="." ^
  --collect-submodules=tkinter ^
  --hidden-import="ctypes.wintypes" ^
  --hidden-import="webbrowser" ^
  --hidden-import="winreg" ^
  --hidden-import="hashlib" ^
  --hidden-import="calendar" ^
  --hidden-import="unicodedata" ^
  --hidden-import="queue" ^
  --hidden-import="shutil" ^
  --hidden-import="watchdog" ^
  --hidden-import="watchdog.observers" ^
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
