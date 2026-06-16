@echo off
cd /d "%~dp0"

echo ============================================
echo   PC Agent - Physical Machine Agent
echo   Cloud: ws://122.51.207.16:9527
echo ============================================
echo   Dashboard: http://localhost:9528
echo   Stop:      Double-click "Stop PC Agent" on Desktop
echo   Press Ctrl+C to stop this window
echo   Log file: pc_agent.log
echo ============================================
echo.

:: Find Python
set PYTHON=python
if exist "C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe" (
    set PYTHON=C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe
)

%PYTHON% pc_agent.py
pause
