' INTENT Portable - in-drive shortcuts (ASCII source only; names come from argv).
' Usage:
'   cscript //nologo shortcuts.vbs make <root> <name1> <script1> <name2> <script2>
'   cscript //nologo shortcuts.vbs read <full-path-to-lnk>
'
' Why the drive scan: a .lnk cannot store a working relative target. An absolute
' target breaks as soon as the USB letter changes (E: -> F:). So the link always
' points at %ComSpec% (present on every Windows install) and its argument scans
' C..Z for INTENT-PORTABLE.marker, then calls START.cmd / WINDOW.cmd on that
' drive. Drive-letter independent, no registry, no admin, no resident process.
Option Explicit
Dim sh, fso, args, mode, root, p, k

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments
If args.Count < 2 Then WScript.Quit 1
mode = LCase(args(0))

If mode = "read" Then
    p = args(1)
    If Not fso.FileExists(p) Then
        WScript.StdOut.WriteLine "MISSING=1"
        WScript.Quit 0
    End If
    Set k = sh.CreateShortcut(p)
    WScript.StdOut.WriteLine "TARGET=" & k.TargetPath
    WScript.StdOut.WriteLine "ARGS=" & k.Arguments
    WScript.StdOut.WriteLine "WORKDIR=" & k.WorkingDirectory
    WScript.StdOut.WriteLine "ICON=" & k.IconLocation
    WScript.Quit 0
End If

If mode <> "make" Then WScript.Quit 1
If args.Count < 6 Then WScript.Quit 1
root = fso.GetAbsolutePathName(args(1))
If Not fso.FolderExists(root) Then WScript.Quit 2

MakeOne root, args(2), args(3), 1
MakeOne root, args(4), args(5), 2
WScript.Quit 0

Sub MakeOne(root, linkName, scriptName, kind)
    Dim lnk, scan, ico
    scan = "/c for %i in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) " _
         & "do @if exist %i:\INTENT-PORTABLE.marker call %i:\" & scriptName
    If kind = 1 Then
        ico = "%SystemRoot%\System32\cmd.exe,0"
    Else
        ico = "%SystemRoot%\explorer.exe,0"
    End If
    Set lnk = sh.CreateShortcut(fso.BuildPath(root, linkName))
    ' 裸文件名由 Windows 按 PATH 解析；不写 C:\Windows，也不写盘符。
    ' WScript.Shell 会把 %ComSpec% 就地展开成绝对本机路径，故不能用它。
    lnk.TargetPath = "cmd.exe"
    lnk.Arguments = scan
    lnk.WorkingDirectory = ""
    lnk.IconLocation = ico
    If kind = 1 Then
        lnk.Description = "INTENT Portable - Terminal chat"
    Else
        lnk.Description = "INTENT Portable - Window chat"
    End If
    lnk.Save
End Sub
