' PC Agent — 静默启动脚本
' 双击此文件，PC Agent 在后台运行（无终端窗口）
' 状态查看: http://localhost:9528

Set WshShell = CreateObject("WScript.Shell")

' 工作目录
WshShell.CurrentDirectory = "D:\app\feishu-agent\pc-agent"

' Python 3.12 pythonw.exe（无控制台窗口）
' 优先使用用户安装的 Python 3.12，找不到则使用系统 pythonw
pythonwPath = "C:\Users\25284\AppData\Local\Programs\Python\Python312\pythonw.exe"

Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

' 启动（0 = 隐藏窗口, False = 不等待）
WshShell.Run """" & pythonwPath & """ pc_agent.py", 0, False
