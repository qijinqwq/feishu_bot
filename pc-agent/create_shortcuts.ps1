$WshShell = New-Object -ComObject WScript.Shell

# PC Agent — silent launch (VBS, no terminal)
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\PC Agent.lnk")
$Shortcut.TargetPath = "D:\app\feishu-agent\pc-agent\run_pc.vbs"
$Shortcut.WorkingDirectory = "D:\app\feishu-agent\pc-agent"
$Shortcut.IconLocation = "%SystemRoot%\System32\imageres.dll,68"
$Shortcut.Save()

# Stop PC Agent
$Shortcut2 = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Stop PC Agent.lnk")
$Shortcut2.TargetPath = "D:\app\feishu-agent\pc-agent\stop_pc.bat"
$Shortcut2.WorkingDirectory = "D:\app\feishu-agent\pc-agent"
$Shortcut2.IconLocation = "shell32.dll,27"
$Shortcut2.Save()

Write-Output "Shortcuts updated."
