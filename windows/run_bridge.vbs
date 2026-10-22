Option Explicit

Dim shell, fso, appDir, pythonw, bridge
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.BuildPath(shell.ExpandEnvironmentStrings("%LOCALAPPDATA%"), "FeishuCodexBridge")
pythonw = fso.BuildPath(appDir, ".venv\Scripts\pythonw.exe")
bridge = fso.BuildPath(appDir, "bridge.py")

If Not fso.FileExists(fso.BuildPath(appDir, "config.toml")) Then
  WScript.Quit 0
End If
If Not fso.FileExists(pythonw) Then
  WScript.Quit 1
End If

shell.CurrentDirectory = appDir
shell.Run """" & pythonw & """ """ & bridge & """ serve", 0, False

