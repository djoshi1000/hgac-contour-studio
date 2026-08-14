@echo off
setlocal
rem Silent launcher: starts the VBS helper and immediately closes this command window.
start "" /b wscript.exe "%~dp0launch_hgac_contour_gui.vbs"
exit /b 0
