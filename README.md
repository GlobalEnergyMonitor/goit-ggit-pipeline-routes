# GOIT-GGIT-pipeline-routes

Pipeline route geometries for Global Energy Monitor's [Global Oil Infrastructure Tracker (GOIT)](https://globalenergymonitor.org/projects/global-oil-infrastructure-tracker/) and [Global Gas Infrastructure Tracker (GGIT)](https://globalenergymonitor.org/projects/global-gas-infrastructure-tracker/), plus hydrogen pipelines. Each route is a standalone GeoJSON file keyed by its ProjectID in the tracker database; this repo is the source of truth for route geometry, and downstream maps are built from it automatically.

**If you're a researcher adding or updating routes**, you only need the first three sections. The rest documents automated checks and maintainer tooling.

## Repository layout

```
data/individual-routes/
├── gas-pipelines/        # one P####.geojson per gas pipeline project
├── liquid-pipelines/     # oil / NGL / products pipelines
└── hydrogen-pipelines/
drive-uploads/            # git-ignored local mirror of the shared Drive upload folder (see QC section)
scripts/                  # validation and QC tools (documented below)
```

Route files are named `[ProjectID].geojson` (e.g. `P1234.geojson`), and each ProjectID lives in exactly one fuel folder. Some projects also have a `[ProjectID]-compressor-stations.geojson` sidecar file holding point features for compressor stations.

## File conventions

**Every project gets a file, even without a route.** If a project has no route — it's a capacity expansion with no new pipeline, or no map exists to trace yet — we still create a `.geojson` for it with empty (`null`) geometry, like this (also at [`data/example-empty-route.geojson`](data/example-empty-route.geojson)):

```json
{
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": null
        }
    ]
}
```

**Don't repeat segment geometry on a system/network row.** When a pipeline's route is already drawn under its individual segment ProjectIDs, the parent `SYSTEM/NETWORK INFO` row keeps the empty file above — never a merge of its segments' geometry. (In the tracker that row reads `RouteType: Included in other ProjectID`, `RouteAccuracy: no route`.)

**Coordinates are WGS 84.** The [GeoJSON spec](https://geojson.org/) fixes the coordinate reference system as WGS 84 (EPSG:4326), so no `crs` member is needed — or allowed. Positions are `[longitude, latitude]` pairs only, no Z/elevation values.

**Only the filename identifies the route.** You don't need to include any pipeline attributes (name, status, etc.) in the file's properties — the `[ProjectID].geojson` filename is the only required label. Extra properties are fine but unnecessary.

## Contributing routes

1. Create a _new_ branch with a short, informative title (for example, `firstname-p9998-p9999`)
2. Add your changes to the branch and push it to the repository
3. Create a pull request and assign it to the repo maintainer for review

### Creating or editing a route file

* **Easiest:** draw the route in [geojson.io](https://geojson.io/) or [placemark.io](https://play.placemark.io/) and save it as `[ProjectID].geojson`.
* **Most capable:** [QGIS](https://www.qgis.org/en/site/) or [JOSM](https://josm.openstreetmap.de/), if you're comfortable with them — create or edit a route and export it as GeoJSON.
* **Editing an existing route:** import the project's current file from this repo into any of the tools above, edit, and re-export.

## Automated checks

Every pull request that changes route files is checked automatically by GitHub Actions (`.github/workflows/validate-routes.yml`). The checks fail the PR if a file:

* is not valid JSON / GeoJSON
* declares a coordinate reference system other than WGS 84 (EPSG:4326 / CRS84)
* contains Z coordinates (positions must be `[longitude, latitude]` only)
* has coordinates outside valid longitude/latitude ranges
* is not named `[ProjectID].geojson` (or `[ProjectID]-compressor-stations.geojson`)
* uses a ProjectID that already exists in a different fuel folder

You can run the same checks locally before pushing:

```
python3 scripts/validate_geojson.py path/to/P1234.geojson    # specific files
python3 scripts/validate_geojson.py --all                    # the whole repo
```

## Pre-commit QC of uploaded routes (`scripts/qc_routes.py`)

Before routes from an update cycle enter the repo, `scripts/qc_routes.py` runs a richer QC pass than the CI validator, comparing each route against the authoritative pipeline database (the tracker Google Sheet). It's a maintainer tool for triaging the geojson files researchers drop in the shared Drive folder, run in two phases.

A local, git-ignored mirror of that Drive folder lives at `drive-uploads/`. `scripts/sync_drive_uploads.sh` refreshes it with a one-way `rclone` sync (no Google Drive for Desktop needed); one-time rclone setup instructions are in the script's header. It's a true mirror — files trashed on Drive after QC disappear locally on the next sync — so QC can point straight at `drive-uploads/<researcher folder>`. After merging a route into the repo, trash the Drive original with `rclone deletefile gem-pipeline-uploads:<researcher>/P####.geojson` (goes to Drive's trash, recoverable for 30 days).

**1. Report (read-only) — scan a folder and see what's good:**

```
python3 scripts/qc_routes.py "/path/to/PIPELINE ROUTES .../<researcher folder>"
python3 scripts/qc_routes.py path/to/P1234.geojson                   # specific files
```

Each route is graded **PASS** / **WARN** / **FAIL**. On top of the CI checks (valid GeoJSON, WGS 84, no Z, coord ranges, filename) it verifies, against the DB:

* the ProjectID exists in the gas/oil tracker (and which fuel it is);
* both **endpoints fall in the DB Start/End country** (either orientation) — a country mismatch is a FAIL;
* **country coverage** — vertices straying outside the DB `CountriesOrAreas` (WARN);
* **route length** vs the DB length field (WARN beyond ±30%);
* **crude geometry** — long straight segments, reported as a quiet "fix eventually" note for `low`/`medium`/`no route` accuracy but a WARN when the DB says `high`;
* **possible duplicate** — geometry identical to another project's route (INFO when they're plausibly parallel routes, WARN otherwise);
* a soft **geocoding hint** — distance from the geocoded DB `StartLocation`/`EndLocation` place name to the matching endpoint (`--no-geocode` to skip).

**2. Copy (stages files into the repo) — only after you've read the report:**

```
python3 scripts/qc_routes.py "/path/.../<researcher folder>" --copy                       # copies PASS routes
python3 scripts/qc_routes.py "/path/.../<researcher folder>" --copy --include P1897 P3961  # also copy these WARN routes
```

The copy phase places routes in `data/individual-routes/<fuel>/` (fuel from the DB tab). **PASS** routes are copied automatically; **WARN** routes only when named with `--include`; **FAIL** routes are always left behind. Features whose geometry has an empty `coordinates` array (a stray empty feature next to the real trace, common in re-exports) are dropped on copy and reported as `(dropped N empty feature(s))`. Nothing is committed — review, then create a branch and PR as usual.

Reference data (DB tabs, Natural Earth country boundaries, geocode cache) is cached under `~/.cache/gem-route-qc/` and never touches the repo; pass `--refresh` to re-download.

## The `normalized` branch (generated — do not edit)

The `main` branch always holds the **original** files exactly as submitted — original metadata, original coordinate precision — so researchers who scrape routes get untouched data.

The [`normalized`](../../tree/normalized) branch holds a standardized copy of every route, rebuilt automatically by GitHub Actions (`.github/workflows/normalize-routes.yml`) whenever route files change on `main`. Normalized files are canonical FeatureCollections with coordinates rounded to 6 decimal places (~10 cm), no legacy `crs` member, and a `ProjectID` property on every feature. Never commit to that branch by hand — any change will be overwritten on the next rebuild.

Before rebuilding, the workflow runs `scripts/check_route_countries.py`: a bbox sanity check that fails if any feature of a route sits entirely outside (>3° from) every country the tracker DB lists for that project — the signature of a batch-export mixup (a route saved under the wrong ProjectID, or a stray copied feature; this is how P0530 shipped with P5259's geometry in a release). A failure blocks the `normalized` branch update until the file on `main` (or the DB countries) is corrected. Same-country contamination is out of its reach — that's covered by `qc_routes.py`'s duplicate-geometry and coverage checks at intake. It reuses the `~/.cache/gem-route-qc/` reference data and skips (loudly, exit 0) if the DB or boundaries can't be downloaded, so normalization never blocks on network availability.

After pushing a new `normalized` branch, the workflow fires a `repository_dispatch` (`routes-normalized`) to [goit-ggit-data-ops](https://github.com/GlobalEnergyMonitor/goit-ggit-data-ops), whose `build map data` workflow rebuilds the interim-map geojson and publishes it for [goit-ggit-interim-maps](https://github.com/GlobalEnergyMonitor/goit-ggit-interim-maps). The dispatch uses the `DATA_OPS_DISPATCH_TOKEN` repo secret (fine-grained PAT for goit-ggit-data-ops, Contents read/write) and is skipped quietly if the secret is unset.
