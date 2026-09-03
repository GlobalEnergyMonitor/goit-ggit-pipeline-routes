#!/usr/bin/env python3
"""QC pipeline-route GeoJSON files before they enter the repo.

Researchers upload P####.geojson routes to per-person folders in the shared
Google Drive during an update cycle. This tool runs a richer QC pass than the
CI validator (scripts/validate_geojson.py) by comparing each route against the
authoritative pipeline database (the tracker Google Sheet), then -- only on an
explicit second invocation -- stages the good routes into the repo.

Two phases:

  1. REPORT (default, read-only)
       qc_routes.py FOLDER_OR_FILES ...
     Runs every check and prints a per-route PASS / WARN / FAIL report. Writes a
     machine-readable qc-results.json next to this script's output for the copy
     phase to reuse.

  2. COPY (explicit, mutates the repo)
       qc_routes.py FOLDER_OR_FILES ... --copy [--include P1234 P5678] [--force]
     Re-runs the checks, then copies routes into data/individual-routes/<fuel>/
     (fuel = which DB tab the project lives in). PASS routes are copied by
     default; WARN routes only when named with --include; FAIL routes are never
     copied. Nothing is committed -- that stays a manual step.

Checks
  FAIL  - structural errors from validate_geojson.validate_file (bad JSON/
          GeoJSON, non-WGS84 crs, Z coords, out-of-range lon/lat, bad filename)
        - ProjectID absent from the gas + oil DB tabs
        - endpoint country mismatch vs DB Start/EndCountryOrArea
  WARN  - route length far from DB length
        - vertices straying outside the DB CountriesOrAreas set
        - geometry near-identical to an unrelated project's route
        - fuel-folder mismatch for an existing file
        - a geocoded DB Start/EndLocation far from the matching endpoint
        - "map:" -- the tracker row lacks something the interim map build
          needs, so the route would be invisible on the map even once merged:
          Fuel not in the map's fuel set, PipelineName blank, Status blank /
          N/A / not a map filter value, RouteAccuracy blank or still 'no route'
          (the build nulls that geometry). Fix the sheet, then re-run with
          --refresh.
  INFO  - crude/low-resolution geometry (only elevated when RouteAccuracy is
          high) ; null-island or duplicate consecutive vertices ; new-vs-update

Requires shapely, pyproj (both already used elsewhere) and the stdlib. Cached
data (DB CSVs, Natural Earth boundaries, geocode results) lives in
~/.cache/gem-route-qc and never touches the repo.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from difflib import get_close_matches
from pathlib import Path

from pyproj import Geod
from shapely.geometry import Point, shape
from shapely.ops import nearest_points
from shapely.strtree import STRtree

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = REPO_ROOT / "data" / "individual-routes"
CACHE = Path.home() / ".cache" / "gem-route-qc"

# import the CI validator so structural checks are never duplicated
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_geojson import validate_file  # noqa: E402

GEOD = Geod(ellps="WGS84")

# --- pipeline DB (tracker Google Sheet) --------------------------------------
# Anonymous CSV endpoints (gviz / export?format=csv) were deliberately disabled
# org-wide on 2026-07-29: the sheet is readable only through the authenticated
# Sheets API, here via the read-only `gws` work profile. Never reintroduce a
# public-URL fallback.
SHEET_ID = "1foPLE6K-uqFlaYgLPAUxzeXfDO5wOOqE7tibNHeqTek"
DB_TABS = {  # fuel folder -> tab title
    "gas-pipelines": "Gas pipelines",
    "liquid-pipelines": "Oil/NGL pipelines",
}
GWS_ENV = {
    "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(Path.home() / ".config" / "gws-gem"),
    "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file",
}

NE_NAME = "ne_10m_admin_0_countries.geojson"
NE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
          "master/geojson/" + NE_NAME)


def prune_cache():
    """Delete orphaned per-tab DB caches left behind by an earlier run.

    The DB caches are named `db_<fuel>.csv` from the current DB_TABS keys, so
    renaming a key (e.g. `oil` -> `liquid-pipelines`) orphans the old file.
    Orphans are harmless to the tool -- it only ever reads the paths it writes
    -- but they are stale cruft and can mislead anyone inspecting the cache by
    hand. Remove any `db_*.csv` that isn't an expected filename; leave every
    other cache file (Natural Earth, geocode) untouched. Returns removed names.
    """
    if not CACHE.exists():
        return []
    expected = {f"db_{fuel}.csv" for fuel in DB_TABS}
    removed = []
    for p in CACHE.glob("db_*.csv"):
        if p.name not in expected:
            p.unlink()
            removed.append(p.name)
    if removed:
        print(f"  pruned {len(removed)} stale DB cache file(s): "
              f"{', '.join(sorted(removed))}", file=sys.stderr)
    return removed
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

# thresholds
LENGTH_TOL = 0.30          # >30% length mismatch -> WARN
# A single straight segment this long is a low-resolution cue. Pipelines can
# genuinely run straight for tens of km, so the bar for a *high*-accuracy route
# to look suspiciously crude is much higher than for a low-accuracy one.
BIG_JUMP_CRUDE_KM = 40.0   # crude/low-accuracy: INFO cue to refine later
BIG_JUMP_HIGH_KM = 75.0    # high-accuracy: WARN, unusually long for a detailed trace
GEOCODE_FAR_KM = 50.0      # geocoded DB location this far from endpoint -> WARN

# Map visibility -- mirrors the row filter in goit-ggit-data-ops
# releases/downloads/pipeline_exports.py (fetch_pipeline_data +
# enforce_no_route_null_geometry), which builds ggit/goit_map_latest.geojson
# for the interim maps. A merged route whose tracker row fails these never
# reaches the map. Fuel sets come from gem-tracker-constants when importable.
try:
    from gem_tracker_constants import GAS_FUEL_OPTIONS, OIL_NGL_COMBINED
except ImportError:  # fallback copy -- keep in sync with gem-tracker-constants
    GAS_FUEL_OPTIONS = ["Gas", "Gas and Hydrogen"]
    OIL_NGL_COMBINED = ["Oil", "NGL", "NGL, oil products", "LPG", "Oil, NGL",
                        "Oil, NGL, naphtha", "Naphtha (only)",
                        "Oil products (only)", "Naphtha, oil products",
                        "Condensate", "Oil, oil products", "Condensate/NGL",
                        "Oil, condensate"]
MAP_FUELS = {  # fuel folder -> Fuel values the matching map build keeps
    "gas-pipelines": set(GAS_FUEL_OPTIONS),                 # --pipeline-type Gas
    "liquid-pipelines": set(OIL_NGL_COMBINED) | {"CO2"},    # --pipeline-type Oil-NGL
}
# Status values the interim map's filter panel knows (goit-ggit-interim-maps
# trackers/*/config.js `filters`); anything else is filtered out on load.
MAP_STATUSES = {"operating", "construction", "proposed", "shelved", "mothballed",
                "cancelled", "retired", "idle", "mixed status"}
OFFSHORE_TOL_KM = 25.0     # endpoint this far offshore still counts as a country
                           # (Natural Earth 10m omits small islands; Gulf/coastal
                           # terminals sit a fair way from the mainland polygon)
# big jumps expected here. 'very low (straight line/schematic)' is the crudest
# tier of all -- omitting it made every correctly-classified 2-point schematic
# warn about being a 2-point schematic.
CRUDE_ACCURACIES = {"very low (straight line/schematic)",
                    "low", "medium", "no route", ""}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def norm(s):
    """Lowercase, strip accents and non-alphanumerics for fuzzy name matching."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def download(url, dest, note):
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {note} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    return data


def fetch_db_tab(title, dest):
    """Download one tracker tab via the authenticated Sheets API (`gws` CLI,
    read-only work profile) and cache it as a CSV grid matching the shape the
    old gviz export produced (full grid from row 1, QUOTE_ALL)."""
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


# --------------------------------------------------------------------------- #
# pipeline database
# --------------------------------------------------------------------------- #
HEADER_ROW = 2   # the real column-title row (3rd line); rows 0-1 are annotations


def patch_header(hdr):
    """Repair the one header the old gviz CSV export lost.

    The length-value column is titled `LengthKnown` in the sheet, but it's a
    merged header cell -- Google's gviz export wrote merged text into a single
    underlying cell and left this one blank, so `LengthKnown` never reached
    the CSV and the column arrived nameless. The authenticated Sheets API
    export keeps the header, so this only fires on a gviz-era cache; it
    restores the name (always immediately left of `LengthKnownUnits`) so the
    column is addressable by name. Returns True if a fix was applied.
    """
    try:
        u = hdr.index("LengthKnownUnits")
    except ValueError:
        return False
    if u > 0 and not hdr[u - 1].strip():
        hdr[u - 1] = "LengthKnown"
        return True
    return False


def load_db(refresh=False):
    """Return {ProjectID: {...fields..., 'fuel': folder}} across gas + oil tabs."""
    db = {}
    for fuel, title in DB_TABS.items():
        path = CACHE / f"db_{fuel}.csv"
        if refresh or not path.exists():
            fetch_db_tab(title, path)
        rows = list(csv.reader(path.open(encoding="utf-8")))
        if patch_header(rows[HEADER_ROW]):
            # rewrite the cached export so it no longer carries a blank header
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh, quoting=csv.QUOTE_ALL).writerows(rows)
        hdr = rows[HEADER_ROW]
        idx = {name: i for i, name in enumerate(hdr) if name}

        def get(row, name):
            i = idx.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        for row in rows[HEADER_ROW + 1:]:
            pid = get(row, "ProjectID")
            if not pid:
                continue
            db[pid] = {
                "fuel": fuel,
                "Fuel": get(row, "Fuel"),
                "PipelineName": get(row, "PipelineName"),
                "Wiki": get(row, "Wiki"),
                "StartLocation": get(row, "StartLocation"),
                "StartCountryOrArea": get(row, "StartCountryOrArea"),
                "EndLocation": get(row, "EndLocation"),
                "EndCountryOrArea": get(row, "EndCountryOrArea"),
                "CountriesOrAreas": get(row, "CountriesOrAreas"),
                "RouteAccuracy": get(row, "RouteAccuracy"),
                "RouteType": get(row, "RouteType"),
                "PipelineNetworkGrouping": get(row, "PipelineNetworkGrouping"),
                "Status": get(row, "Status"),
                "Length": get(row, "LengthKnown"),
                "LengthUnits": get(row, "LengthKnownUnits"),
            }
    return db


# --------------------------------------------------------------------------- #
# country boundaries (Natural Earth)
# --------------------------------------------------------------------------- #
class Countries:
    def __init__(self, refresh=False):
        path = CACHE / NE_NAME
        if refresh or not path.exists():
            download(NE_URL, path, "Natural Earth country boundaries")
        ne = json.loads(path.read_text())
        self.geoms = []
        self.names = []            # canonical NAME per feature
        self.name2id = {}          # normalised name -> feature index
        for feat in ne["features"]:
            props = feat["properties"]
            geom = shape(feat["geometry"])
            self.geoms.append(geom)
            self.names.append(props.get("NAME") or props.get("ADMIN") or "?")
        # field-major so a strong field (NAME) of any feature beats a weak
        # one (SOVEREIGNT) of an earlier feature — else e.g. "Netherlands"
        # resolves to Sint Maarten (whose SOVEREIGNT is "Netherlands")
        for f in NE_NAME_FIELDS:
            for i, feat in enumerate(ne["features"]):
                v = feat["properties"].get(f)
                if v:
                    self.name2id.setdefault(norm(v), i)
        self.tree = STRtree(self.geoms)

    def country_of(self, lon, lat):
        """Feature index of the country containing (lon,lat), or the nearest
        within OFFSHORE_TOL_KM, else None."""
        pt = Point(lon, lat)
        for i in self.tree.query(pt, predicate="intersects"):
            if self.geoms[i].covers(pt):
                return int(i)
        # just offshore? snap to the nearest country within tolerance
        j = int(self.tree.nearest(pt))
        p2 = nearest_points(self.geoms[j], pt)[0]   # nearest point on the country
        _, _, dist = GEOD.inv(lon, lat, p2.x, p2.y)
        return j if dist / 1000.0 <= OFFSHORE_TOL_KM else None

    def resolve(self, db_name):
        """DB country string -> feature index (alias + fuzzy), or None."""
        key = norm(db_name)
        if not key:
            return None
        key = COUNTRY_ALIASES.get(key, key)
        key = norm(key)
        if key in self.name2id:
            return self.name2id[key]
        m = get_close_matches(key, list(self.name2id), n=1, cutoff=0.85)
        return self.name2id[m[0]] if m else None

    def same(self, feat_id, db_name):
        """True if the point's country matches the DB country name."""
        if feat_id is None:
            return None            # couldn't place the point
        target = self.resolve(db_name)
        if target is None:
            return None            # couldn't resolve DB name
        return feat_id == target


# --------------------------------------------------------------------------- #
# geometry extraction
# --------------------------------------------------------------------------- #
def read_geojson(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def line_coords(gj):
    """All LineString/MultiLineString coordinate sequences in the file."""
    if gj.get("type") == "Feature":
        feats = [gj]
    elif gj.get("type") == "FeatureCollection":
        feats = gj.get("features", [])
    else:
        feats = []
    lines = []
    for ft in feats:
        g = (ft or {}).get("geometry")
        if not g:
            continue
        t = g.get("type")
        if t == "LineString":
            lines.append(g["coordinates"])
        elif t == "MultiLineString":
            lines.extend(g["coordinates"])
        elif t == "GeometryCollection":
            for sub in g.get("geometries", []):
                if sub.get("type") == "LineString":
                    lines.append(sub["coordinates"])
                elif sub.get("type") == "MultiLineString":
                    lines.extend(sub["coordinates"])
    return [ln for ln in lines if len(ln) >= 2]


def geodesic_km(line):
    return GEOD.line_length([p[0] for p in line], [p[1] for p in line]) / 1000.0


def geom_hash(lines):
    """Order-insensitive hash of rounded coords -- catches copied routes."""
    rounded = sorted(
        tuple((round(p[0], 5), round(p[1], 5)) for p in ln) for ln in lines
    )
    return hashlib.sha256(json.dumps(rounded).encode()).hexdigest() if rounded else None


# --------------------------------------------------------------------------- #
# repo index (for duplicate + new/update detection)
# --------------------------------------------------------------------------- #
def build_repo_index():
    """Return (hash -> [ProjectID...], ProjectID -> Path) over existing routes."""
    by_hash, by_pid = {}, {}
    for f in ROUTES_DIR.glob("*/*.geojson"):
        pid = f.name[:-len(".geojson")].replace("-compressor-stations", "") \
            .replace("-compressor-station", "")
        by_pid.setdefault(pid, f)
        try:
            h = geom_hash(line_coords(read_geojson(f)))
        except Exception:
            continue
        if h:
            by_hash.setdefault(h, []).append(f.name[:-len(".geojson")])
    return by_hash, by_pid


# --------------------------------------------------------------------------- #
# geocoding (Nominatim, cached, soft signal only)
# --------------------------------------------------------------------------- #
class Geocoder:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.path = CACHE / "geocode_cache.json"
        self.cache = {}
        if self.path.exists():
            try:
                self.cache = json.loads(self.path.read_text())
            except Exception:
                self.cache = {}
        self._last = 0.0

    def geocode(self, query):
        if not self.enabled or not query.strip():
            return None
        if query in self.cache:
            return self.cache[query]
        # be polite: <=1 request/second
        wait = 1.05 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "limit": 1})
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "gem-route-qc/1.0 (GEM pipeline QC)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            self._last = time.time()
            result = [float(data[0]["lon"]), float(data[0]["lat"])] if data else None
        except Exception:
            result = None
        self.cache[query] = result
        return result

    def save(self):
        try:
            self.path.write_text(json.dumps(self.cache))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# per-route check
# --------------------------------------------------------------------------- #
class Result:
    def __init__(self, path, pid):
        self.path = path
        self.pid = pid
        self.status = "PASS"          # PASS | WARN | FAIL
        self.map_hidden = False       # tracker row would drop it from the map
        self.fuel = None              # target folder
        self.state = None             # NEW | UPDATE | UPDATE(unchanged)
        self.notes = []               # (level, text) ; level in FAIL/WARN/OK/INFO

    def add(self, level, text):
        self.notes.append((level, text))
        order = {"FAIL": 3, "WARN": 2}
        if order.get(level, 0) > order.get(self.status, 0):
            self.status = level


def check_map_visibility(res, rec):
    """Flag tracker-row gaps that would keep a merged route off the interim map."""
    fuel_ok = MAP_FUELS.get(rec["fuel"], set())
    problems = []
    if rec["Fuel"] not in fuel_ok:
        problems.append(f"Fuel {rec['Fuel'] or '<blank>'!r} is not in the "
                        f"{rec['fuel']} map's fuel set {sorted(fuel_ok)}")
    if not rec["PipelineName"]:
        problems.append("PipelineName blank")
    st = rec["Status"]
    if not st or st == "N/A":
        problems.append(f"Status {st or '<blank>'!r}")
    elif st.lower() not in MAP_STATUSES:
        problems.append(f"Status {st!r} is not a map filter value")
    acc = rec["RouteAccuracy"]
    if not acc:
        problems.append("RouteAccuracy blank")
    elif acc.lower() == "no route":
        problems.append("RouteAccuracy is 'no route' -- the map build nulls "
                        "the geometry until the cell is updated")
    for msg in problems:
        res.add("WARN", f"map: {msg}")
    if problems:
        res.map_hidden = True
    if not rec["CountriesOrAreas"]:
        res.add("INFO", "map: CountriesOrAreas blank (renders, but no country "
                        "in the table/filter)")
    if not rec["Wiki"]:
        res.add("INFO", "map: Wiki blank (gets a card of its own; segments of "
                        "one pipeline won't group)")


def check_route(path, db, countries, repo_hash, repo_pid, geocoder):
    path = Path(path)
    pid = path.name[:-len(".geojson")] if path.name.endswith(".geojson") else path.name
    pid = pid.replace("-compressor-stations", "").replace("-compressor-station", "")
    res = Result(path, pid)

    # 1. structural validity (reuse the CI validator)
    errors, warnings = validate_file(path)
    for e in errors:
        res.add("FAIL", f"structure: {e}")
    for w in warnings:
        res.add("WARN", f"structure: {w}")
    if errors:
        return res  # nothing more is meaningful on a broken file

    # 2. DB presence + fuel
    rec = db.get(pid)
    if not rec:
        res.add("FAIL", f"{pid} not found in gas/oil DB "
                        "(hydrogen routes live in a separate sheet -- check fuel)")
        return res
    res.fuel = rec["fuel"]
    check_map_visibility(res, rec)

    # 3. new vs update + duplicate detection
    lines = line_coords(read_geojson(path))
    h = geom_hash(lines)
    existing = repo_pid.get(pid)
    if existing is None:
        res.state = "NEW"
    else:
        try:
            same = geom_hash(line_coords(read_geojson(existing))) == h
        except Exception:
            same = False
        res.state = "UPDATE(unchanged)" if same else "UPDATE"
    # fuel-folder mismatch for an existing file
    if existing is not None and existing.parent.name != rec["fuel"]:
        res.add("WARN", f"currently in {existing.parent.name}, DB fuel is "
                        f"{rec['fuel']}")
    # duplicate of a *different* project?
    if h:
        twins = [t for t in repo_hash.get(h, []) if t != pid and
                 t.replace("-compressor-stations", "") != pid]
        if twins:
            twin = twins[0]
            trec = db.get(twin.replace("-compressor-stations", ""))
            grp = rec["PipelineNetworkGrouping"]
            intentional = trec and (
                (grp and trec["PipelineNetworkGrouping"] == grp) or
                (trec["CountriesOrAreas"] == rec["CountriesOrAreas"] and
                 rec["CountriesOrAreas"]))
            if intentional:
                res.add("INFO", f"geometry identical to {twin} -- likely an "
                                "intentional parallel route")
            else:
                res.add("WARN", f"geometry identical to {twin} -- possible "
                                "copy-paste error (verify it should differ)")

    # empty (null-geometry) route: valid, but note it
    if not lines:
        res.add("INFO", "empty route (null geometry) -- no geometry checks")
        return res

    # geometry summary
    total_km = sum(geodesic_km(ln) for ln in lines)
    npts = sum(len(ln) for ln in lines)
    res.add("OK", f"{'LineString' if len(lines) == 1 else 'MultiLineString'} "
                  f"· {npts} pts · {total_km:.0f} km")

    verts = [p for ln in lines for p in ln]
    start, end = verts[0], verts[-1]

    # 4. endpoint country match (endpoints may be reversed vs the DB)
    c_start = countries.country_of(start[0], start[1])
    c_end = countries.country_of(end[0], end[1])
    db_s, db_e = rec["StartCountryOrArea"], rec["EndCountryOrArea"]

    def endpoint_line():
        sn = countries.names[c_start] if c_start is not None else "off-map"
        en = countries.names[c_end] if c_end is not None else "off-map"
        return sn, en
    sn, en = endpoint_line()

    if db_s or db_e:
        # accept either orientation
        forward = (countries.same(c_start, db_s), countries.same(c_end, db_e))
        reverse = (countries.same(c_start, db_e), countries.same(c_end, db_s))
        ok_fwd = forward[0] is not False and forward[1] is not False
        ok_rev = reverse[0] is not False and reverse[1] is not False
        if forward == (True, True) or reverse == (True, True):
            extra = " (endpoints reversed vs DB)" if reverse == (True, True) \
                and forward != (True, True) else ""
            res.add("OK", f"endpoints: {sn} -> {en} match DB "
                          f"({db_s} -> {db_e}){extra}")
        elif ok_fwd or ok_rev:
            res.add("OK", f"endpoints: {sn} -> {en} vs DB ({db_s} -> {db_e}) "
                          "-- partial/unresolved match")
        else:
            res.add("FAIL", f"endpoint country mismatch: route {sn} -> {en}, "
                            f"DB {db_s} -> {db_e}")
    else:
        res.add("INFO", f"endpoints: {sn} -> {en} (DB start/end country blank)")

    # 5. country coverage
    coa = [c.strip() for c in rec["CountriesOrAreas"].replace(";", ",").split(",")
           if c.strip()]
    if coa:
        allowed = {countries.resolve(c) for c in coa}
        allowed.discard(None)
        if allowed:
            stray = {}
            for lon, lat in verts:
                cid = countries.country_of(lon, lat)
                if cid is not None and cid not in allowed:
                    stray[countries.names[cid]] = stray.get(countries.names[cid], 0) + 1
            if stray:
                where = ", ".join(f"{k} ({v})" for k, v in sorted(
                    stray.items(), key=lambda x: -x[1]))
                res.add("WARN", f"coverage: {sum(stray.values())}/{len(verts)} "
                                f"vertices outside DB countries -> {where}")
            else:
                res.add("OK", f"coverage: all {len(verts)} vertices within "
                              f"{rec['CountriesOrAreas']}")

    # 6. length vs DB
    try:
        db_len = float(rec["Length"])
        if rec["LengthUnits"] == "mi":
            db_len *= 1.60934
    except ValueError:
        db_len = None
    if db_len and db_len > 0:
        diff = (total_km - db_len) / db_len
        msg = f"length: route {total_km:.0f} km vs DB {db_len:.0f} km ({diff:+.0%})"
        res.add("WARN" if abs(diff) > LENGTH_TOL else "OK", msg)

    # 7. crude / big-jump geometry (a cue, severity depends on RouteAccuracy)
    seg_max = 0.0
    for ln in lines:
        for a, b in zip(ln, ln[1:]):
            seg_max = max(seg_max, geodesic_km([a, b]))
    acc = rec["RouteAccuracy"].strip().lower()
    if acc in CRUDE_ACCURACIES:
        if seg_max > BIG_JUMP_CRUDE_KM:
            res.add("INFO", f"longest straight segment {seg_max:.0f} km -- "
                            f"expected for '{rec['RouteAccuracy']}' accuracy "
                            "(low-res, fix eventually)")
    elif seg_max > BIG_JUMP_HIGH_KM:
        res.add("WARN", f"longest straight segment {seg_max:.0f} km with "
                        f"'{rec['RouteAccuracy']}' accuracy -- geometry looks "
                        "crude for its rating")
    if npts <= 2 and acc not in CRUDE_ACCURACIES:
        res.add("WARN", f"only {npts} points but accuracy is "
                        f"'{rec['RouteAccuracy']}'")
    # null island
    if any(abs(lon) < 0.01 and abs(lat) < 0.01 for lon, lat in verts):
        res.add("WARN", "a vertex sits at ~(0,0) (null island)")

    # 8. geocode hint (soft)
    for label, name, cc, pt in (
        ("start", rec["StartLocation"], rec["StartCountryOrArea"], start),
        ("end", rec["EndLocation"], rec["EndCountryOrArea"], end)):
        if not name:
            continue
        q = f"{name}, {cc}" if cc else name
        loc = geocoder.geocode(q)
        if not loc:
            continue
        _, _, d = GEOD.inv(pt[0], pt[1], loc[0], loc[1])
        dkm = d / 1000.0
        # compare to the closer endpoint (orientation-agnostic)
        _, _, d2 = GEOD.inv(end[0] if label == "start" else start[0],
                            end[1] if label == "start" else start[1],
                            loc[0], loc[1])
        dkm = min(dkm, d2 / 1000.0)
        res.add("WARN" if dkm > GEOCODE_FAR_KM else "OK",
                f"geocode: DB {label} '{name}' ~{dkm:.0f} km from nearest endpoint")

    return res


# --------------------------------------------------------------------------- #
# reporting + copy
# --------------------------------------------------------------------------- #
GLYPH = {"FAIL": "✗", "WARN": "⚠", "OK": "✓", "INFO": "·", "PASS": "✓"}


def gather_files(targets):
    files = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_dir():
            files.extend(sorted(p.glob("*.geojson")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"skip (not found): {t}", file=sys.stderr)
    return files


def print_report(results):
    for r in results:
        folder = r.path.parent.name
        head = f"{r.pid:9} {r.fuel or '?':16} [{r.state or '-'}]"
        print(f"\n{head:44} {GLYPH[r.status]} {r.status}   ({folder}/{r.path.name})")
        for level, text in r.notes:
            print(f"   {GLYPH.get(level, ' ')} {text}")
    n_pass = sum(r.status == "PASS" for r in results)
    n_warn = sum(r.status == "WARN" for r in results)
    n_fail = sum(r.status == "FAIL" for r in results)
    print(f"\n{'='*60}\n{len(results)} route(s): "
          f"{n_pass} pass · {n_warn} warn · {n_fail} fail")
    if n_warn:
        warned = " ".join(sorted(r.pid for r in results if r.status == "WARN"))
        print(f"WARN routes: {warned}")
    hidden = sorted(r.pid for r in results if r.map_hidden)
    if hidden:
        print(f"MAP-HIDDEN routes (tracker row incomplete -- fix the sheet, then "
              f"re-run with --refresh): {' '.join(hidden)}")


# --- empty-feature stripping -------------------------------------------------
# Some researcher exports carry features whose geometry has an empty
# coordinates array ("coordinates": []). RFC 7946 requires at least two
# positions in a LineString, so these are invalid GeoJSON -- but they slip
# past validate_geojson.py, which only checks the positions that are there.
# They are pure noise (one upload carried 51 of them), so routes are stripped
# on the way into the repo: main keeps the researcher's file as submitted
# apart from these dead features.
#
# The edit is done on the raw text rather than by re-serializing, so the
# surviving features keep their original byte-for-byte formatting and full
# coordinate precision (main is the full-precision copy; only the normalized
# branch rounds). A file is never stripped down to zero features -- a route
# with no geometry at all is the null-geojson convention in README.md, not
# something this function should invent.

def _is_empty_geometry(obj):
    """True for a feature whose geometry exists but holds no usable position."""
    if not isinstance(obj, dict):
        return False
    geom = obj.get("geometry")
    if not isinstance(geom, dict):
        return False  # null geometry is the deliberate empty-route convention
    coords = geom.get("coordinates")
    if coords is None:
        return False
    gtype = geom.get("type")
    if gtype in ("LineString", "MultiPoint"):
        return len(coords) < 2
    if gtype == "MultiLineString":
        return all(len(part) < 2 for part in coords)
    if gtype == "Point":
        return len(coords) == 0
    return False


def _feature_spans(text):
    """Locate each element of the top-level "features" array in the raw text.

    Returns (spans, open_idx, close_idx) where spans is a list of
    (start, end, parsed_object). Raises ValueError if the array cannot be
    found, so callers can fall back to a plain copy.
    """
    key = text.find('"features"')
    if key < 0:
        raise ValueError('no "features" member')
    open_idx = text.find("[", key)
    if open_idx < 0:
        raise ValueError('no "features" array')
    decoder = json.JSONDecoder()
    spans = []
    i = open_idx + 1
    while True:
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            raise ValueError("unterminated features array")
        if text[i] == "]":
            return spans, open_idx, i
        obj, end = decoder.raw_decode(text, i)
        spans.append((i, end, obj))
        i = end
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i < len(text) and text[i] == ",":
            i += 1


def strip_empty_features(path):
    """Return (text, n_removed) with empty-geometry features deleted.

    text is None when nothing needed removing, so callers can copy the file
    unchanged. Falls back to no-op if the file cannot be scanned.
    """
    text = path.read_text()
    try:
        spans, _, close_idx = _feature_spans(text)
    except (ValueError, json.JSONDecodeError):
        return None, 0
    keep = [s for s in spans if not _is_empty_geometry(s[2])]
    n_drop = len(spans) - len(keep)
    if not n_drop or not keep:
        return None, 0
    # Everything before the first feature and from the last feature's end
    # onwards is copied verbatim. Between two kept features, the separator
    # is the text that originally followed the earlier one -- so a file
    # written one-feature-per-line keeps its newlines and a file written as
    # a single long line stays a single long line, and the diff is limited
    # to the removed features.
    index = {start: i for i, (start, _e, _o) in enumerate(spans)}
    out = [text[:spans[0][0]]]
    for i, (start, end, _obj) in enumerate(keep):
        if i:
            prev = index[keep[i - 1][0]]
            out.append(text[spans[prev][1]:spans[prev + 1][0]])
        out.append(text[start:end])
    out.append(text[spans[-1][1]:])
    return "".join(out), n_drop


def do_copy(results, include, force):
    include = set(include or [])
    copied, skipped = [], []
    for r in results:
        ok = r.status == "PASS" or (r.status == "WARN" and r.pid in include)
        if not ok or not r.fuel:
            skipped.append(r)
            continue
        dest_dir = ROUTES_DIR / r.fuel
        dest = dest_dir / f"{r.pid}.geojson"
        stripped, n_empty = strip_empty_features(r.path)
        if dest.exists() and not force:
            # only copy over an existing file when content actually differs;
            # compare against what would be written, so a file already
            # stripped in the repo counts as unchanged
            try:
                incoming = (stripped.encode() if stripped is not None
                            else r.path.read_bytes())
                if dest.read_bytes() == incoming:
                    skipped.append(r)
                    continue
            except Exception:
                pass
        dest_dir.mkdir(parents=True, exist_ok=True)
        if stripped is None:
            shutil.copy2(r.path, dest)
        else:
            dest.write_text(stripped)
        copied.append((r, dest, n_empty))
    print(f"\ncopied {len(copied)} route(s):")
    for r, dest, n_empty in copied:
        extra = f"  (dropped {n_empty} empty feature(s))" if n_empty else ""
        print(f"   {r.pid} -> {dest.relative_to(REPO_ROOT)}{extra}")
    left = [r for r in results if r.status == "FAIL"]
    if left:
        print(f"left behind {len(left)} FAIL route(s): "
              + " ".join(r.pid for r in left))
    warn_left = [r for r in results if r.status == "WARN" and r.pid not in include]
    if warn_left:
        print(f"skipped {len(warn_left)} WARN route(s) (add with --include): "
              + " ".join(r.pid for r in warn_left))


def main(argv):
    ap = argparse.ArgumentParser(description="QC pipeline-route GeoJSON files.")
    ap.add_argument("targets", nargs="+", help="folders and/or .geojson files")
    ap.add_argument("--copy", action="store_true",
                    help="stage PASS routes into the repo (mutates the repo)")
    ap.add_argument("--include", nargs="*", default=[],
                    help="ProjectIDs of WARN routes to also copy")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing repo files even if unchanged")
    ap.add_argument("--no-geocode", action="store_true",
                    help="skip the Nominatim place-name distance hint")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the DB tabs and country boundaries")
    ap.add_argument("--json", metavar="PATH", help="also write results as JSON")
    args = ap.parse_args(argv)

    files = gather_files(args.targets)
    if not files:
        print("no .geojson files to check", file=sys.stderr)
        return 2

    print("loading pipeline DB + country boundaries ...", file=sys.stderr)
    prune_cache()
    db = load_db(refresh=args.refresh)
    countries = Countries(refresh=args.refresh)
    repo_hash, repo_pid = build_repo_index()
    geocoder = Geocoder(enabled=not args.no_geocode)

    results = []
    for f in files:
        results.append(check_route(f, db, countries, repo_hash, repo_pid, geocoder))
    geocoder.save()

    print_report(results)

    if args.json:
        Path(args.json).write_text(json.dumps(
            [{"pid": r.pid, "status": r.status, "fuel": r.fuel,
              "state": r.state, "file": str(r.path),
              "map_hidden": r.map_hidden, "notes": r.notes} for r in results], indent=2))

    if args.copy:
        do_copy(results, args.include, args.force)

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
