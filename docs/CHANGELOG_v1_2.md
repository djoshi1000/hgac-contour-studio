# HGAC USGS Contour GUI v1.2

## Fixes for zero-feature contour tiles

- Reads/calculates DEM minimum and maximum before contouring and logs the native and feet elevation ranges.
- Computes the 2-ft and 5-ft contour levels that should occur inside each DEM's elevation range.
- Uses **Spatial Analyst Contour first** when Spatial Analyst is available, matching the engine used successfully by the 2018-vs-2024 comparison script.
- Automatically retries with **3D Analyst Contour** if the first engine produces zero features unexpectedly or fails.
- A genuinely flat tile with no 2-ft or 5-ft level inside its elevation range is now a **valid empty contour output**, not an attribute-validation failure.
- Empty but valid outputs still receive the complete delivery schema.
- If the DEM range indicates contours should exist but both engines produce zero features, the worker stops with a detailed diagnostic instead of reporting an attribute error.

## USGS download fixes

- Removed the arbitrary 1 MB minimum TIFF size rule.
- Downloads are validated using the TIFF file signature; a small but valid compressed GeoTIFF is accepted.
- If a USGS TIFF passes download validation but later fails in ArcGIS, `USGS first; local fallback` automatically retries the best local `bare_earth/be_rasters` match.

## Diagnostics added to per-tile JSON report

- DEM minimum/maximum in native units and feet
- DEM relief in feet
- Expected 2-ft/5-ft interior contour levels
- Contour engine used
- All engine attempts and feature counts
- Whether a zero-feature output was expected and valid
