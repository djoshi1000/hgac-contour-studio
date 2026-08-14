Option Explicit
Dim shell, fso, here, geoaiPy, arcPy, systemPy, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

geoaiPy = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\anaconda3\envs\geoai\pythonw.exe"
arcPy = "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\pythonw.exe"
systemPy = "pythonw.exe"

If fso.FileExists(geoaiPy) Then
    cmd = Chr(34) & geoaiPy & Chr(34) & " " & Chr(34) & here & "\hgac_usgs_contour_gui.py" & Chr(34)
ElseIf fso.FileExists(arcPy) Then
    cmd = Chr(34) & arcPy & Chr(34) & " " & Chr(34) & here & "\hgac_usgs_contour_gui.py" & Chr(34)
Else
    cmd = systemPy & " " & Chr(34) & here & "\hgac_usgs_contour_gui.py" & Chr(34)
End If

' Window style 0 = hidden. The GUI itself remains visible because pythonw creates the Tk window.
shell.Run cmd, 0, False
