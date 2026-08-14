# HGAC Contour Studio v1.3

## Interface redesign

- New navy/teal dashboard theme with modern card-based layout.
- Wider resizable workspace designed for 1080p and larger desktop displays.
- Header status indicator for Ready / Processing / Completed / Attention Needed states.
- Four summary cards for tile count, DEM strategy, contour outputs, and ArcGIS readiness.
- Configuration moved into three clean tabs: **Tiles**, **DEM source**, and **Contours & output**.
- Tile status table and live log moved into a larger monitoring workspace.
- Color-coded tile rows distinguish active, successful, and failed tiles.
- Dark console-style live log with color-coded warnings, errors, successes, and ArcGIS activity.
- Added **Copy log** and **Clear** actions.
- Added numeric percent beside the progress bar.

## Background process behavior

- ArcGIS worker command windows are hidden by default using Windows `CREATE_NO_WINDOW` and `STARTUPINFO` flags.
- ArcGIS environment checks are also hidden by default.
- Added **Hide ArcGIS worker command windows (recommended)** control.
- New `launch_hgac_contour_gui.vbs` runs the GUI with `pythonw.exe` and no persistent command prompt.
- `launch_hgac_contour_gui.bat` now immediately hands off to the silent VBS launcher and exits.
- Added `launch_hgac_contour_gui_visible.bat` for troubleshooting when a visible terminal is desired.

## Processing logic

The v1.2 contour engine is unchanged:

- USGS Houston 1-4 index search with local bare-earth fallback.
- Robust TIFF validation.
- DEM range diagnostics.
- Spatial Analyst contour generation with 3D Analyst fallback.
- Raw 2-ft and 5-ft contours in feet.
- Delivery attribute population and validation.
- Automatic local DEM retry after a failed USGS ArcGIS attempt.
