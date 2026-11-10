@echo off
REM ============================================================
REM  TunerLab Launcher
REM  Auto cd to this script's folder, then choose app / ui.
REM  No Chinese path is typed here on purpose: %~dp0 is
REM  resolved by the OS, so codepage/quote issues are avoided.
REM ============================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Cannot change directory to: %~dp0
    pause
    exit /b 1
)

REM Make sure python is available on PATH
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 'python' not found on PATH. Install Python 3.11+ and add to PATH.
    pause
    exit /b 1
)

:menu
cls
echo ============================================
echo            TunerLab Launcher
echo ============================================
echo  Current dir : %CD%
echo  Python      : 
for /f "tokens=*" %%v in ('python -c "import sys;print(sys.version.split()[0])" 2^>nul') do echo               %%v
echo ============================================
echo   1  Motion Console      (tuner.app  - real Zmotion hardware)
echo   2  Calibration Studio  (tuner.ui   - Mock data)
echo   3  Web Console         (tuner.web  - browser, zero dependency)
echo   0  Exit
echo ============================================
set "choice="
set /p choice="Select [1/2/3/0]: "

if "%choice%"=="1" goto :app
if "%choice%"=="2" goto :ui
if "%choice%"=="3" goto :web
if "%choice%"=="0" exit /b 0
echo Invalid choice, please enter 1, 2 or 0.
timeout /t 1 >nul
goto :menu

:app
echo.
echo Launching Motion Console (tuner.app)...
python -m tuner.app
if errorlevel 1 (
    echo.
    echo [ERROR] tuner.app exited with an error (see above).
    pause
)
goto :end

:ui
echo.
echo Launching Calibration Studio (tuner.ui)...
python -m tuner.ui
if errorlevel 1 (
    echo.
    echo [ERROR] tuner.ui exited with an error (see above).
    pause
)
goto :end

:web
echo.
echo Launching Web Console (tuner.web) on http://127.0.0.1:8000 ...
python -m tuner.web
if errorlevel 1 (
    echo.
    echo [ERROR] tuner.web exited with an error (see above).
    pause
)
goto :end

:end
endlocal
