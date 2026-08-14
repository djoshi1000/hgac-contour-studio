#!/usr/bin/env python3
"""ArcGIS Pro seamless multi-tile contour worker for HGAC Contour Studio.

The worker groups adjacent DEM tiles, mosaics each connected group before
contouring, generates one seamless run-wide 2-ft and 5-ft feature class, and
clips those seamless contours back to the original tile footprints so the
per-tile layers meet exactly at shared edges.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

import hgac_contour_arcgis_worker as core

BUILD = "2026-08-14-hgac-contour-arcgis-batch-worker-v1.4"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input-json", required=True)
    p.add_argument("--output-gdb", required=True)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--dem-z-units", choices=("meters", "feet", "us_survey_feet"), default="meters")
    p.add_argument("--base-2ft", type=float, default=0.0)
    p.add_argument("--base-5ft", type=float, default=0.0)
    p.add_argument("--report", default="")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--keep-work-mosaics", action="store_true")
    return p.parse_args()


def write_report(path: str, data: dict) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def extent_record(arcpy, item: dict) -> dict:
    dem = item["dem"]
    if not arcpy.Exists(dem):
        raise FileNotFoundError(f"ArcGIS cannot read DEM: {dem}")
    d = arcpy.Describe(dem)
    sr = d.spatialReference
    if not sr or getattr(sr, "name", "") in ("", "Unknown", "Unknown Coordinate System"):
        raise RuntimeError(f"DEM has unknown coordinate system: {dem}")
    r = arcpy.Raster(dem)
    e = d.extent
    min_native, max_native = core.raster_minmax(arcpy, dem)
    return {
        **item,
        "xmin": float(e.XMin), "ymin": float(e.YMin),
        "xmax": float(e.XMax), "ymax": float(e.YMax),
        "cell_x": abs(float(r.meanCellWidth)),
        "cell_y": abs(float(r.meanCellHeight)),
        "sr_name": sr.name,
        "wkid": getattr(sr, "factoryCode", 0),
        "sr": sr,
        "min_native": min_native,
        "max_native": max_native,
    }


def are_adjacent(a: dict, b: dict, multiplier: float = 1.6) -> bool:
    """Return True when raster extents overlap/touch within ~1.6 cells."""
    dx = max(0.0, max(a["xmin"], b["xmin"]) - min(a["xmax"], b["xmax"]))
    dy = max(0.0, max(a["ymin"], b["ymin"]) - min(a["ymax"], b["ymax"]))
    tol_x = multiplier * max(a["cell_x"], b["cell_x"])
    tol_y = multiplier * max(a["cell_y"], b["cell_y"])
    return dx <= tol_x and dy <= tol_y


def connected_groups(records: list[dict]) -> list[list[int]]:
    n = len(records)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if are_adjacent(records[i], records[j]):
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: min(g))


def assert_compatible(records: list[dict]) -> None:
    first = records[0]
    problems = []
    for rec in records[1:]:
        same_sr = False
        if first.get("wkid") and rec.get("wkid"):
            same_sr = first["wkid"] == rec["wkid"]
        else:
            same_sr = first["sr_name"] == rec["sr_name"]
        if not same_sr:
            problems.append(f"{rec['tile']}: CRS {rec['sr_name']} differs from {first['sr_name']}")
        if abs(rec["cell_x"] - first["cell_x"]) > max(first["cell_x"], rec["cell_x"]) * 1e-6:
            problems.append(f"{rec['tile']}: cell width {rec['cell_x']} differs from {first['cell_x']}")
        if abs(rec["cell_y"] - first["cell_y"]) > max(first["cell_y"], rec["cell_y"]) * 1e-6:
            problems.append(f"{rec['tile']}: cell height {rec['cell_y']} differs from {first['cell_y']}")
    if problems:
        raise RuntimeError("Selected DEMs are not grid-compatible for seamless mosaicking:\n" + "\n".join(problems))


def make_group_surface(arcpy, records: list[dict], group_indices: list[int], group_no: int, work_dir: Path) -> tuple[str, bool]:
    members = [records[i] for i in group_indices]
    if len(members) == 1:
        return members[0]["dem"], False

    name = f"seamless_group_{group_no:03d}.tif"
    out = work_dir / name
    if arcpy.Exists(str(out)):
        arcpy.management.Delete(str(out))
    first = members[0]
    arcpy.env.snapRaster = first["dem"]
    arcpy.env.cellSize = first["dem"]
    arcpy.env.outputCoordinateSystem = first["sr"]
    print(
        f"Mosaicking adjacent group {group_no}: {len(members)} DEM tile(s) -> {out}",
        flush=True,
    )
    # Use 32-bit float and FIRST so source elevations are preserved rather than
    # smoothed/averaged. The purpose of the mosaic is continuity at tile seams.
    arcpy.management.MosaicToNewRaster(
        input_rasters=";".join(m["dem"] for m in members),
        output_location=str(work_dir),
        raster_dataset_name_with_extension=name,
        coordinate_system_for_the_raster=first["sr"],
        pixel_type="32_BIT_FLOAT",
        cellsize=first["cell_x"],
        number_of_bands=1,
        mosaic_method="FIRST",
        mosaic_colormap_mode="FIRST",
    )
    try:
        arcpy.management.CalculateStatistics(str(out), 1, 1, [], "OVERWRITE")
    except Exception:
        pass
    return str(out), True


def add_group_fields(arcpy, fc: str, group_id: str, tiles: list[str]) -> None:
    existing = {f.name.upper() for f in arcpy.ListFields(fc)}
    if "GROUP_ID" not in existing:
        arcpy.management.AddField(fc, "GROUP_ID", "TEXT", field_length=24, field_alias="Seamless mosaic group")
    if "SOURCE_TILES" not in existing:
        arcpy.management.AddField(fc, "SOURCE_TILES", "TEXT", field_length=1024, field_alias="Source tile IDs")
    text = ";".join(tiles)[:1024]
    with arcpy.da.UpdateCursor(fc, ["GROUP_ID", "SOURCE_TILES"]) as cur:
        for row in cur:
            row[0] = group_id
            row[1] = text
            cur.updateRow(row)


def set_batch_tile_id(arcpy, fc: str) -> None:
    with arcpy.da.UpdateCursor(fc, ["TILE_ID"]) as cur:
        for row in cur:
            row[0] = "BATCH"
            cur.updateRow(row)


def create_footprints(arcpy, records: list[dict], gdb: str) -> str:
    out_name = core.valid_fc_name(arcpy, gdb, "RUN_Tile_Footprints")
    out_fc = os.path.join(gdb, out_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)
    arcpy.management.CreateFeatureclass(gdb, out_name, "POLYGON", spatial_reference=records[0]["sr"])
    arcpy.management.AddField(out_fc, "TILE_ID", "TEXT", field_length=32)
    arcpy.management.AddField(out_fc, "SOURCE_DEM", "TEXT", field_length=255)
    arcpy.management.AddField(out_fc, "SOURCE_URL", "TEXT", field_length=1024)
    with arcpy.da.InsertCursor(out_fc, ["SHAPE@", "TILE_ID", "SOURCE_DEM", "SOURCE_URL"]) as cur:
        for rec in records:
            e = arcpy.Extent(rec["xmin"], rec["ymin"], rec["xmax"], rec["ymax"])
            arr = arcpy.Array([
                arcpy.Point(e.XMin, e.YMin), arcpy.Point(e.XMin, e.YMax),
                arcpy.Point(e.XMax, e.YMax), arcpy.Point(e.XMax, e.YMin),
                arcpy.Point(e.XMin, e.YMin),
            ])
            poly = arcpy.Polygon(arr, records[0]["sr"])
            cur.insertRow([poly, rec["tile"][:32], rec["dem"][:255], rec.get("source_url", "")[:1024]])
    try:
        arcpy.management.AddSpatialIndex(out_fc)
    except Exception:
        pass
    return out_fc


def clip_to_tile(arcpy, run_fc: str, footprints_fc: str, rec: dict, out_fc: str) -> None:
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)
    lyr = f"fp_{os.getpid()}_{rec['tile']}"
    delim = arcpy.AddFieldDelimiters(footprints_fc, "TILE_ID")
    where = f"{delim} = '{rec['tile'].replace(chr(39), chr(39)*2)}'"
    arcpy.management.MakeFeatureLayer(footprints_fc, lyr, where)
    try:
        try:
            arcpy.analysis.PairwiseClip(run_fc, lyr, out_fc)
        except Exception:
            arcpy.analysis.Clip(run_fc, lyr, out_fc)
    finally:
        arcpy.management.Delete(lyr)


def specialize_tile_fields(arcpy, fc: str, rec: dict) -> int:
    count = 0
    with arcpy.da.UpdateCursor(fc, ["TILE_ID", "SOURCE_DEM", "SOURCE_URL"]) as cur:
        for row in cur:
            row[0] = rec["tile"][:32]
            row[1] = rec["dem"][:255]
            row[2] = rec.get("source_url", "")[:1024]
            cur.updateRow(row)
            count += 1
    return count


def merge_group_outputs(arcpy, group_fcs: list[str], out_fc: str) -> None:
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)
    if len(group_fcs) == 1:
        arcpy.management.CopyFeatures(group_fcs[0], out_fc)
    else:
        arcpy.management.Merge(group_fcs, out_fc)
    try:
        arcpy.management.RepairGeometry(out_fc, "DELETE_NULL")
        arcpy.management.AddSpatialIndex(out_fc)
    except Exception:
        pass


def main() -> int:
    args = parse_args()
    inputs = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    if isinstance(inputs, dict):
        inputs = inputs.get("tiles", inputs.get("inputs", []))
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("Input JSON must contain a non-empty list of tile/DEM records.")

    report = {
        "build": BUILD,
        "status": "started",
        "input_count": len(inputs),
        "seamless_mode": True,
        "combined_outputs": {},
        "tiles": {},
        "adjacency_groups": [],
        "invalid_inputs": [],
    }

    try:
        import arcpy

        info = arcpy.GetInstallInfo()
        report["arcgis_product"] = info.get("ProductName")
        report["arcgis_version"] = info.get("Version")
        licenses = core.checkout_contour_licenses(arcpy)
        report["checked_out"] = licenses
        core.ensure_gdb(arcpy, args.output_gdb)
        work_dir = Path(args.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        arcpy.env.overwriteOutput = bool(args.overwrite)
        arcpy.env.parallelProcessingFactor = "75%"

        records = []
        for item in inputs:
            try:
                rec = extent_record(arcpy, item)
                records.append(rec)
            except Exception as exc:
                report["invalid_inputs"].append({"tile": item.get("tile"), "dem": item.get("dem"), "error": f"{type(exc).__name__}: {exc}"})
        if report["invalid_inputs"]:
            raise RuntimeError("One or more DEM inputs cannot be read by ArcGIS. See invalid_inputs in the report.")

        assert_compatible(records)
        zfactor = core.z_to_feet_factor(args.dem_z_units)
        for rec in records:
            rec["min_ft"] = rec["min_native"] * zfactor if rec["min_native"] is not None else None
            rec["max_ft"] = rec["max_native"] * zfactor if rec["max_native"] is not None else None

        groups = connected_groups(records)
        report["adjacency_groups"] = [[records[i]["tile"] for i in g] for g in groups]
        print(f"ArcGIS Pro {report['arcgis_version']}; seamless batch mode; {len(records)} tile(s); {len(groups)} adjacency group(s)", flush=True)
        for gi, group in enumerate(groups, 1):
            print(f"  Group {gi}: " + ", ".join(records[i]["tile"] for i in group), flush=True)

        footprints_fc = create_footprints(arcpy, records, args.output_gdb)
        report["tile_footprints"] = footprints_fc

        group_outputs = {2: [], 5: []}
        group_mosaics = []
        for gi, group in enumerate(groups, 1):
            members = [records[i] for i in group]
            surface, is_mosaic = make_group_surface(arcpy, records, group, gi, work_dir)
            if is_mosaic:
                group_mosaics.append(surface)
            group_min_native, group_max_native = core.raster_minmax(arcpy, surface)
            group_min_ft = group_min_native * zfactor if group_min_native is not None else None
            group_max_ft = group_max_native * zfactor if group_max_native is not None else None
            group_id = f"GROUP_{gi:03d}"

            for interval in (2.0, 5.0):
                base_ft = args.base_2ft if interval == 2.0 else args.base_5ft
                expected = core.interior_contour_levels(group_min_ft, group_max_ft, interval, base_ft)
                tmp_name = core.valid_fc_name(arcpy, args.output_gdb, f"TMP_{group_id}_{int(interval)}FT")
                tmp_fc = os.path.join(args.output_gdb, tmp_name)
                if arcpy.Exists(tmp_fc):
                    arcpy.management.Delete(tmp_fc)
                print(
                    f"{group_id}: creating seamless {int(interval)}-ft contours from "
                    f"{len(members)} tile(s); {len(expected)} interior level(s) expected.",
                    flush=True,
                )
                count, engine, attempts = core.create_contours(
                    arcpy, surface, tmp_fc, interval, base_ft, zfactor, licenses, expected
                )
                allow_empty = count == 0 and not expected
                core.add_delivery_fields(
                    arcpy, tmp_fc, group_id, surface, "", interval, base_ft, allow_empty=allow_empty
                )
                add_group_fields(arcpy, tmp_fc, group_id, [m["tile"] for m in members])
                group_outputs[int(interval)].append(tmp_fc)
                report.setdefault("groups", {}).setdefault(group_id, {})[f"{int(interval)}ft"] = {
                    "surface": surface,
                    "tiles": [m["tile"] for m in members],
                    "count": count,
                    "engine": engine,
                    "attempts": attempts,
                }

        # One authoritative combined layer per contour interval.
        for interval in (2, 5):
            base_ft = args.base_2ft if interval == 2 else args.base_5ft
            run_name = core.valid_fc_name(arcpy, args.output_gdb, f"RUN_Contours_{interval}FT")
            run_fc = os.path.join(args.output_gdb, run_name)
            merge_group_outputs(arcpy, group_outputs[interval], run_fc)
            set_batch_tile_id(arcpy, run_fc)
            run_count = int(arcpy.management.GetCount(run_fc)[0])
            core.verify_delivery_fields(
                arcpy, run_fc, "BATCH", float(interval), base_ft,
                expected_updated=None, allow_empty=(run_count == 0),
            )
            report["combined_outputs"][f"{interval}ft"] = {
                "feature_class": run_fc,
                "count": run_count,
                "attributes_verified": True,
            }
            print(f"Combined seamless {interval}-ft output: {run_fc} ({run_count:,} features)", flush=True)

        # Per-tile outputs are clipped FROM the seamless run-wide lines. Therefore
        # adjacent per-tile line endpoints are derived from exactly the same geometry.
        for rec in records:
            tile_result = {"dem": rec["dem"], "source": rec.get("source", ""), "source_url": rec.get("source_url", ""), "outputs": {}}
            for interval in (2, 5):
                base_ft = args.base_2ft if interval == 2 else args.base_5ft
                run_fc = report["combined_outputs"][f"{interval}ft"]["feature_class"]
                out_name = core.valid_fc_name(arcpy, args.output_gdb, f"T_{rec['tile']}_Contours_{interval}FT")
                out_fc = os.path.join(args.output_gdb, out_name)
                clip_to_tile(arcpy, run_fc, footprints_fc, rec, out_fc)
                updated = specialize_tile_fields(arcpy, out_fc, rec)
                expected = core.interior_contour_levels(rec["min_ft"], rec["max_ft"], float(interval), base_ft)
                count = int(arcpy.management.GetCount(out_fc)[0])
                allow_empty = count == 0 and not expected
                core.verify_delivery_fields(
                    arcpy, out_fc, rec["tile"], float(interval), base_ft,
                    expected_updated=updated, allow_empty=allow_empty,
                )
                try:
                    arcpy.management.RepairGeometry(out_fc, "DELETE_NULL")
                    arcpy.management.AddSpatialIndex(out_fc)
                except Exception:
                    pass
                tile_result["outputs"][f"{interval}ft"] = {
                    "feature_class": out_fc,
                    "count": count,
                    "attributes_verified": True,
                }
                print(f"  {rec['tile']} {interval}-ft clipped seamless output: {count:,} features", flush=True)
            tile_result["attribute_validation_passed"] = True
            report["tiles"][rec["tile"]] = tile_result

        # Temporary group feature classes are implementation details; combined and
        # per-tile outputs are retained.
        for fcs in group_outputs.values():
            for fc in fcs:
                try:
                    if arcpy.Exists(fc):
                        arcpy.management.Delete(fc)
                except Exception:
                    pass

        if not args.keep_work_mosaics:
            for mosaic in group_mosaics:
                try:
                    if arcpy.Exists(mosaic):
                        arcpy.management.Delete(mosaic)
                    elif Path(mosaic).exists():
                        Path(mosaic).unlink()
                except Exception as exc:
                    print(f"WARNING: could not remove working mosaic {mosaic}: {exc}", flush=True)

        report["status"] = "succeeded"
        report["attribute_validation_passed"] = True
        report["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_report(args.report, report)
        print("SEAMLESS BATCH COMPLETE — per-tile + combined 2-ft/5-ft contours verified", flush=True)
        return 0

    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
        write_report(args.report, report)
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
