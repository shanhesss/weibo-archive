Option Explicit
Dim objWMIService, col, p, n
n = 0
Set objWMIService = GetObject("winmgmts:\\.\root\cimv2")
Set col = objWMIService.ExecQuery("SELECT ProcessId,CommandLine FROM Win32_Process WHERE Name='weibo_archive.exe'")
For Each p In col
  p.Terminate()
  n = n + 1
Next
If n > 0 Then
  MsgBox "已关闭 " & n & " 个服务进程。", vbInformation, "微博存档"
Else
  MsgBox "当前没有在运行的微博存档服务。", vbInformation, "微博存档"
End If
