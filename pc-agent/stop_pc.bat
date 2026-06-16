@echo off
cd /d "%~dp0"

echo ============================================
echo   Stop PC Agent
echo ============================================
echo.

echo   正在通过仪表盘 API 请求关闭...
powershell -Command "try { Invoke-WebRequest -Uri 'http://localhost:9528/api/shutdown' -Method POST -TimeoutSec 3 | Out-Null; Write-Host '  ✅ 关闭请求已发送' } catch { Write-Host '  ⚠️ API 不可用，尝试强制关闭...'; exit 1 }"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   正在查找 pc_agent 进程...
    powershell -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -match 'pc_agent' } | Select-Object -First 1; if ($p) { Stop-Process -Id $p.ProcessId -Force; Write-Host ('  已终止 PID=' + $p.ProcessId) } else { Write-Host '  未找到 pc_agent 进程' }"
)

echo.
pause
