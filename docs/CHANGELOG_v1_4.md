# HGAC Contour Studio v1.4 — Seamless multi-tile contours

## Main correction: tile-edge contour gaps

Previous versions contoured each DEM tile independently. Raster contour algorithms cannot evaluate cells beyond an individual raster boundary, so two adjacent independently generated contour datasets can stop short of the common edge or meet imperfectly.

v1.4 changes the production architecture:

1. Resolve/download every requested DEM before ArcGIS contouring begins.
2. Read each DEM extent, CRS and cell size.
3. Automatically identify adjacent/overlapping DEM groups.
4. Mosaic each adjacent group to a temporary **32-bit floating-point** working surface.
5. Generate raw 2-ft and 5-ft contours from the continuous group surface.
6. Merge all groups into two run-wide feature classes:
   - `RUN_Contours_2FT`
   - `RUN_Contours_5FT`
7. Clip those exact run-wide contour geometries back to each original tile footprint to create:
   - `T_<TILE>_Contours_2FT`
   - `T_<TILE>_Contours_5FT`

Because adjacent per-tile outputs come from the same continuous contour geometry, their endpoints meet at the shared tile boundary instead of being independently calculated.

## Run-wide deliverables

Every successful batch now creates both per-tile layers and combined layers. The recommended customer/run-wide deliverables are:

- `RUN_Contours_2FT`
- `RUN_Contours_5FT`

The two intervals remain separate intentionally. Merging 2-ft and 5-ft products into one feature class can duplicate contours at elevations common to both series.

## Efficient non-adjacent processing

The batch worker uses connected-component grouping on raster extents. DEMs that touch or overlap within approximately 1.6 source cells are treated as one group. Distant tiles are processed as separate groups rather than generating a single huge bounding-box mosaic filled mostly with NoData.

## Accuracy behavior

- Working mosaics use `32_BIT_FLOAT` to preserve DEM elevation precision.
- Mosaic operator is `FIRST`, avoiding averaging or smoothing elevations in overlaps.
- Contours remain raw/unsmoothed.
- No artificial interpolation is applied across genuine source-DEM NoData gaps.
- Original tile footprints are retained in `RUN_Tile_Footprints` for QA and traceability.

## Attribute behavior

All previous delivery fields remain populated and validated. Combined layers use `TILE_ID = BATCH` and preserve group provenance through `GROUP_ID` and `SOURCE_TILES`. Per-tile layers are re-stamped with the original tile ID, DEM path, and source URL.

## Working mosaics

A new GUI option allows retaining working group mosaics for QA. It is off by default to save disk space. The resolved source DEM list is always retained in `reports/resolved_dem_inputs.json` and the run manifests.
