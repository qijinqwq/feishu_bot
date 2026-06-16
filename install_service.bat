@echo off
cd /d "%~dp0"

echo ============================================
echo   Install as Windows Service (7x24)
echo ============================================
echo.
echo This script requires NSSM (Non-Sucking Service Manager)
echo Download from: https://nssm.cc/download
echo Put nssm.exe into D:\app\nssm\
echo.

set NSSM_EXE=D:\app\nssm\nssm.exe

if not exist "%NSSM_EXE%" (
    echo [INFO] NSSM not found at %NSSM_EXE%
    echo.
    choice /c yn /m "Open download page now?"
    if errorlevel 2 goto :skip_dl
    if errorlevel 1 start https://nssm.cc/download
    :skip_dl
    pause
    exit /b 1
)

echo [1/3] Removing old service if exists...
"%NSSM_EXE%" status FeishuAgent >nul 2>&1
if not errorlevel 1 (
    "%NSSM_EXE%" stop FeishuAgent >nul 2>&1
    timeout /t 2 >nul
    "%NSSM_EXE%" remove FeishuAgent confirm >nul 2>&1
    echo   Old service removed.
)

echo [2/3] Registering new service...

:: Find Python (prefer the installed 3.12)
set PYTHON=python
if exist "C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe" (
    set PYTHON=C:\Users\25284\AppData\Local\Programs\Python\Python312\python.exe
)
echo   Using Python: %PYTHON%

"%NSSM_EXE%" install FeishuAgent "%PYTHON%" "%~dp0agent.py"
if errorlevel 1 (
    echo   [ERROR] Service registration failed!
    pause
    exit /b 1
)

"%NSSM_EXE%" set FeishuAgent AppDirectory "%~dp0"
"%NSSM_EXE%" set FeishuAgent DisplayName "Feishu Personal Agent"
"%NSSM_EXE%" set FeishuAgent Description "7x24 Feishu bot - todo + Claude file ops"
"%NSSM_EXE%" set FeishuAgent Start SERVICE_AUTO_START
"%NSSM_EXE%" set FeishuAgent AppExit Default Restart
"%NSSM_EXE%" set FeishuAgent AppThrottle 30000

echo   Service registered!

echo [3/3] Starting service...
"%NSSM_EXE%" start FeishuAgent
if errorlevel 1 (
    echo   [WARN] Start failed. Check logs: D:\app\feishu-agent\agent.log
) else (
    echo   Service started!
)

echo.
echo ============================================
echo   Done! Service info:
echo   Name     : FeishuAgent
echo   Display  : Feishu Personal Agent
echo   Startup  : Auto (boot)
echo.
echo   Commands:
echo   sc query FeishuAgent
echo   nssm stop FeishuAgent
echo   nssm start FeishuAgent
echo   nssm remove FeishuAgent confirm
echo ============================================
pause
