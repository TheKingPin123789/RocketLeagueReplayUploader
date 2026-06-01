@echo off
title Ballchasing Uploader — Uninstall

echo.
echo  Ballchasing Uploader - Uninstaller
echo  ====================================
echo.
set /p CONFIRM= Are you sure you want to uninstall? (Y/N):
if /i not "%CONFIRM%"=="Y" (
    echo  Cancelled.
    pause
    exit /b
)

echo.
echo  Stopping app...
taskkill /f /im BallchasingUploader.exe >nul 2>&1

echo  Removing startup entry...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "BallchasingUploader" /f >nul 2>&1

echo  Removing desktop shortcut...
del "%USERPROFILE%\Desktop\Ballchasing Uploader.lnk" >nul 2>&1

echo  Deleting app files...
set "APP_DIR=%~dp0"

:: Move out of the folder so we can delete it
cd /d "%TEMP%"

rd /s /q "%APP_DIR%"

echo.
echo  Ballchasing Uploader has been removed.
echo.
pause
