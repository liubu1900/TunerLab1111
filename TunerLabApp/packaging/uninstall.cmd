@echo off
REM ============================================================
REM  TunerLab uninstaller (per-user)
REM
REM  Removes the program folder, the Start Menu / Desktop shortcuts
REM  and the "Apps & Features" entry.
REM
REM  Deliberately KEEPS:
REM    Documents\TunerLab\calibrations   measurement data
REM    %LOCALAPPDATA%\TunerLab           settings and logs
REM  Delete those by hand if you really want a clean slate.
REM ============================================================
setlocal EnableExtensions

set "TARGET=%~dp0"
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"

echo Removing TunerLab from:
echo   %TARGET%
echo.
echo Calibration data in Documents\TunerLab and logs in
echo %%LOCALAPPDATA%%\TunerLab will be kept.

reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /f >nul 2>&1

if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\TunerLab.lnk" del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\TunerLab.lnk" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=Join-Path ([Environment]::GetFolderPath('Desktop')) 'TunerLab.lnk'; if (Test-Path $d) { Remove-Item $d -Force }" >nul 2>&1

REM A running exe cannot be deleted from its own folder, so hand the removal to
REM a detached shell that waits for this process to exit.
start "" /min cmd /c "timeout /t 2 /nobreak >nul & rmdir /s /q \"%TARGET%\""
echo Uninstalled.
exit /b 0
