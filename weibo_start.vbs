Option Explicit
Dim fso, sh
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)

Const URL = "http://127.0.0.1:8766/"

' 找微博工具的服务进程（命令行里带 weibo_server.py 的 python/pythonw）
Function IsServerProcess(cmdline)
  IsServerProcess = False
  If Not IsNull(cmdline) Then
    If InStr(cmdline, "weibo_server.py") > 0 Then IsServerProcess = True
  End If
End Function

' 启动服务
Sub StartServer()
  Dim objWMIService, col, p, pid
  pid = 0
  Set objWMIService = GetObject("winmgmts:\\.\root\cimv2")
  Set col = objWMIService.ExecQuery("SELECT ProcessId,CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
  For Each p In col
    If IsServerProcess(p.CommandLine) Then pid = p.ProcessId
  Next
  If pid > 0 Then
    MsgBox "工具已在运行（PID " & pid & "）。" & vbCrLf & vbCrLf & "浏览器打开 " & URL, vbInformation, "微博存档"
    Exit Sub
  End If
  Dim pyw
  pyw = "pythonw.exe"
  If fso.FileExists("C:\Python314\pythonw.exe") Then pyw = "C:\Python314\pythonw.exe"
  sh.Run """" & pyw & """ weibo_server.py", 0, False
  MsgBox "服务已启动。" & vbCrLf & vbCrLf & "浏览器打开 " & URL, vbInformation, "微博存档"
End Sub

' 关闭服务
Sub StopServer()
  Dim objWMIService, col, p, n
  n = 0
  Set objWMIService = GetObject("winmgmts:\\.\root\cimv2")
  Set col = objWMIService.ExecQuery("SELECT ProcessId,CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
  For Each p In col
    If IsServerProcess(p.CommandLine) Then
      p.Terminate()
      n = n + 1
    End If
  Next
  If n > 0 Then
    MsgBox "已关闭服务。", vbInformation, "微博存档"
  Else
    MsgBox "当前没有在运行的服务。", vbInformation, "微博存档"
  End If
End Sub

' 重启服务
Sub RestartServer()
  Dim objWMIService, col, p
  Set objWMIService = GetObject("winmgmts:\\.\root\cimv2")
  Set col = objWMIService.ExecQuery("SELECT ProcessId,CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
  For Each p In col
    If IsServerProcess(p.CommandLine) Then p.Terminate()
  Next
  WScript.Sleep 1500
  StartServer
End Sub

' 弹菜单
Dim choice
choice = InputBox("微博存档工具" & vbCrLf & vbCrLf & "输入 1：启动服务" & vbCrLf & "输入 2：关闭服务" & vbCrLf & "输入 3：重启服务", "微博存档", "1")
If choice = "" Then WScript.Quit
If choice = "1" Then
  StartServer
ElseIf choice = "2" Then
  StopServer
ElseIf choice = "3" Then
  RestartServer
Else
  MsgBox "无效输入，请输入 1、2 或 3。", vbExclamation, "微博存档"
End If
