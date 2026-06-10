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
