@echo off
setlocal enabledelayedexpansion

rem --- Go into the ou_dan_tong folder next to this .bat ---
cd /d "%~dp0ou_dan_tong" 2>nul

rem --- Fallback: if the folder name was mangled, search for web_app.py ---
if not exist "web_app.py" (
  set "APPDIR="
  for /r "%~dp0" %%f in (web_app.py) do if not defined APPDIR set "APPDIR=%%~dpf"
  if defined APPDIR cd /d "!APPDIR!"
)

if not exist "web_app.py" (
  echo.
  echo [ERROR] Cannot find web_app.py.
  echo Please unzip the WHOLE package first, then run this file.
  echo.
  pause
  exit /b 1
)

echo.
echo   ============================================
echo     OU DAN TONG  /  Order Task Co-pilot
echo   ============================================
echo.
echo   Folder  : %CD%
echo   Starting local service ...
echo.
echo   Then open your browser and visit:
echo.
echo         http://127.0.0.1:8000
echo.
echo   KEEP THIS WINDOW OPEN while using it.
echo   Close this window to STOP the service.
echo   ============================================
echo.

set PY=python
where python >nul 2>&1
if errorlevel 1 (
  set PY=py
  where py >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python was not found on this computer.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
  )
)

%PY% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python version too old. Python 3.10 or newer is required.
  echo.
  pause
  exit /b 1
)

rem --- Open the browser 5 seconds later (service will be up by then) ---
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start http://127.0.0.1:8000"

%PY% web_app.py
echo.
echo Service stopped.
pause
