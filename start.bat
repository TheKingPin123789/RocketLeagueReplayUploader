@echo off
setlocal enabledelayedexpansion
title Ballchasing Uploader

:: ── Determine install directory ───────────────────────────────────────────────
:: If running from Downloads or Desktop → auto-create folder in user profile
:: Otherwise → create a BallchasingUploader subfolder where start.bat is

set "SRC=%~dp0"

echo "%SRC%" | findstr /i "\\Downloads\\" >nul
if not errorlevel 1 goto :pick_default_dir
echo "%SRC%" | findstr /i "\\Desktop\\" >nul
if not errorlevel 1 goto :pick_default_dir

:: Normal location — install in a BallchasingUploader subfolder here
:: (skip subfolder if start.bat is already inside a BallchasingUploader folder)
echo "%SRC%" | findstr /i "\\BallchasingUploader\\" >nul
if not errorlevel 1 (
    set "INSTALL=%SRC%"
    goto :launch_or_setup
)
set "INSTALL=%SRC%BallchasingUploader\"
if not exist "%INSTALL%" mkdir "%INSTALL%"
goto :launch_or_setup

:pick_default_dir
set "INSTALL=%USERPROFILE%\BallchasingUploader\"
if not exist "%INSTALL%" (
    echo.
    echo  [i]  Creating install folder at:
    echo       %INSTALL%
    mkdir "%INSTALL%"
    echo.
)
:: Copy start.bat to the install folder so future launches work from there
if not exist "%INSTALL%start.bat" copy /y "%~f0" "%INSTALL%start.bat" >nul

:launch_or_setup
:: ── If already installed, run the exe directly ────────────────────────────────
if exist "%INSTALL%BallchasingUploader.exe" (
    start "" "%INSTALL%BallchasingUploader.exe"
    exit
)

:: ── First time setup ──────────────────────────────────────────────────────────
echo =============================================
echo   Ballchasing Uploader - First Time Setup
echo =============================================
echo.
echo  Install location: %INSTALL%
echo.

:: ── Python check ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Python not found.
    echo  Install from https://www.python.org/downloads/
    echo  Make sure "Add Python to PATH" is checked during install.
    echo.
    pause
    exit /b
)

:: ── Python dependencies ───────────────────────────────────────────────────────
if not exist "%INSTALL%src\lib\" (
    echo Installing dependencies...
    pip install requests customtkinter pillow watchdog -q --target "%INSTALL%src\lib"
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b
    )
    echo Done.
    echo.
)

:: ── Download uninstaller ───────────────────────────────────────────────────────
if not exist "%INSTALL%uninstall.bat" (
    powershell -NoProfile -Command ^
        "try { Invoke-WebRequest 'http://ballchasingautouploader.com:8766/uninstall' -OutFile '%INSTALL%uninstall.bat' -UseBasicParsing; Unblock-File '%INSTALL%uninstall.bat' } catch { Write-Host 'Could not download uninstaller.' }"
)

:: ── Download launcher from server ─────────────────────────────────────────────
if not exist "%INSTALL%launcher.py" (
    echo Downloading launcher...
    powershell -NoProfile -Command ^
        "try { Invoke-WebRequest 'http://ballchasingautouploader.com:8766/launcher' -OutFile '%INSTALL%launcher.py' -UseBasicParsing } catch { exit 1 }"
    if not exist "%INSTALL%launcher.py" (
        echo Failed to download launcher. Check your internet connection.
        pause
        exit /b
    )
    echo Done.
    echo.
)

:: ── First launch via Python (downloads exe + everything else) ─────────────────
echo Downloading app files...
for /f "delims=" %%P in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYTHONW=%%P

if exist "%PYTHONW%" (
    start "" "%PYTHONW%" "%INSTALL%launcher.py"
) else (
    start "" python "%INSTALL%launcher.py"
)
exit
