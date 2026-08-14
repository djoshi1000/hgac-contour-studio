from pathlib import Path
import tempfile
from hgac_usgs_contour_gui import normalize_tile, parse_tile_list, parse_index_for_tile, find_local_dem

assert normalize_tile("15RUN316243.las") == "15RUN316243"
assert normalize_tile("USGS_OPR_TX_Houston_B24_15RUN316243.tif") == "15RUN316243"
assert parse_tile_list("15RUN316243.las, 15RUN316244.laz\n15RUN316243") == ["15RUN316243", "15RUN316244"]
text = "\n".join([
    "https://example/x/LAS/15RUN316243.las",
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/OPR/Projects/TX_Houston_B24/TX_Houston_3_B24/TIFF/USGS_OPR_TX_Houston_B24_15RUN316243.tif",
])
m = parse_index_for_tile(text, "15RUN316243")
assert len(m) == 1 and m[0].endswith("15RUN316243.tif")

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    good = root / "abc" / "bare_earth" / "be_rasters"
    bad = root / "abc" / "other" / "intensity_images"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "15RUN316243.tif").write_bytes(b"x")
    (bad / "15RUN316243.tif").write_bytes(b"x")
    chosen, alts = find_local_dem(str(root), "15RUN316243")
    assert chosen and "be_rasters" in chosen
print("All core tests passed")

# v1.1 regression: worker must populate and validate customer attributes before final copy.
worker_text = (Path(__file__).parent / "hgac_contour_arcgis_worker.py").read_text(encoding="utf-8")
for required_field in (
    "ELEV_FT", "INTERVAL_FT", "BASE_FT", "MAJOR", "CONTOUR_TYPE",
    "TILE_ID", "SOURCE_DEM", "SOURCE_URL", "CREATED_UTC",
):
    assert required_field in worker_text
assert "verify_delivery_fields" in worker_text
assert "CopyFeatures(tmp_fc, out_fc)" in worker_text
assert "attributes VERIFIED" in worker_text

gui_text = (Path(__file__).parent / "hgac_usgs_contour_gui.py").read_text(encoding="utf-8")
assert "attribute_validation_passed" in gui_text
assert "seamless 2ft + 5ft VERIFIED" in gui_text
assert "RUN_Contours_2FT" in gui_text
assert "RUN_Contours_5FT" in gui_text
print("v1.1 attribute-safety tests passed")

# v1.4 seamless batch grouping regression.
from hgac_contour_arcgis_batch_worker import connected_groups
records = [
    {"tile":"A","xmin":0,"ymin":0,"xmax":10,"ymax":10,"cell_x":1,"cell_y":1},
    {"tile":"B","xmin":10,"ymin":0,"xmax":20,"ymax":10,"cell_x":1,"cell_y":1},
    {"tile":"C","xmin":100,"ymin":100,"xmax":110,"ymax":110,"cell_x":1,"cell_y":1},
]
assert connected_groups(records) == [[0, 1], [2]]
batch_text = (Path(__file__).parent / "hgac_contour_arcgis_batch_worker.py").read_text(encoding="utf-8")
assert "MosaicToNewRaster" in batch_text
assert "RUN_Contours_{interval}FT" in batch_text
assert "clip_to_tile" in batch_text
print("v1.4 seamless batch tests passed")
