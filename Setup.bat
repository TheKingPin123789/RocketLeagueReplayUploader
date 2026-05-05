@echo off
setlocal enabledelayedexpansion
title Ballchasing Uploader - Setup

:: Keep window open no matter what happens
if "%1"=="RUNNING" goto :main
cmd /k "%~f0" RUNNING
exit

:main
echo =============================================
echo   Ballchasing Auto Uploader - Setup
echo =============================================
echo.

:: ── Visual C++ Redistributable ───────────────────────────────────────────────
echo [1/5] Checking Visual C++ Redistributable...
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed >nul 2>&1
if not errorlevel 1 (
    echo Already installed.
    echo.
    goto :python
)
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed >nul 2>&1
if not errorlevel 1 (
    echo Already installed.
    echo.
    goto :python
)
echo Not found. Downloading Visual C++ Redistributable...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Invoke-WebRequest 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile '%~dp0_vcredist.exe'"
if errorlevel 1 (
    echo [WARNING] Could not download VC++ Redistributable. Skipping.
    echo.
    goto :python
)
echo Installing silently...
"%~dp0_vcredist.exe" /install /quiet /norestart
del /q "%~dp0_vcredist.exe"
echo Done.
echo.

:: ── Python ───────────────────────────────────────────────────────────────────
:python
echo [2/5] Checking for Python...
python --version
if errorlevel 1 goto :nopython
echo.
goto :pythondeps

:nopython
echo.
echo [ERROR] Python was not found.
echo Please install Python from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during install.
goto :done

:: ── Python dependencies ───────────────────────────────────────────────────────
:pythondeps
echo [3/5] Installing Python dependencies into src\lib...
echo.
pip install -r "%~dp0requirements.txt" --target "%~dp0src\lib" --upgrade
if errorlevel 1 goto :piperror
echo.
goto :shortcut

:piperror
echo.
echo [ERROR] Failed to install Python dependencies.
goto :done

:: ── Desktop shortcut ─────────────────────────────────────────────────────────
:shortcut
echo [4/5] Creating desktop shortcut...
for /f "delims=" %%P in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYTHONW=%%P"
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Ballchasing Uploader.lnk'); $s.TargetPath='%PYTHONW%'; $s.Arguments='\"%~dp0src\main.pyw\"'; $s.WorkingDirectory='%~dp0src'; $s.Description='Ballchasing Auto Uploader'; $s.Save()"
echo Done.
echo.

:: ── rattletrap ────────────────────────────────────────────────────────────────
:rattletrap
echo [5/5] Checking for rrrocket (replay parser)...
if exist "%~dp0src\rattletrap.exe" if exist "%~dp0src\.rrrocket_ok" (
    echo Replay parser already present.
    goto :alldone
)
echo Downloading rrrocket from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$urls = @();" ^
    "try {" ^
    "  $r = Invoke-RestMethod 'https://api.github.com/repos/nickbabcock/rrrocket/releases/latest';" ^
    "  $a = $r.assets | Where-Object { $_.name -like '*x86_64*windows*' } | Select-Object -First 1;" ^
    "  if ($a) { $urls += $a.browser_download_url }" ^
    "} catch {};" ^
    "$urls += 'https://github.com/nickbabcock/rrrocket/releases/download/v0.11.1/rrrocket-0.11.1-x86_64-pc-windows-msvc.zip';" ^
    "$ok = $false;" ^
    "foreach ($url in $urls) {" ^
    "  try {" ^
    "    Write-Host ('Trying ' + $url);" ^
    "    Invoke-WebRequest $url -OutFile '%~dp0_rr.zip';" ^
    "    Expand-Archive '%~dp0_rr.zip' -DestinationPath '%~dp0_rr_tmp' -Force;" ^
    "    $e = Get-ChildItem '%~dp0_rr_tmp' -Recurse -Filter 'rrrocket.exe' | Select-Object -First 1;" ^
    "    if (-not $e) { throw 'rrrocket.exe not found in archive' };" ^
    "    Copy-Item $e.FullName '%~dp0src\rattletrap.exe' -Force;" ^
    "    $ok = $true; break" ^
    "  } catch { Write-Host ('  failed: ' + $_.Exception.Message) }" ^
    "};" ^
    "if (Test-Path '%~dp0_rr.zip')  { Remove-Item '%~dp0_rr.zip'  -Force };" ^
    "if (Test-Path '%~dp0_rr_tmp')  { Remove-Item '%~dp0_rr_tmp'  -Recurse -Force };" ^
    "if (-not $ok) { exit 1 };" ^
    "Write-Host 'rrrocket installed successfully.'"
if errorlevel 1 goto :rattletrap_warn
echo. > "%~dp0src\.rrrocket_ok"
goto :alldone

:rattletrap_warn
echo.
echo [WARNING] Could not auto-download rrrocket.exe.
echo Download it manually from:
echo   https://github.com/nickbabcock/rrrocket/releases
echo Place rrrocket.exe as rattletrap.exe inside the src\ folder.

:alldone
echo.
echo =============================================
echo   Setup complete! Shortcut added to Desktop.
echo =============================================

:done
echo.
echo Press any key to close...
pause >nul
