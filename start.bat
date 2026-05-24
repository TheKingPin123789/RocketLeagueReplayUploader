@echo off
setlocal enabledelayedexpansion
title Ballchasing Uploader

:: ── location check ────────────────────────────────────────────────────────────
echo "%~dp0" | findstr /i "Downloads" >nul
if not errorlevel 1 (
    echo.
    echo  [!] It looks like you are running this from your Downloads folder.
    echo.
    echo  Please do this first:
    echo    1. Move this folder somewhere permanent, e.g.  C:\BallchasingUploader
    echo    2. Run start.bat from there
    echo.
    pause
    exit /b
)

:: ── first time setup check ────────────────────────────────────────────────────
if exist "%~dp0src\lib" if exist "%~dp0src\rattletrap.exe" goto :launch

echo =============================================
echo   Ballchasing Uploader - First Time Setup
echo =============================================
echo.

:: ── Python check ──────────────────────────────────────────────────────────────
echo [1/3] Checking for Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b
)
echo Done.
echo.

:: ── Python dependencies ───────────────────────────────────────────────────────
echo [2/3] Installing dependencies...
pip install -r "%~dp0requirements.txt" --target "%~dp0src\lib" --upgrade -q
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b
)
echo Done.
echo.

:: ── rattletrap ────────────────────────────────────────────────────────────────
echo [3/3] Downloading replay parser...
if exist "%~dp0src\rattletrap.exe" if exist "%~dp0src\.rrrocket_ok" goto :shortcut
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
    "    Invoke-WebRequest $url -OutFile '%~dp0_rr.zip';" ^
    "    Expand-Archive '%~dp0_rr.zip' -DestinationPath '%~dp0_rr_tmp' -Force;" ^
    "    $e = Get-ChildItem '%~dp0_rr_tmp' -Recurse -Filter 'rrrocket.exe' | Select-Object -First 1;" ^
    "    if (-not $e) { throw 'not found' };" ^
    "    Copy-Item $e.FullName '%~dp0src\rattletrap.exe' -Force;" ^
    "    $ok = $true; break" ^
    "  } catch { Write-Host ('  failed: ' + $_.Exception.Message) }" ^
    "};" ^
    "if (Test-Path '%~dp0_rr.zip')  { Remove-Item '%~dp0_rr.zip'  -Force };" ^
    "if (Test-Path '%~dp0_rr_tmp')  { Remove-Item '%~dp0_rr_tmp'  -Recurse -Force };" ^
    "if (-not $ok) { exit 1 }"
if errorlevel 1 (
    echo [WARNING] Could not download replay parser.
) else (
    echo. > "%~dp0src\.rrrocket_ok"
    echo Done.
)
echo.

:: ── desktop shortcut ──────────────────────────────────────────────────────────
:shortcut
echo Creating desktop shortcut...
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Ballchasing Uploader.lnk'); $s.TargetPath='%~dp0BallchasingUploader.exe'; $s.WorkingDirectory='%~dp0'; $s.Description='Ballchasing Auto Uploader'; $s.Save()"
echo Done.
echo.

echo =============================================
echo   Setup complete! Launching...
echo =============================================
timeout /t 2 >nul

:: ── launch ────────────────────────────────────────────────────────────────────
:launch
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Ballchasing Uploader.lnk'); $s.TargetPath='%~dp0BallchasingUploader.exe'; $s.WorkingDirectory='%~dp0'; $s.Description='Ballchasing Auto Uploader'; $s.Save()" >nul 2>&1
start "" "%~dp0BallchasingUploader.exe"
exit
