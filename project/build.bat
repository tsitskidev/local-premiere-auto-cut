@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================
REM CONFIG
REM ============================================================
REM Set to 1 = console build (shows traceback, good for debugging)
REM Set to 0 = windowed build (no console)
set BUILD_CONSOLE=0

set "GUI=silencecut_gui.py"
set "CUTTER=cut_silence_to_fcpxml.py"
set "NAME=SilenceCut"
set "PY=C:\Python313\python.exe"

REM User-site pyinstaller location (because pip installs --user)
set "USER_SCRIPTS=%APPDATA%\Python\Python313\Scripts"
set "PYI=%USER_SCRIPTS%\pyinstaller.exe"

REM ============================================================
REM ENV FIXES
REM ============================================================
REM Make sure user-site packages are visible
set PYTHONNOUSERSITE=

REM ============================================================
REM INFO
REM ============================================================
echo Using python: %PY%
echo Using pyinstaller: %PYI%
echo Working dir: %cd%
echo Console build: %BUILD_CONSOLE%
echo.

REM ============================================================
REM CHECK FILES
REM ============================================================
if not exist "%GUI%" ( echo ERROR: Missing %GUI% & pause & exit /b 1 )
if not exist "%CUTTER%" ( echo ERROR: Missing %CUTTER% & pause & exit /b 1 )

REM ============================================================
REM ENSURE PYINSTALLER
REM ============================================================
if not exist "%PYI%" (
  echo PyInstaller not found. Installing to user site...
  "%PY%" -m pip install --user --upgrade pip
  "%PY%" -m pip install --user --upgrade pyinstaller
)

if not exist "%PYI%" (
  echo ERROR: pyinstaller.exe still not found:
  echo   %PYI%
  pause
  exit /b 1
)

REM ============================================================
REM CLEAN
REM ============================================================
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%NAME%.spec" del /q "%NAME%.spec"
del /q build_log.txt >nul 2>&1

REM ============================================================
REM BUILD FLAGS
REM ============================================================
if "%BUILD_CONSOLE%"=="1" (
  set "WINFLAG=--console"
) else (
  set "WINFLAG=--windowed"
)

set "CUTTER_ABS=%cd%\%CUTTER%"

REM ============================================================
REM BUILD
REM ============================================================
echo.
echo Building...
echo "%PYI%" --noconfirm --clean --onefile %WINFLAG% --name "%NAME%" --add-data "%CUTTER_ABS%;." "%GUI%"
echo.

"%PYI%" --noconfirm --clean --onefile %WINFLAG% --name "%NAME%" --add-data "%CUTTER_ABS%;." "%GUI%" > build_log.txt 2>&1

if errorlevel 1 (
  echo.
  echo BUILD FAILED. Tail of log:
  powershell -NoProfile -Command "Get-Content -Tail 80 build_log.txt"
  echo Full log: %cd%\build_log.txt
  pause
  exit /b 1
)

REM ============================================================
REM DONE
REM ============================================================
echo.
echo SUCCESS!
echo Output: %cd%\dist\%NAME%.exe
echo.
if "%BUILD_CONSOLE%"=="0" (
  echo NOTE: This is a windowed build (no console).
  echo       For debugging, set BUILD_CONSOLE=1.
)
pause
endlocal
