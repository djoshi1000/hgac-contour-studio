@echo off
setlocal
set "HERE=%~dp0"
set "ARCGIS_PY=C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
if not exist "%ARCGIS_PY%" (
  echo ERROR: ArcGIS Pro Python not found at:
  echo %ARCGIS_PY%
  pause
  exit /b 1
)
"%ARCGIS_PY%" "%HERE%hgac_contour_arcgis_worker.py" --check-only
pause
