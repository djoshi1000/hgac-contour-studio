@echo off
setlocal
set "HERE=%~dp0"
set "ARCGIS_PY=C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
set "GEOAI_PY=%LOCALAPPDATA%\anaconda3\envs\geoai\python.exe"

if exist "%GEOAI_PY%" (
  echo Launching GUI with geoai Python in visible troubleshooting mode...
  "%GEOAI_PY%" "%HERE%hgac_usgs_contour_gui.py"
  goto :eof
)
if exist "%ARCGIS_PY%" (
  echo Launching GUI with ArcGIS Pro Python in visible troubleshooting mode...
  "%ARCGIS_PY%" "%HERE%hgac_usgs_contour_gui.py"
  goto :eof
)
python "%HERE%hgac_usgs_contour_gui.py"
