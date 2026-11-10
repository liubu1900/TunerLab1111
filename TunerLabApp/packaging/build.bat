@echo off
REM ============================================================
REM  TunerLab desktop build
REM    1. icon
REM    2. PyInstaller onedir  -> dist\TunerLab\TunerLab.exe
REM    3. Inno Setup (if ISCC is on PATH) -> dist\installer
REM ============================================================
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found in %CD%
    echo         python -m venv .venv
    echo         .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo [1/3] generating icon ...
.venv\Scripts\python.exe packaging\make_icon.py || goto :fail

echo [2/3] running PyInstaller ...
.venv\Scripts\python.exe -m PyInstaller packaging\tunerlab.spec --noconfirm --clean || goto :fail
if not exist "dist\TunerLab\TunerLab.exe" (
    echo [ERROR] PyInstaller did not produce dist\TunerLab\TunerLab.exe
    goto :fail
)
echo       built: %CD%\dist\TunerLab\TunerLab.exe

echo [3/3] building installer ...
where iscc >nul 2>nul
if errorlevel 1 (
    echo       [skip] Inno Setup ISCC.exe not on PATH
    echo              install Inno Setup 6, then run:
    echo              "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
) else (
    iscc packaging\installer.iss || goto :fail
    echo       built: %CD%\dist\installer
)

echo.
echo Done.
exit /b 0

:fail
echo.
echo [FAILED] see the messages above.
pause
exit /b 1
