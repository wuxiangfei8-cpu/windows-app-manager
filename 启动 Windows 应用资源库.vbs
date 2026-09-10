Option Explicit

Dim shell, fso, scriptPath, pythonw
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "windows_manager.pyw")
pythonw = "pythonw.exe"
shell.Run """" & pythonw & """ """" & scriptPath & """", 0, False
