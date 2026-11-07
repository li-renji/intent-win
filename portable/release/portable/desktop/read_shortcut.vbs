' Self-test helper: print shortcut properties as key=value lines (ASCII only).
Option Explicit
Dim sh, lnk, p
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count < 1 Then WScript.Quit 1
p = WScript.Arguments(0)
If Not CreateObject("Scripting.FileSystemObject").FileExists(p) Then
    WScript.StdOut.WriteLine "MISSING=1"
    WScript.Quit 0
End If
Set lnk = sh.CreateShortcut(p)
WScript.StdOut.WriteLine "TARGET=" & lnk.TargetPath
WScript.StdOut.WriteLine "ARGS=" & lnk.Arguments
WScript.StdOut.WriteLine "WORKDIR=" & lnk.WorkingDirectory
WScript.StdOut.WriteLine "ICON=" & lnk.IconLocation
