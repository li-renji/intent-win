' INTENT Portable - Desktop shortcut helper (ASCII only, no Chinese in source)
' Args: <hostDir> <once|probe|remove> [desktopOverride]
'   once  : always create/refresh the shortcut (called by PIN-TO-DESKTOP.cmd)
'   probe : only act when the INTENT U disk is present (called by scheduled task)
'   remove: delete the shortcut
' No registry, no admin, no resident process.
Option Explicit
Dim sh, fso, args, hostDir, mode, deskOverride, desk, lnkPath, lnk, comspec
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments
If args.Count < 2 Then WScript.Quit 1
hostDir = args(0)
mode = LCase(args(1))
deskOverride = ""
If args.Count > 2 Then deskOverride = args(2)
If Len(deskOverride) > 0 Then
    desk = deskOverride
Else
    desk = sh.SpecialFolders("Desktop")
End If
If Len(desk) = 0 Then WScript.Quit 1
lnkPath = fso.BuildPath(desk, "INTENT Portable.lnk")

If mode = "remove" Then
    If fso.FileExists(lnkPath) Then fso.DeleteFile lnkPath, True
    WScript.Quit 0
End If
If mode = "probe" Then
    If Not UsbPresent() Then WScript.Quit 0
    If fso.FileExists(lnkPath) Then WScript.Quit 0
End If
If Not fso.FileExists(fso.BuildPath(hostDir, "launch.cmd")) Then WScript.Quit 1
If Not fso.FolderExists(desk) Then fso.CreateFolder desk

comspec = sh.ExpandEnvironmentStrings("%COMSPEC%")
Set lnk = sh.CreateShortcut(lnkPath)
lnk.TargetPath = comspec
lnk.Arguments = "/c """ & fso.BuildPath(hostDir, "launch.cmd") & """"
lnk.WorkingDirectory = hostDir
lnk.IconLocation = fso.BuildPath(hostDir, "intent.ico") & ",0"
lnk.Description = "INTENT Portable Agent (USB)"
lnk.Save
WScript.Quit 0

Function UsbPresent()
    Dim c
    For c = Asc("C") To Asc("Z")
        If fso.FileExists(Chr(c) & ":\INTENT-PORTABLE.marker") Then
            UsbPresent = True
            Exit Function
        End If
    Next
    UsbPresent = False
End Function
