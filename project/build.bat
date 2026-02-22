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
if not exist "%PY%" (
  for /f "usebackq delims=" %%P in (`where python 2^>nul`) do if not defined _PY_FOUND (set "PY=%%P" & set "_PY_FOUND=1")
)

set "USER_SCRIPTS=%APPDATA%\Python\Python313\Scripts"
set "PYI=%USER_SCRIPTS%\pyinstaller.exe"
if not exist "%PYI%" (
  for /f "usebackq delims=" %%P in (`where pyinstaller 2^>nul`) do if not defined _PYI_FOUND (set "PYI=%%P" & set "_PYI_FOUND=1")
)

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
REM ENSURE onnxruntime + numpy (for Silero VAD speech detection)
REM ============================
call :log "Installing onnxruntime and numpy (for VAD)..."
"%PY%" -m pip install --user --upgrade onnxruntime numpy 1>>"%LOG%" 2>>&1
if errorlevel 1 (
  call :log "WARNING: onnxruntime/numpy install failed. VAD checkbox will be disabled."
)

REM Download silero_vad.onnx if not already present
if not exist "silero_vad.onnx" (
  call :log "Downloading silero_vad.onnx from GitHub..."
  "%PY%" -c "import urllib.request; urllib.request.urlretrieve('https://github.com/snakers4/silero-vad/raw/v4.0/files/silero_vad.onnx', 'silero_vad.onnx'); print('Downloaded silero_vad.onnx')" 1>>"%LOG%" 2>>&1
  if errorlevel 1 (
    call :log "WARNING: Failed to download silero_vad.onnx. VAD will be disabled in the exe."
  )
) else (
  call :log "silero_vad.onnx already present."
)

REM Detect onnxruntime + model availability and build PyInstaller VAD args
del /q "_vad_arg.tmp" >nul 2>&1
if exist "silero_vad.onnx" (
  "%PY%" -c "import onnxruntime; open('_vad_arg.tmp','w').write('--collect-all onnxruntime --hidden-import=numpy')" >nul 2>&1
)
set "VAD_PYI_ARGS="
if exist "_vad_arg.tmp" (
  set /p VAD_PYI_ARGS=<"_vad_arg.tmp"
  del /q "_vad_arg.tmp" >nul 2>&1
  call :log "VAD available -- bundling onnxruntime and silero model"
) else (
  call :log "VAD not available -- onnxruntime or model missing, checkbox will be disabled"
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
call :log "VAD_PYI_ARGS: !VAD_PYI_ARGS!"

set "SILERO_DATA="
if exist "silero_vad.onnx" set "SILERO_DATA=--add-data silero_vad.onnx;."

"%PYI%" --noconfirm --clean --onefile %WINFLAG% --hidden-import=vlc !VAD_PYI_ARGS! !SILERO_DATA! --name "%NAME%" --add-data "%CUTTER_ABS%;." "%GUI%" 1>>"%LOG%" 2>>&1
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
