# HGAC USGS Contour GUI v1.1 — changes

## Attribute-table fix

Version 1.1 prevents a tile from being marked successful unless the final 2-ft and 5-ft feature classes contain populated delivery attributes.

Processing is now:

1. Generate ArcGIS contour into a temporary feature class.
2. Add and populate delivery fields.
3. Validate the temporary attribute table.
4. Copy the populated feature class to the final `T_<TILE>_Contours_2FT` or `5FT` name.
5. Validate the final attribute table again.
6. Delete the temporary feature class.
7. Mark the tile complete only when both intervals pass.

## Final fields

- `ELEV_FT`
- `INTERVAL_FT`
- `BASE_FT`
- `MAJOR`
- `CONTOUR_TYPE`
- `TILE_ID`
- `SOURCE_DEM`
- `SOURCE_URL`
- `CREATED_UTC`

`CONTOUR_TYPE` is `INDEX` for every fifth contour and `INTERMEDIATE` otherwise.

## Reporting

The ArcGIS JSON report now contains:

- `attribute_validation_passed`
- output feature counts for 2-ft and 5-ft
- required/missing field lists
- validation null counts
- a sample populated attribute row

The batch manifest now contains:

- `attributes_verified`
- `2ft_count`
- `5ft_count`
- final 2-ft and 5-ft feature-class paths

## GUI

- Success status now reads `COMPLETE — 2ft + 5ft + attributes VERIFIED`.
- Added `Open last output` button.
