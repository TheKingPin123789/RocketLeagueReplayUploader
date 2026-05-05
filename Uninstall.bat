@echo off
setlocal enabledelayedexpansion
title Ballchasing Uploader - Uninstall

if "%1"=="RUNNING" goto :main
cmd /k "%~f0" RUNNING
exit

:main
echo =============================================
echo   Ballchasing Auto Uploader - Uninstall
echo =============================================
echo.
echo This will remove:
echo   - Desktop shortcut
echo   - Taskbar pin
echo   - The entire application folder
echo.
set /p CONFIRM=Type YES to confirm:
if /i not "!CONFIRM!"=="YES" (
    echo.
    echo Cancelled.
    goto :done
)
echo.

:: ── Desktop shortcut ─────────────────────────────────────────────────────────
echo Removing desktop shortcut...
del /q "%USERPROFILE%\Desktop\Ballchasing Uploader.lnk" 2>nul
echo Done.
echo.

:: ── Taskbar pin ──────────────────────────────────────────────────────────────
echo Removing taskbar pin...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$tb = [IO.Path]::Combine([Environment]::GetFolderPath('ApplicationData')," ^
    "    'Microsoft','Internet Explorer','Quick Launch','User Pinned','TaskBar');" ^
    "Get-ChildItem $tb -Filter '*Ballchasing*' -ErrorAction SilentlyContinue | Remove-Item -Force"
echo Done.
echo.

:: ── Delete application folder via temp script ─────────────────────────────────
echo Removing application folder...
set "APP=%~dp0"
if "!APP:~-1!"=="\" set "APP=!APP:~0,-1!"
set "CLEANUP=%TEMP%\bc_cleanup_%RANDOM%.bat"
> "!CLEANUP!" (
    echo @echo off
    echo timeout /t 2 /nobreak ^>nul
    echo rd /s /q "!APP!"
    echo del /q "%%~f0"
)
start "" /min cmd /c "!CLEANUP!"
echo Scheduled — folder will be removed in a few seconds.
echo.

echo =============================================
echo   Uninstall complete.
echo =============================================

:done
echo.
echo Press any key to close...
pause >nul
exit
