#!/usr/bin/env python3
"""ArcGIS Pro worker for HGAC USGS DEM contour generation.

Creates raw 2-ft and 5-ft contours from one DEM tile. Intended to be called by
hgac_usgs_contour_gui.py using ArcGIS Pro's Python executable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

BUILD = "2026-08-14-hgac-contour-arcgis-worker-v1.2"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dem")
    p.add_argument("--tile")
    p.add_argument("--output-gdb")
    p.add_argument("--dem-z-units", choices=("meters", "feet", "us_survey_feet"), default="meters")
    p.add_argument("--base-2ft", type=float, default=0.0)
    p.add_argument("--base-5ft", type=float, default=0.0)
    p.add_argument("--source-url", default="")
    p.add_argument("--report", default="")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--check-only", action="store_true")
    return p.parse_args()


def z_to_feet_factor(unit: str) -> float:
    if unit == "meters":
        return 3.280839895013123
    if unit == "feet":
        return 1.0
    if unit == "us_survey_feet":
        # US survey foot -> international foot
        return (1200.0 / 3937.0) / 0.3048
    raise ValueError(unit)


def write_report(path: str, data: dict) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def checkout_contour_licenses(arcpy):
    """Check out every available contour-capable extension.

    Spatial Analyst is preferred for raster contouring because this is the same
    ArcPy engine used by the successful 2018-vs-2024 comparison workflow. 3D
    Analyst remains available as an automatic fallback.
    """
    checked = []
    for extension in ("Spatial", "3D"):
        if arcpy.CheckExtension(extension) == "Available":
            arcpy.CheckOutExtension(extension)
            checked.append(extension)
    if not checked:
        raise RuntimeError("Contour requires an available Spatial Analyst or 3D Analyst extension.")
    return checked


def _as_float(value):
    try:
        if value in (None, "", "#"):
            return None
        return float(value)
    except Exception:
        return None


def raster_minmax(arcpy, raster_path: str):
    """Return robust raster minimum/maximum, calculating statistics when needed."""
    def read_once():
        minimum = maximum = None
        try:
            r = arcpy.Raster(raster_path)
            minimum = _as_float(getattr(r, "minimum", None))
            maximum = _as_float(getattr(r, "maximum", None))
        except Exception:
            pass
        if minimum is None:
            try:
                minimum = _as_float(arcpy.management.GetRasterProperties(raster_path, "MINIMUM").getOutput(0))
            except Exception:
                pass
        if maximum is None:
            try:
                maximum = _as_float(arcpy.management.GetRasterProperties(raster_path, "MAXIMUM").getOutput(0))
            except Exception:
                pass
        return minimum, maximum

    minimum, maximum = read_once()
    if minimum is None or maximum is None:
        print("DEM statistics missing; calculating raster statistics...", flush=True)
        try:
            arcpy.management.CalculateStatistics(raster_path, 1, 1, [], "OVERWRITE")
        except Exception as exc:
            print(f"WARNING: CalculateStatistics failed: {exc}", flush=True)
        minimum, maximum = read_once()
    return minimum, maximum


def interior_contour_levels(min_ft, max_ft, interval, base_ft):
    """Return potential contour levels strictly inside the DEM z-range."""
    import math
    if min_ft is None or max_ft is None or not (max_ft > min_ft):
        return []
    eps = max(1e-9, interval * 1e-9)
    k0 = math.ceil(((min_ft + eps) - base_ft) / interval)
    k1 = math.floor(((max_ft - eps) - base_ft) / interval)
    if k1 < k0:
        return []
    count = k1 - k0 + 1
    if count > 100000:
        return []
    return [base_ft + k * interval for k in range(k0, k1 + 1)]


def create_contours(arcpy, in_dem, out_fc, interval, base_ft, zfactor, licenses, expected_levels):
    """Create contours with engine fallback and return diagnostics."""
    attempts = []
    engines = []
    if "Spatial" in licenses:
        engines.append("Spatial Analyst")
    if "3D" in licenses:
        engines.append("3D Analyst")

    for engine in engines:
        if arcpy.Exists(out_fc):
            arcpy.management.Delete(out_fc)
        try:
            if engine == "Spatial Analyst":
                from arcpy.sa import Contour as SAContour
                SAContour(in_dem, out_fc, interval, base_ft, zfactor, "CONTOUR", 1_000_000)
            else:
                arcpy.ddd.Contour(in_dem, out_fc, interval, base_ft, zfactor, "CONTOUR", 1_000_000)
            count = int(arcpy.management.GetCount(out_fc)[0])
            attempts.append({"engine": engine, "status": "succeeded", "feature_count": count})
            print(f"  {engine} produced {count:,} contour feature(s).", flush=True)
            if count > 0:
                return count, engine, attempts
            # A truly flat tile may legitimately contain no requested contour level.
            if not expected_levels:
                print(
                    f"  No {interval:g}-ft contour level falls inside this DEM's elevation range; "
                    "empty output is valid for this tile.",
                    flush=True,
                )
                return 0, engine, attempts
            print(
                f"  WARNING: {engine} returned zero features although "
                f"{len(expected_levels)} contour level(s) fall inside the DEM elevation range; retrying alternate engine.",
                flush=True,
            )
        except Exception as exc:
            attempts.append({"engine": engine, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            print(f"  WARNING: {engine} contour attempt failed: {type(exc).__name__}: {exc}", flush=True)

    if expected_levels:
        preview = ", ".join(f"{v:g}" for v in expected_levels[:10])
        if len(expected_levels) > 10:
            preview += ", ..."
        raise RuntimeError(
            f"Contour generation produced zero features even though the DEM range contains "
            f"{len(expected_levels)} requested level(s): {preview}. Attempts={attempts}"
        )
    # If stats were unavailable, zero features cannot be certified as expected.
    raise RuntimeError(f"Contour generation produced zero features and DEM range could not validate an empty result. Attempts={attempts}")


def ensure_gdb(arcpy, gdb_path: str) -> None:
    gdb = Path(gdb_path)
    if arcpy.Exists(str(gdb)):
        return
    gdb.parent.mkdir(parents=True, exist_ok=True)
    arcpy.management.CreateFileGDB(str(gdb.parent), gdb.name)


def valid_fc_name(arcpy, gdb: str, requested: str) -> str:
    # Prefix ensures the LAS-style tile name never begins the FGDB object name.
    if not requested[0].isalpha():
        requested = "T_" + requested
    return arcpy.ValidateTableName(requested, gdb)


def find_contour_field(arcpy, fc: str) -> str:
    fields = {f.name.lower(): f.name for f in arcpy.ListFields(fc)}
    for candidate in ("contour", "contour_", "elev", "elevation"):
        if candidate in fields:
            return fields[candidate]
    numeric = [
        f.name for f in arcpy.ListFields(fc)
        if f.type in ("Double", "Single", "Integer", "SmallInteger")
        and f.name.lower() not in {"objectid", "fid", "shape_length", "major", "interval_ft"}
    ]
    if len(numeric) == 1:
        return numeric[0]
    raise RuntimeError(f"Could not identify contour elevation field in {fc}")


def add_delivery_fields(
    arcpy,
    fc: str,
    tile: str,
    dem: str,
    source_url: str,
    interval: float,
    base_ft: float,
    allow_empty: bool = False,
) -> dict:
    """Add and populate customer-facing contour attributes.

    Returns a validation dictionary. The caller treats validation failure as a
    processing failure so the GUI never reports success for an empty attribute
    schema.
    """
    existing = {f.name.upper() for f in arcpy.ListFields(fc)}
    defs = [
        ("ELEV_FT", "DOUBLE", None, "Contour elevation (ft)"),
        ("INTERVAL_FT", "DOUBLE", None, "Contour interval (ft)"),
        ("BASE_FT", "DOUBLE", None, "Base contour (ft)"),
        ("MAJOR", "SHORT", None, "Index contour flag (1=yes)"),
        ("CONTOUR_TYPE", "TEXT", 16, "Contour type"),
        ("TILE_ID", "TEXT", 32, "Source tile ID"),
        ("SOURCE_DEM", "TEXT", 255, "Source DEM path"),
        ("SOURCE_URL", "TEXT", 1024, "Source DEM URL"),
        ("CREATED_UTC", "TEXT", 40, "Created UTC"),
    ]
    for name, typ, length, alias in defs:
        if name not in existing:
            kw = {"field_alias": alias}
            if length:
                kw["field_length"] = length
            arcpy.management.AddField(fc, name, typ, **kw)

    cf = find_contour_field(arcpy, fc)
    major_interval = interval * 5.0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    fields = [
        cf,
        "ELEV_FT",
        "INTERVAL_FT",
        "BASE_FT",
        "MAJOR",
        "CONTOUR_TYPE",
        "TILE_ID",
        "SOURCE_DEM",
        "SOURCE_URL",
        "CREATED_UTC",
    ]
    updated = 0
    with arcpy.da.UpdateCursor(fc, fields) as cur:
        for row in cur:
            elev = float(row[0])
            q = (elev - base_ft) / major_interval if major_interval else 0.0
            major = int(abs(q - round(q)) <= 1e-7)
            row[1] = elev
            row[2] = float(interval)
            row[3] = float(base_ft)
            row[4] = major
            row[5] = "INDEX" if major else "INTERMEDIATE"
            row[6] = tile[:32]
            row[7] = str(dem)[:255]
            row[8] = str(source_url)[:1024]
            row[9] = now[:40]
            cur.updateRow(row)
            updated += 1

    return verify_delivery_fields(arcpy, fc, tile, interval, base_ft, updated, allow_empty=allow_empty)


def verify_delivery_fields(
    arcpy,
    fc: str,
    tile: str,
    interval: float,
    base_ft: float,
    expected_updated: int | None = None,
    allow_empty: bool = False,
) -> dict:
    required = [
        "ELEV_FT",
        "INTERVAL_FT",
        "BASE_FT",
        "MAJOR",
        "CONTOUR_TYPE",
        "TILE_ID",
        "SOURCE_DEM",
        "SOURCE_URL",
        "CREATED_UTC",
    ]
    actual = {f.name.upper(): f.name for f in arcpy.ListFields(fc)}
    missing = [name for name in required if name not in actual]
    total = int(arcpy.management.GetCount(fc)[0])
    null_counts = {name: 0 for name in required}
    sample = None

    if not missing and total > 0:
        cursor_fields = [actual[name] for name in required]
        with arcpy.da.SearchCursor(fc, cursor_fields) as cur:
            for row_number, row in enumerate(cur):
                if sample is None:
                    sample = dict(zip(required, row))
                for name, value in zip(required, row):
                    # SOURCE_URL is intentionally allowed to be blank for local DEMs.
                    if name == "SOURCE_URL":
                        continue
                    if value is None or (isinstance(value, str) and not value.strip()):
                        null_counts[name] += 1
                if row_number >= 9999 and total > 10000:
                    # Full scans can be expensive for enormous contours. A 10k-row
                    # validation sample is sufficient after UpdateCursor completed.
                    break

    checked_rows = 0 if sample is None else min(total, 10000)
    bad_non_null = {k: v for k, v in null_counts.items() if k != "SOURCE_URL" and v > 0}
    sample_ok = bool(sample) or (allow_empty and total == 0)
    if sample:
        try:
            sample_ok = (
                abs(float(sample["INTERVAL_FT"]) - float(interval)) <= 1e-9
                and abs(float(sample["BASE_FT"]) - float(base_ft)) <= 1e-9
                and str(sample["TILE_ID"]) == str(tile)[:32]
                and sample["ELEV_FT"] is not None
            )
        except Exception:
            sample_ok = False

    passed = (
        not missing
        and (total > 0 or allow_empty)
        and sample_ok
        and not bad_non_null
        and (expected_updated is None or expected_updated == total)
    )
    result = {
        "passed": passed,
        "feature_count": total,
        "updated_count": expected_updated,
        "required_fields": required,
        "missing_fields": missing,
        "null_counts_in_validation_scan": null_counts,
        "validation_rows_checked": checked_rows,
        "sample": sample,
        "allow_empty": allow_empty,
    }
    if not passed:
        raise RuntimeError(
            "Contour attribute validation failed for " + fc + ": " + json.dumps(result, default=str)
        )
    return result


def main() -> int:
    args = parse_args()
    report = {
        "build": BUILD,
        "status": "started",
        "tile": args.tile,
        "dem": args.dem,
        "source_url": args.source_url,
        "outputs": {},
    }
    try:
        import arcpy

        info = arcpy.GetInstallInfo()
        report.update({
            "arcgis_product": info.get("ProductName"),
            "arcgis_version": info.get("Version"),
            "three_d": arcpy.CheckExtension("3D"),
            "spatial": arcpy.CheckExtension("Spatial"),
        })

        if args.check_only:
            licenses = checkout_contour_licenses(arcpy)
            report.update({"status": "succeeded", "checked_out": licenses})
            print(json.dumps(report, indent=2), flush=True)
            write_report(args.report, report)
            return 0

        if not args.dem or not args.tile or not args.output_gdb:
            raise ValueError("--dem, --tile, and --output-gdb are required unless --check-only is used.")
        if not arcpy.Exists(args.dem):
            raise FileNotFoundError(f"ArcGIS cannot read DEM: {args.dem}")

        licenses = checkout_contour_licenses(arcpy)
        report["checked_out"] = licenses
        ensure_gdb(arcpy, args.output_gdb)

        arcpy.env.overwriteOutput = bool(args.overwrite)
        arcpy.env.snapRaster = args.dem
        arcpy.env.cellSize = args.dem
        arcpy.env.parallelProcessingFactor = "75%"

        desc = arcpy.Describe(args.dem)
        sr = desc.spatialReference
        report["spatial_reference"] = getattr(sr, "name", None)
        report["wkid"] = getattr(sr, "factoryCode", None)
        try:
            r = arcpy.Raster(args.dem)
            report["cell_width"] = float(r.meanCellWidth)
            report["cell_height"] = float(r.meanCellHeight)
        except Exception:
            pass

        zfactor = z_to_feet_factor(args.dem_z_units)
        report["z_factor_to_feet"] = zfactor
        report["base_2ft"] = args.base_2ft
        report["base_5ft"] = args.base_5ft

        dem_min_native, dem_max_native = raster_minmax(arcpy, args.dem)
        dem_min_ft = dem_min_native * zfactor if dem_min_native is not None else None
        dem_max_ft = dem_max_native * zfactor if dem_max_native is not None else None
        report["dem_min_native"] = dem_min_native
        report["dem_max_native"] = dem_max_native
        report["dem_min_ft"] = dem_min_ft
        report["dem_max_ft"] = dem_max_ft
        report["dem_relief_ft"] = (dem_max_ft - dem_min_ft) if dem_min_ft is not None and dem_max_ft is not None else None

        print(f"ArcGIS Pro {report.get('arcgis_version')}; extension(s)={','.join(licenses)}", flush=True)
        print(f"DEM: {args.dem}", flush=True)
        print(f"Z units: {args.dem_z_units}; z-factor to feet={zfactor:.12g}", flush=True)
        if dem_min_ft is not None and dem_max_ft is not None:
            print(
                f"DEM elevation range: {dem_min_native:.3f} to {dem_max_native:.3f} {args.dem_z_units} "
                f"= {dem_min_ft:.3f} to {dem_max_ft:.3f} ft "
                f"(relief {dem_max_ft-dem_min_ft:.3f} ft)",
                flush=True,
            )
        else:
            print("WARNING: DEM minimum/maximum could not be determined.", flush=True)

        for interval in (2.0, 5.0):
            base_ft = args.base_2ft if interval == 2.0 else args.base_5ft
            name = valid_fc_name(arcpy, args.output_gdb, f"T_{args.tile}_Contours_{int(interval)}FT")
            out_fc = os.path.join(args.output_gdb, name)
            if arcpy.Exists(out_fc):
                if args.overwrite:
                    arcpy.management.Delete(out_fc)
                else:
                    raise FileExistsError(f"Output exists: {out_fc}")

            # Build into a temporary feature class, fully populate/validate attributes,
            # then copy to the final name. This guarantees the final output is born
            # with the complete schema instead of relying on an open ArcGIS table to
            # refresh after fields are added.
            tmp_name = valid_fc_name(
                arcpy,
                args.output_gdb,
                f"TMP_{args.tile}_{int(interval)}FT_{os.getpid()}",
            )
            tmp_fc = os.path.join(args.output_gdb, tmp_name)
            if arcpy.Exists(tmp_fc):
                arcpy.management.Delete(tmp_fc)

            expected_levels = interior_contour_levels(
                dem_min_ft, dem_max_ft, interval, base_ft
            )
            report["outputs"].setdefault(f"{int(interval)}ft", {})["expected_interior_levels"] = expected_levels[:500]
            report["outputs"][f"{int(interval)}ft"]["expected_interior_level_count"] = len(expected_levels)
            if expected_levels:
                preview = ", ".join(f"{v:g}" for v in expected_levels[:10])
                if len(expected_levels) > 10:
                    preview += ", ..."
                print(
                    f"Creating {int(interval)}-ft contours (base={base_ft:g} ft); "
                    f"{len(expected_levels)} interior level(s) expected: {preview}",
                    flush=True,
                )
            else:
                print(
                    f"Creating {int(interval)}-ft contours (base={base_ft:g} ft); "
                    "no requested contour level lies strictly inside the DEM range.",
                    flush=True,
                )

            generated_count, contour_engine, contour_attempts = create_contours(
                arcpy, args.dem, tmp_fc, interval, base_ft, zfactor, licenses, expected_levels
            )
            allow_empty = (generated_count == 0 and not expected_levels)
            tmp_validation = add_delivery_fields(
                arcpy,
                tmp_fc,
                args.tile,
                args.dem,
                args.source_url,
                interval,
                base_ft,
                allow_empty=allow_empty,
            )
            arcpy.management.RepairGeometry(tmp_fc, "DELETE_NULL")

            if arcpy.Exists(out_fc):
                arcpy.management.Delete(out_fc)
            arcpy.management.CopyFeatures(tmp_fc, out_fc)
            final_validation = verify_delivery_fields(
                arcpy, out_fc, args.tile, interval, base_ft, expected_updated=None, allow_empty=allow_empty
            )
            try:
                arcpy.management.AddSpatialIndex(out_fc)
            except Exception:
                pass
            count = int(arcpy.management.GetCount(out_fc)[0])
            report["outputs"][f"{int(interval)}ft"] = {
                "feature_class": out_fc,
                "count": count,
                "attributes_verified": True,
                "attribute_validation": final_validation,
                "temporary_validation": tmp_validation,
                "contour_engine": contour_engine,
                "contour_attempts": contour_attempts,
                "expected_interior_level_count": len(expected_levels),
                "expected_empty": allow_empty,
            }
            try:
                arcpy.management.Delete(tmp_fc)
            except Exception:
                pass
            print(
                f"Finished {int(interval)}-ft contours: {count:,} feature(s); "
                f"engine={contour_engine}; attributes VERIFIED "
                f"({len(final_validation['required_fields'])} delivery fields)",
                flush=True,
            )

        report["attribute_validation_passed"] = all(
            bool(v.get("attributes_verified")) for v in report["outputs"].values()
        ) and len(report["outputs"]) == 2
        if not report["attribute_validation_passed"]:
            raise RuntimeError("Final attribute verification did not pass for both contour intervals.")
        report["status"] = "succeeded"
        report["completed_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_report(args.report, report)
        print("WORKER COMPLETE", flush=True)
        return 0
    except Exception as exc:
        report.update({
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
        write_report(args.report, report)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
