@echo off
REM ============================================================
REM  TunerLab per-user installer (no admin rights required)
REM
REM  Copies the application next to this script into
REM    %LOCALAPPDATA%\Programs\TunerLab
REM  creates Start Menu / optional Desktop shortcuts and registers
REM  an entry in "Apps & Features".
REM
REM  Optional environment variables:
REM    TUNERLAB_DIR           install directory (default as above)
REM    TUNERLAB_NOSHORTCUTS=1 skip shortcut creation (for testing)
REM
REM  Calibration data in Documents\TunerLab and logs in
REM  %LOCALAPPDATA%\TunerLab are NEVER touched by this script.
REM ============================================================
setlocal EnableExtensions

set "SRC=%~dp0"
if not exist "%SRC%TunerLab.exe" goto :no_payload

if "%TUNERLAB_DIR%"=="" set "TUNERLAB_DIR=%LOCALAPPDATA%\Programs\TunerLab"
set "TARGET=%TUNERLAB_DIR%"

echo Installing TunerLab to:
echo   %TARGET%
if not exist "%TARGET%" mkdir "%TARGET%" >nul 2>&1
if not exist "%TARGET%" goto :no_target

REM /MIR keeps the target identical to the payload (no stale DLLs after upgrade).
robocopy "%SRC%." "%TARGET%" /MIR /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :copy_failed

if exist "%SRC%uninstall.cmd" copy /y "%SRC%uninstall.cmd" "%TARGET%\uninstall.cmd" >nul

if /I "%TUNERLAB_TEST%"=="1" goto :done
if /I "%TUNERLAB_NOSHORTCUTS%"=="1" goto :registry

powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; $start=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'; $s=$w.CreateShortcut((Join-Path $start 'TunerLab.lnk')); $s.TargetPath='%TARGET%\TunerLab.exe'; $s.WorkingDirectory='%TARGET%'; $s.IconLocation='%TARGET%\TunerLab.exe,0'; $s.Description='TunerLab tuner control and calibration'; $s.Save(); $d=Join-Path ([Environment]::GetFolderPath('Desktop')) 'TunerLab.lnk'; $s2=$w.CreateShortcut($d); $s2.TargetPath='%TARGET%\TunerLab.exe'; $s2.WorkingDirectory='%TARGET%'; $s2.IconLocation='%TARGET%\TunerLab.exe,0'; $s2.Save()" >nul 2>&1

:registry
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v DisplayName /t REG_SZ /d "TunerLab" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v DisplayVersion /t REG_SZ /d "0.2.0" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v Publisher /t REG_SZ /d "TunerLab" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v InstallLocation /t REG_SZ /d "%TARGET%" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v DisplayIcon /t REG_SZ /d "%TARGET%\TunerLab.exe,0" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v UninstallString /t REG_SZ /d "\"%TARGET%\uninstall.cmd\"" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v NoModify /t REG_DWORD /d 1 /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TunerLab" /v NoRepair /t REG_DWORD /d 1 /f >nul

echo.
echo Installed. Launching TunerLab...
start "" "%TARGET%\TunerLab.exe"
exit /b 0

:done
echo.
echo Test install complete (no shortcuts, no registry entry).
exit /b 0

:no_payload
echo [ERROR] TunerLab.exe was not found next to this script.
echo         Run install.cmd from the folder that contains TunerLab.exe.
pause
exit /b 1

:no_target
echo [ERROR] Cannot create the install directory: %TARGET%
pause
exit /b 1

:copy_failed
echo [ERROR] Copy failed (robocopy exit code %errorlevel%).
echo         Close any running TunerLab instance and try again.
pause
exit /b 1
