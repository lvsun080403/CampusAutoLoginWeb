' CampusAutoLoginWeb - silent launcher
' Starts run-autologin-web.cmd with the "hidden" mode in a windowless console.
' Used by the Startup item and by the first-run window of run-autologin-web.cmd.
Option Explicit

Dim fso, sh, here, runner, cmdline
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
runner = fso.BuildPath(here, "run-autologin-web.cmd")

If Not fso.FileExists(runner) Then
  MsgBox "run-autologin-web.cmd not found next to this file.", 48, "CampusAutoLoginWeb"
  WScript.Quit 1
End If

sh.CurrentDirectory = here

' cmd.exe /c ""<runner>" hidden"
cmdline = "cmd.exe /c " & Chr(34) & Chr(34) & runner & Chr(34) & " hidden" & Chr(34)
sh.Run cmdline, 0, False
