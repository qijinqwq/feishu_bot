@echo off
cd /d "%~dp0"

echo ============================================
echo   Feishu Agent - Setup
echo ============================================
echo.

:: --- Find Python ---
set PYTHON=
:: Try the newly installed 3.12 first
if exist "C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe" (
    set PYTHON=C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe
    echo [INFO] Using Python: %PYTHON%
    goto :python_found
)
:: Fallback to generic python
python --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    echo [INFO] Using system Python
    goto :python_found
)
echo   [ERROR] Python not found.
echo   Expected: C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe
echo   Please install Python 3.12 from https://www.python.org/downloads/
pause
exit /b 1

:python_found
%PYTHON% --version
echo.

echo [1/2] Installing dependencies (lark-oapi, APScheduler)...
%PYTHON% -m pip install lark-oapi APScheduler
if errorlevel 1 (
    echo   [ERROR] Install failed. Check your network connection.
    pause
    exit /b 1
)
echo.

echo [2/2] Verifying...
%PYTHON% -c "import lark_oapi; print('  lark-oapi: OK'); import apscheduler; print('  apscheduler: OK')"
if errorlevel 1 (
    echo   [ERROR] Verification failed.
    pause
    exit /b 1
)
echo.

echo ============================================
echo   Setup complete!
echo.
echo   Next:
echo   1. Check config.py - APP_ID & APP_SECRET are filled in
echo   2. Double-click run.bat to start
echo   3. Chat with your bot on Feishu mobile app
echo ============================================
echo.
pause
