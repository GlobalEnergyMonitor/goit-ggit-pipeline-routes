#!/usr/bin/env python3
"""Sanity-check every route's geometry against its DB countries (bbox test).

Catches gross misplacements -- a route (or a stray copied feature) sitting in
a completely different part of the world than the countries the tracker DB
says the pipeline runs through. This is the class of error that put P5259's
Basra Sealines geometry into P0530 (Sumed, Egypt) via a bad batch export and
shipped it in a release.

The check is deliberately coarse and conservative so it can gate the
normalize workflow without false alarms:

  - each country's Natural Earth polygons are reduced to per-polygon
    bounding boxes, padded by BBOX_PAD_DEG (offshore terminals, sealines,
    border wobble)
  - a feature FAILS only when NONE of its vertices fall inside any padded
    bbox of the route's DB countries (CountriesOrAreas + Start/End) -- a
    subsea route between two DB countries still passes on its landfalls
  - routes missing from the DB, with unresolvable country names, or with
    null geometry are skipped (reported, never fatal)

Finer-grained checks (per-vertex polygon coverage, endpoint countries,
length) live in qc_routes.py, which runs at intake; this gate exists because
batch imports have historically bypassed intake QC.

Data sources (same as qc_routes.py, cached in ~/.cache/gem-route-qc):
  - tracker DB tabs via the authenticated Sheets API (`gws` CLI, read-only
    work profile; anonymous CSV export was disabled org-wide 2026-07-29)
  - Natural Earth 10m admin_0 countries GeoJSON

If a download fails, the check is SKIPPED with a loud warning and exit 0 --
normalization must not be blocked by Google/GitHub availability. NOTE: in CI
there is no gws install or credential, so the DB fetch always fails there and
this check now effectively runs only on local machines (CI skips it unless a
Sheets service-account secret is wired up someday).

Usage:
  check_route_countries.py            # check every route in the repo
  check_route_countries.py --refresh  # re-download DB tabs + boundaries

Exit codes: 0 = ok/skipped, 1 = at least one route failed. Stdlib only.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import unicodedata
import urllib.request
from difflib import get_close_matches
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "data" / "individual-routes"
CACHE = Path.home() / ".cache" / "gem-route-qc"

# same sheet + tabs as qc_routes.py. Anonymous CSV endpoints (gviz /
# export?format=csv) were deliberately disabled org-wide on 2026-07-29: the
# sheet is readable only through the authenticated Sheets API, here via the
# read-only `gws` work profile. Never reintroduce a public-URL fallback. In CI
# (no gws, no credentials) the fetch fails and the check is skipped as below.
SHEET_ID = "1foPLE6K-uqFlaYgLPAUxzeXfDO5wOOqE7tibNHeqTek"
DB_TABS = {  # fuel folder -> tab title
    "gas-pipelines": "Gas pipelines",
    "liquid-pipelines": "Oil/NGL pipelines",
}
GWS_ENV = {
    "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(Path.home() / ".config" / "gws-gem"),
    "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file",
}
HEADER_ROW = 2  # rows 0-1 are annotation rows

NE_NAME = "ne_10m_admin_0_countries.geojson"
NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
          "master/geojson/" + NE_NAME)
NE_NAME_FIELDS = ("NAME", "NAME_LONG", "NAME_EN", "ADMIN", "FORMAL_EN",
                  "BRK_NAME", "GEOUNIT", "SOVEREIGNT")

# DB country spelling -> Natural Earth spelling (both normalised before lookup)
COUNTRY_ALIASES = {
    "unitedstates": "united states of america",
    "usa": "united states of america",
    "turkiye": "turkey",
    "czechrepublic": "czechia",
    "ivorycoast": "cote d'ivoire",
    "drcongo": "dem. rep. congo",
    "democraticrepublicofthecongo": "dem. rep. congo",
    "republicofthecongo": "congo",
    "southkorea": "south korea",
    "northkorea": "north korea",
    "laopdr": "laos",
    "burma": "myanmar",
    "swaziland": "eswatini",
    "macedonia": "north macedonia",
    "unitedarabemirates": "united arab emirates",
    "unitedkingdom": "united kingdom",
    "russianfederation": "russia",
}

# Padding around each country polygon's bbox, in degrees. Generous on purpose:
# it must absorb genuinely far-offshore infrastructure -- North Sea platform
# tie-backs sit ~300 km (2.7 deg) from the Norwegian/UK bbox edges, Gulf
# sealines ~0.5 deg out -- while staying far smaller than a continent: the
# failures we hunt are thousands of km off, not hundreds.
BBOX_PAD_DEG = 3.0


def norm(s):
    """Lowercase, strip accents and non-alphanumerics for name matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def fetch(url, dest, note, refresh):
    if not refresh and dest.exists():
        return dest.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {note} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    return data


def fetch_db_tab(title, dest, refresh):
    """One tracker tab via the authenticated Sheets API (`gws` CLI, read-only
    work profile), cached as a CSV grid in the same shape qc_routes.py writes.
    Raises if gws is unavailable (e.g. CI) -- callers treat that as SKIP."""
    if not refresh and dest.exists():
        return list(csv.reader(dest.read_text(encoding="utf-8").splitlines()))
    print(f"  downloading DB tab {title!r} via gws ...", file=sys.stderr)
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get", "--params",
         json.dumps({"spreadsheetId": SHEET_ID, "range": f"'{title}'"})],
        env={**os.environ, **GWS_ENV},
        capture_output=True, text=True, check=True).stdout
    # the CLI prints a non-JSON preamble line ("Using keyring backend: file")
    values = json.loads(out[out.index("{"):])["values"]
    width = max(len(r) for r in values)
    rows = [r + [""] * (width - len(r)) for r in values]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, quoting=csv.QUOTE_ALL).writerows(rows)
    return rows


def load_db_countries(refresh):
    """{ProjectID: set of DB country strings (CountriesOrAreas + Start/End)}."""
    out = {}
    for fuel, title in DB_TABS.items():
        rows = fetch_db_tab(title, CACHE / f"db_{fuel}.csv", refresh)
        hdr = rows[HEADER_ROW]
        idx = {name: i for i, name in enumerate(hdr) if name}

        def get(row, name):
            i = idx.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        for row in rows[HEADER_ROW + 1:]:
            pid = get(row, "ProjectID")
            if not pid:
                continue
            names = set()
            for c in get(row, "CountriesOrAreas").replace(";", ",").split(","):
                if c.strip():
                    names.add(c.strip())
            for f in ("StartCountryOrArea", "EndCountryOrArea"):
                if get(row, f):
                    names.add(get(row, f))
            out[pid] = names
    return out


def polygon_bboxes(geom):
    """Per-polygon (lon_min, lat_min, lon_max, lat_max) list for a NE geometry.

    Per-polygon rather than whole-country so multi-part countries (France +
    French Guiana, US + Alaska + Hawaii) don't produce one bbox spanning half
    the globe."""
    gtype = geom.get("type")
    polys = []
    if gtype == "Polygon":
        polys = [geom["coordinates"]]
    elif gtype == "MultiPolygon":
        polys = geom["coordinates"]
    boxes = []
    for poly in polys:
        ring = poly[0]  # exterior ring bounds the polygon
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        boxes.append((min(lons), min(lats), max(lons), max(lats)))
    return boxes


def load_country_bboxes(refresh):
    """(normalised name -> feature index, feature index -> [bbox, ...])."""
    raw = fetch(NE_URL, CACHE / NE_NAME, "Natural Earth country boundaries",
                refresh)
    ne = json.loads(raw)
    name2id = {}
    boxes = []
    for feat in ne["features"]:
        boxes.append(polygon_bboxes(feat["geometry"]))
    # field-major so a strong field (NAME) of any feature beats a weak one
    # (SOVEREIGNT) of an earlier feature (same rule as qc_routes.py)
    for f in NE_NAME_FIELDS:
        for i, feat in enumerate(ne["features"]):
            v = feat["properties"].get(f)
            if v:
                name2id.setdefault(norm(v), i)
    return name2id, boxes


def resolve(name2id, db_name):
    key = norm(db_name)
    if not key:
        return None
    key = norm(COUNTRY_ALIASES.get(key, key))
    if key in name2id:
        return name2id[key]
    m = get_close_matches(key, list(name2id), n=1, cutoff=0.85)
    return name2id[m[0]] if m else None


def feature_vertices(geom):
    """All (lon, lat) vertices of any geometry type."""
    if geom is None:
        return []
    if geom.get("type") == "GeometryCollection":
        verts = []
        for g in geom.get("geometries", []):
            verts.extend(feature_vertices(g))
        return verts

    def walk(coords):
        if not isinstance(coords, list) or not coords:
            return
        if isinstance(coords[0], (int, float)):
            yield (coords[0], coords[1])
        else:
            for c in coords:
                yield from walk(c)
    return list(walk(geom.get("coordinates")))


def in_any_bbox(lon, lat, bboxes):
    for lo, la, hi, ha in bboxes:
        if (lo - BBOX_PAD_DEG <= lon <= hi + BBOX_PAD_DEG and
                la - BBOX_PAD_DEG <= lat <= ha + BBOX_PAD_DEG):
            return True
    return False


def check_file(path, db, name2id, country_boxes):
    """Return (failures, skip_reason). failures is a list of message strings."""
    pid = path.name[:-len(".geojson")].replace("-compressor-stations", "") \
        .replace("-compressor-station", "")
    countries = db.get(pid)
    if countries is None:
        return [], "not in DB"
    allowed = []
    unresolved = []
    for c in countries:
        i = resolve(name2id, c)
        if i is None:
            unresolved.append(c)
        else:
            allowed.extend(country_boxes[i])
    if not allowed:
        return [], ("no resolvable DB countries"
                    + (f" ({', '.join(unresolved)})" if unresolved else ""))

    data = json.loads(path.read_text(encoding="utf-8"))
    feats = ([data] if data.get("type") == "Feature"
             else data.get("features", []))
    failures = []
    for n, feat in enumerate(feats):
        verts = feature_vertices((feat or {}).get("geometry"))
        if not verts:
            continue
        if not any(in_any_bbox(lon, lat, allowed) for lon, lat in verts):
            lons = [v[0] for v in verts]
            lats = [v[1] for v in verts]
            failures.append(
                f"feature {n + 1}/{len(feats)} entirely outside DB countries "
                f"({', '.join(sorted(countries))}): {len(verts)} vertices in "
                f"lon {min(lons):.2f}..{max(lons):.2f}, "
                f"lat {min(lats):.2f}..{max(lats):.2f}")
    return failures, None


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true",
                        help="re-download the DB tabs and country boundaries")
    args = parser.parse_args(argv)

    try:
        db = load_db_countries(args.refresh)
        name2id, country_boxes = load_country_bboxes(args.refresh)
    except Exception as e:  # network/availability must never block normalize
        print(f"WARNING: could not load DB or boundaries ({e}) -- "
              "SKIPPING country sanity check", file=sys.stderr)
        return 0

    files = sorted(ROUTES_DIR.glob("*/*.geojson"))
    n_checked = 0
    skipped = {}
    bad = []
    for path in files:
        try:
            failures, skip = check_file(path, db, name2id, country_boxes)
        except (ValueError, json.JSONDecodeError, OSError) as e:
            # structural problems are validate_geojson.py's job
            skipped.setdefault(f"unreadable ({e.__class__.__name__})", []) \
                .append(path.name)
            continue
        if skip:
            skipped.setdefault(skip, []).append(path.name)
            continue
        n_checked += 1
        if failures:
            bad.append((path, failures))

    for reason, names in sorted(skipped.items()):
        shown = " ".join(names[:8]) + (" ..." if len(names) > 8 else "")
        print(f"  skipped {len(names)} ({reason}): {shown}", file=sys.stderr)

    if bad:
        print(f"\n{len(bad)} route file(s) FAILED the country sanity check:")
        for path, failures in bad:
            print(f"\n✗ {path.relative_to(REPO_ROOT)}")
            for f in failures:
                print(f"    {f}")
        print("\nA failing feature has no vertex within "
              f"{BBOX_PAD_DEG} deg of any DB country of its project -- "
              "usually a batch-export mixup (wrong route saved under this "
              "ProjectID, or a stray copied feature). Fix the file on main "
              "or correct the DB countries; the normalized branch will not "
              "update until this passes.")
        return 1

    print(f"country sanity check: {n_checked}/{len(files)} route files ok "
          f"({len(files) - n_checked} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
