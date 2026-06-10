#!/usr/bin/env python3
"""Build the normalized/standardized copy of every pipeline-route GeoJSON.

This produces the contents of the `normalized` branch. Originals on `main`
are never touched -- they keep their original metadata and full coordinate
precision for researchers who scrape them.

Normalization rules:
  - top level is always a FeatureCollection (a bare top-level Feature is
    wrapped)
  - the legacy `crs` member is removed (RFC 7946 GeoJSON is always WGS 84)
  - coordinates are rounded to 6 decimal places (~10 cm) and any Z values
    are dropped
  - every feature gets a ProjectID property matching the filename; if the
    original file carried a different ProjectID (e.g. the route was copied
    from a related project), that value is preserved as SourceProjectID
  - other feature properties and feature ids are preserved as-is, except
    properties whose value is an embedded JSON copy of a geometry (e.g. a
    GEOJSON field from a source-data export), which are dropped as redundant
  - files with no features become a single null-geometry feature, matching
    the empty-route convention in README.md
  - deterministic serialization: one feature per line, stable key order,
    so unchanged inputs always produce byte-identical outputs

Usage:
  normalize_geojson.py --out DIR [--precision 6]

Reads every data/individual-routes/*/*.geojson in this repo and writes the
same tree (plus a branch README) under DIR. Requires only the Python
standard library.
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "data" / "individual-routes"

FILENAME_RE = re.compile(r"^(P\d{4,6})(-compressor-stations?)?\.geojson$")

BRANCH_README = """\
# normalized pipeline routes (generated -- do not edit)

This branch is rebuilt automatically from `main` by the
`normalize-routes` GitHub Actions workflow whenever route files change.
Do not commit to it or open pull requests against it; any manual change
will be overwritten on the next rebuild.

Compared to the original files on `main`, every file here:

- is a canonical GeoJSON FeatureCollection (WGS 84, no legacy `crs` member)
- has coordinates rounded to 6 decimal places (~10 cm) with no Z values
- carries a `ProjectID` property on every feature, matching the filename
  (where the original file carried a different `ProjectID`, that value is
  preserved as `SourceProjectID`)

If you want the original files exactly as researchers submitted them --
original metadata, original coordinate precision -- use the `main` branch.
"""


GEOMETRY_TYPES = {
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection",
}


EMBEDDED_GEOMETRY_RE = re.compile(
    r'^\s*\{\s*"type"\s*:\s*"(%s)"\s*,\s*"(coordinates|geometries)"'
    % "|".join(GEOMETRY_TYPES)
)


def is_embedded_geometry(value):
    """True if a property value is a JSON string encoding a geometry -- a
    redundant copy of the feature's geometry left by some source-data
    exports. Matched on structure rather than parsed, because these strings
    are often truncated to 254 chars (shapefile DBF field limit) and no
    longer valid JSON."""
    return isinstance(value, str) and bool(EMBEDDED_GEOMETRY_RE.match(value))


def round_coordinates(coords, precision):
    """Recursively round a coordinates array and drop Z values."""
    if not isinstance(coords, list):
        return coords
    if coords and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in coords):
        # A single position: keep [lon, lat] only.
        return [round(float(v), precision) for v in coords[:2]]
    return [round_coordinates(item, precision) for item in coords]


def normalize_geometry(geom, precision):
    if geom is None:
        return None
    out = {"type": geom.get("type")}
    if geom.get("type") == "GeometryCollection":
        out["geometries"] = [
            normalize_geometry(g, precision) for g in geom.get("geometries", [])
        ]
    else:
        out["coordinates"] = round_coordinates(geom.get("coordinates"), precision)
    return out


def normalize_feature(feat, project_id, precision):
    props = feat.get("properties")
    props = dict(props) if isinstance(props, dict) else {}

    original_pid = props.pop("ProjectID", None)
    new_props = {}
    if project_id is not None:
        new_props["ProjectID"] = project_id
        if original_pid is not None and str(original_pid).strip() != project_id:
            new_props["SourceProjectID"] = str(original_pid).strip()
    elif original_pid is not None:
        new_props["ProjectID"] = original_pid
    new_props.update(
        (k, v) for k, v in props.items() if not is_embedded_geometry(v)
    )

    out = {"type": "Feature"}
    if "id" in feat:
        out["id"] = feat["id"]
    out["properties"] = new_props
    out["geometry"] = normalize_geometry(feat.get("geometry"), precision)
    return out


def normalize_file(src, precision):
    """Return the normalized file content (str) for one source file."""
    data = json.loads(src.read_text(encoding="utf-8"))

    if isinstance(data, dict) and data.get("type") == "Feature":
        features = [data]
    elif isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = [f for f in data.get("features", []) if isinstance(f, dict)]
    else:
        raise ValueError(f"{src}: not a Feature or FeatureCollection")

    m = FILENAME_RE.match(src.name)
    project_id = m.group(1) if m else None

    if not features:
        features = [{"type": "Feature", "properties": {}, "geometry": None}]
    normalized = [normalize_feature(f, project_id, precision) for f in features]

    feature_lines = ",\n".join(
        json.dumps(f, ensure_ascii=False, separators=(", ", ": ")) for f in normalized
    )
    return (
        '{\n"type": "FeatureCollection",\n"features": [\n'
        + feature_lines
        + "\n]\n}\n"
    )


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="output directory for the normalized tree")
    parser.add_argument("--precision", type=int, default=6, help="coordinate decimal places (default 6)")
    args = parser.parse_args(argv)

    out_root = Path(args.out)
    files = sorted(ROUTES_DIR.glob("*/*.geojson"))
    if not files:
        print(f"no .geojson files found under {ROUTES_DIR}", file=sys.stderr)
        return 2

    n_failed = 0
    bytes_in = 0
    bytes_out = 0
    for src in files:
        try:
            content = normalize_file(src, args.precision)
        except (ValueError, json.JSONDecodeError, OSError) as e:
            print(f"FAIL  {src}: {e}", file=sys.stderr)
            n_failed += 1
            continue
        dest = out_root / src.relative_to(REPO_ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        bytes_in += src.stat().st_size
        bytes_out += len(content.encode("utf-8"))

    (out_root / "README.md").write_text(BRANCH_README, encoding="utf-8")

    print(
        f"normalized {len(files) - n_failed}/{len(files)} files: "
        f"{bytes_in / 1e6:.1f} MB -> {bytes_out / 1e6:.1f} MB"
    )
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
