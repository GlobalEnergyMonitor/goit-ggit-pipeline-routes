# GOIT-GGIT-pipeline-routes
This is a GitHub repository at Global Energy Monitor to store pipeline routes.

Individual routes are stored as `[ProjectID].geojson` in the `data/individual-routes` folder, split by fuel into `gas-pipelines`, `liquid-pipelines`, and `hydrogen-pipelines`. Each ProjectID lives in exactly one fuel folder.

## Every project has a GeoJSON file associated with it.
If a given project does not have a route, either because it's a capacity expansion with no actual new pipeline associated with it, or because we haven't created the route yet or cannot find a map to trace online, we STILL create a `.geojson` file for it, it's just stored as an **empty GeoJSON file** (i.e., `None`-type geometry).

An example of an "empty" GeoJSON file could look something like this:
```
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

## Contribute by creating a new branch and a pull request

If you update a route or multiple routes...
1. Create a _new_ branch with a short, informative title (for example, `firstname-p9998-p9999`)
2. Add your changes to the branch and push it to the repository
3. Create a pull request and assign it to Baird for review

## How can I create a GeoJSON file from scratch for a route?

* If you are comfortable working in [QGIS](https://www.qgis.org/en/site/) or [JOSM](https://josm.openstreetmap.de/), those are the most complex ways to do it. Create a route or edit an existing one and re-export it as a GeoJSON file. You __don't__ need to include any specific information about the pipeline itself (name, status, etc.) in the GeoJSON file; the __only__ way I ask you to label it is via the title: `[ProjectID].geojson`. (You can of course include more info, but it's not necessary.)

* If you're creating a new route from scratch, and the tools above aren't familiar, try using [geojson.io](https://geojson.io/) or [placemark.io](https://play.placemark.io/).

* If you're editing an existing route, you can import the GeoJSON file that already exists for it

## Coordinate reference system

The [GeoJSON](https://geojson.org/) file format specification says that GeoJSON files use a WSG 84 (EPSG:4326) coordinate reference system, so this is expected for all pipelines and no crs is required in the GeoJSON file.

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

## The `normalized` branch (generated — do not edit)

The `main` branch always holds the **original** files exactly as submitted — original metadata, original coordinate precision — so researchers who scrape routes get untouched data.

The [`normalized`](../../tree/normalized) branch holds a standardized copy of every route, rebuilt automatically by GitHub Actions (`.github/workflows/normalize-routes.yml`) whenever route files change on `main`. Normalized files are canonical FeatureCollections with coordinates rounded to 6 decimal places (~10 cm), no legacy `crs` member, and a `ProjectID` property on every feature. Never commit to that branch by hand — any change will be overwritten on the next rebuild.
