@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM ============================
REM CONFIG
REM ============================
set BUILD_CONSOLE=0
set "GUI=silencecut_gui.py"
set "CUTTER=cut_silence_to_fcpxml.py"
set "NAME=SilenceCut"
set "PY=C:\Python313\python.exe"

set "USER_SCRIPTS=%APPDATA%\Python\Python313\Scripts"
set "PYI=%USER_SCRIPTS%\pyinstaller.exe"

REM Make sure user-site is allowed
set PYTHONNOUSERSITE=

REM ============================
REM LOG SETUP (ALWAYS)
REM ============================
set "LOG=%cd%\build_log.txt"
del /q "%LOG%" >nul 2>&1

call :log "=== BUILD START ==="
call :log "CWD: %cd%"
call :log "PY: %PY%"
call :log "PYI: %PYI%"
call :log "BUILD_CONSOLE: %BUILD_CONSOLE%"
call :log "APPDATA: %APPDATA%"

REM ============================
REM BASIC CHECKS
REM ============================
if not exist "%PY%" (
  call :log "ERROR: Python not found at %PY%"
  goto :fail
)

"%PY%" -V 1>>"%LOG%" 2>>&1
if errorlevel 1 (
  call :log "ERROR: Python failed to run."
  goto :fail
)

if not exist "%GUI%" (
  call :log "ERROR: Missing %GUI% in %cd%"
  goto :fail
)

if not exist "%CUTTER%" (
  call :log "ERROR: Missing %CUTTER% in %cd%"
  goto :fail
)

REM ============================
REM ENSURE PIP
REM ============================
call :log "Checking pip..."
"%PY%" -m pip --version 1>>"%LOG%" 2>>&1
if errorlevel 1 (
  call :log "ERROR: pip not available for this Python."
  goto :fail
)

REM ============================
REM ENSURE PYINSTALLER
REM ============================
if not exist "%PYI%" (
  call :log "PyInstaller not found, installing (user)..."
  "%PY%" -m pip install --user --upgrade pip 1>>"%LOG%" 2>>&1
  "%PY%" -m pip install --user --upgrade pyinstaller 1>>"%LOG%" 2>>&1
)

if not exist "%PYI%" (
  call :log "ERROR: Still no pyinstaller.exe at %PYI%"
  call :log "Try running: %PY% -m pip show pyinstaller"
  goto :fail
)

REM ============================
REM ENSURE python-vlc
REM ============================
call :log "Ensuring python-vlc is installed..."
"%PY%" -m pip install --user --upgrade python-vlc 1>>"%LOG%" 2>>&1
if errorlevel 1 (
  call :log "WARNING: python-vlc install returned errorlevel %ERRORLEVEL% (continuing)."
)

REM ============================
REM CLEAN
REM ============================
call :log "Cleaning build/dist..."
if exist build rmdir /s /q build 1>>"%LOG%" 2>>&1
if exist dist rmdir /s /q dist 1>>"%LOG%" 2>>&1
if exist "%NAME%.spec" del /q "%NAME%.spec" 1>>"%LOG%" 2>>&1

REM ============================
REM FLAGS
REM ============================
if "%BUILD_CONSOLE%"=="1" (
  set "WINFLAG=--console"
) else (
  set "WINFLAG=--windowed"
)

set "CUTTER_ABS=%cd%\%CUTTER%"
call :log "WINFLAG: %WINFLAG%"
call :log "CUTTER_ABS: %CUTTER_ABS%"

REM ============================
REM BUILD
REM ============================
call :log "Running PyInstaller..."
call :log "%PYI% --noconfirm --clean --onefile %WINFLAG% --hidden-import=vlc --name %NAME% --add-data %CUTTER_ABS%;. %GUI%"

"%PYI%" --noconfirm --clean --onefile %WINFLAG% --hidden-import=vlc --name "%NAME%" --add-data "%CUTTER_ABS%;." "%GUI%" 1>>"%LOG%" 2>>&1
if errorlevel 1 (
  call :log "ERROR: PyInstaller failed with errorlevel %ERRORLEVEL%."
  goto :fail
)

call :log "SUCCESS: dist\%NAME%.exe"
echo.
echo SUCCESS!
echo Output: %cd%\dist\%NAME%.exe
echo Log: %LOG%
echo.
pause
exit /b 0

REM ============================
REM FAIL HANDLER
REM ============================
:fail
echo.
echo BUILD FAILED.
echo Log: %LOG%
echo.
echo --- Last 80 lines of log ---
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command "if(Test-Path '%LOG%'){Get-Content -Tail 80 '%LOG%'}"`) do echo %%L
echo ---------------------------
echo.
pause
exit /b 1

REM ============================
REM LOG FUNCTION
REM ============================
:log
echo %~1
>>"%LOG%" echo %~1
exit /b 0
