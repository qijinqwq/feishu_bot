@echo off
cd /d "%~dp0"

echo ============================================
echo   Feishu Personal Agent
echo ============================================
echo   Press Ctrl+C to stop
echo   Log file: agent.log
echo ============================================
echo.

:: Find Python (prefer the installed 3.12)
set PYTHON=python
if exist "C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe" (
    set PYTHON=C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe
)

%PYTHON% agent.py
pause
