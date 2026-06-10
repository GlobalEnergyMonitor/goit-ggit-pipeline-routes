#!/usr/bin/env python3
"""Validate GEM pipeline-route GeoJSON files.

Checks each file for:
  - valid JSON, top-level FeatureCollection (or a single top-level Feature)
  - filename matches P####.geojson or P####-compressor-station(s).geojson
  - CRS is WGS 84 / CRS84 (or no crs member at all, per RFC 7946)
  - no Z coordinates (positions must be [lon, lat] only)
  - longitude in [-180, 180], latitude in [-90, 90]
  - the same ProjectID file does not exist in more than one fuel folder

Warnings (reported but do not fail validation):
  - a ProjectID property that does not match the filename (this is common
    when a route was copied from a related project)
  - a route file (P####.geojson, not a -compressor-stations file) containing
    Point/MultiPoint/Polygon/MultiPolygon features -- embedded valves,
    markers, or traced station outlines. These are dropped when building the
    normalized branch; station points belong in a -compressor-stations file.

Empty routes (geometry: null) are valid -- see README.md.

Usage:
  validate_geojson.py FILE [FILE ...]    validate specific files
  validate_geojson.py --all              validate every route file in the repo

Exits nonzero if any file fails. Requires only the Python standard library.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "data" / "individual-routes"

FILENAME_RE = re.compile(r"^(P\d{4,6})(-compressor-stations?)?\.geojson$")

# Acceptable spellings of the (optional) legacy crs member. RFC 7946 GeoJSON
# is always WGS 84, so the only valid crs declarations are the equivalents.
ACCEPTED_CRS = {
    "urn:ogc:def:crs:OGC:1.3:CRS84",
    "urn:ogc:def:crs:OGC::CRS84",
    "urn:ogc:def:crs:EPSG::4326",
    "OGC:CRS84",
    "CRS84",
    "EPSG:4326",
}

GEOMETRY_TYPES = {
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection",
}

LINE_GEOMETRY_TYPES = {"LineString", "MultiLineString"}


def has_line_geometry(geom):
    """True if a geometry is (or, for a GeometryCollection, contains) a
    LineString or MultiLineString. Null geometries count as line-compatible
    (empty-route placeholder)."""
    if not isinstance(geom, dict):
        return geom is None
    gtype = geom.get("type")
    if gtype == "GeometryCollection":
        return any(has_line_geometry(g) for g in geom.get("geometries", []))
    return gtype in LINE_GEOMETRY_TYPES


def check_positions(coords, errors, path="coordinates"):
    """Recursively validate a coordinates array: every position must be
    [lon, lat] with no Z value and coordinates in valid ranges."""
    if not isinstance(coords, list):
        errors.append(f"{path}: expected an array, got {type(coords).__name__}")
        return
    if coords and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in coords):
        # This is a single position
        if len(coords) != 2:
            errors.append(
                f"{path}: position has {len(coords)} values {coords} -- "
                "must be exactly [longitude, latitude] (no Z coordinate)"
            )
            return
        lon, lat = coords
        if not -180 <= lon <= 180:
            errors.append(f"{path}: longitude {lon} outside [-180, 180]")
        if not -90 <= lat <= 90:
            errors.append(f"{path}: latitude {lat} outside [-90, 90]")
        return
    for i, item in enumerate(coords):
        check_positions(item, errors, f"{path}[{i}]")


def check_geometry(geom, errors, path="geometry"):
    if geom is None:
        return  # empty route -- valid by convention
    if not isinstance(geom, dict):
        errors.append(f"{path}: must be an object or null")
        return
    gtype = geom.get("type")
    if gtype not in GEOMETRY_TYPES:
        errors.append(f"{path}: unknown geometry type {gtype!r}")
        return
    if gtype == "GeometryCollection":
        for i, g in enumerate(geom.get("geometries", [])):
            check_geometry(g, errors, f"{path}.geometries[{i}]")
        return
    if "coordinates" not in geom:
        errors.append(f"{path}: missing 'coordinates'")
        return
    check_positions(geom["coordinates"], errors, f"{path}.coordinates")


def check_duplicate_id(filepath, errors):
    """Flag the same filename appearing in more than one fuel folder."""
    try:
        fuel_dirs = [d for d in ROUTES_DIR.iterdir() if d.is_dir()]
    except FileNotFoundError:
        return
    hits = [d.name for d in fuel_dirs if (d / filepath.name).exists()]
    if len(hits) > 1:
        errors.append(
            f"{filepath.name} exists in more than one fuel folder: {', '.join(sorted(hits))} "
            "-- ProjectIDs must live in exactly one"
        )


def validate_file(filepath):
    """Return (errors, warnings) lists of strings for one file."""
    errors = []
    warnings = []
    filepath = Path(filepath)

    m = FILENAME_RE.match(filepath.name)
    if not m:
        errors.append(
            f"filename {filepath.name!r} does not match the required pattern "
            "P####.geojson or P####-compressor-stations.geojson"
        )
        project_id = None
        is_route_file = False
    else:
        project_id = m.group(1)
        is_route_file = m.group(2) is None

    try:
        text = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        errors.append(f"cannot read file: {e}")
        return errors, warnings

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        errors.append(f"invalid JSON: {e}")
        return errors, warnings

    if isinstance(data, dict) and data.get("type") == "Feature":
        # A single top-level Feature is valid GeoJSON (common geojson.io
        # export); treat it as a one-feature collection.
        data = {"type": "FeatureCollection", "features": [data], "crs": data.get("crs")}

    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        errors.append("top level must be a FeatureCollection (or a single Feature)")
        return errors, warnings

    crs = data.get("crs")
    if crs is not None:
        crs_name = crs.get("properties", {}).get("name") if isinstance(crs, dict) else None
        if crs_name not in ACCEPTED_CRS:
            errors.append(
                f"crs is {crs_name!r} -- GeoJSON must be WGS 84 (EPSG:4326 / CRS84); "
                "reproject before committing"
            )

    features = data.get("features")
    if not isinstance(features, list):
        errors.append("'features' must be an array")
        return errors, warnings

    n_non_line = 0
    for i, feat in enumerate(features):
        fpath = f"features[{i}]"
        if not isinstance(feat, dict) or feat.get("type") != "Feature":
            errors.append(f"{fpath}: must be an object with type 'Feature'")
            continue
        check_geometry(feat.get("geometry"), errors, f"{fpath}.geometry")
        if is_route_file and not has_line_geometry(feat.get("geometry")):
            n_non_line += 1
        props = feat.get("properties")
        if isinstance(props, dict) and project_id is not None:
            pid = props.get("ProjectID")
            if pid is not None and str(pid).strip() != project_id:
                warnings.append(
                    f"{fpath}: ProjectID property {pid!r} does not match filename ({project_id})"
                )

    if n_non_line:
        warnings.append(
            f"{n_non_line} point/polygon feature(s) in a route file -- these are "
            "dropped from the normalized branch; station points belong in a "
            "-compressor-stations file"
        )

    if filepath.resolve().is_relative_to(ROUTES_DIR):
        check_duplicate_id(filepath, errors)

    return errors, warnings


def main(argv):
    if not argv or argv == ["--help"] or argv == ["-h"]:
        print(__doc__.strip())
        return 2

    if argv == ["--all"]:
        files = sorted(ROUTES_DIR.glob("*/*.geojson"))
        if not files:
            print(f"no .geojson files found under {ROUTES_DIR}", file=sys.stderr)
            return 2
    else:
        files = [Path(a) for a in argv]

    n_failed = 0
    n_warned = 0
    for f in files:
        errors, warnings = validate_file(f)
        if errors:
            n_failed += 1
            print(f"FAIL  {f}")
            shown = errors[:20]
            for err in shown:
                print(f"      - {err}")
            if len(errors) > len(shown):
                print(f"      ... and {len(errors) - len(shown)} more errors")
        if warnings:
            n_warned += 1
            print(f"WARN  {f}")
            for w in warnings:
                print(f"      - {w}")

    print(
        f"\nchecked {len(files)} file(s): {len(files) - n_failed} passed, "
        f"{n_failed} failed, {n_warned} with warnings"
    )
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
