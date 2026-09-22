---
name: holds-report
description: Write a shareable map-by-map report explaining to a researcher why some of their Drive route uploads were held (not merged), and what would fix each. Use after triaging a researcher's held uploads, when the user asks for a report/explanation to send them.
---

# Held-uploads report for a researcher

Produces one HTML page per researcher batch with, for every held ProjectID, a
map (repo route in blue, upload in orange, tracker towns, optional context
routes), the tracker facts, a computed points / length / closest-approach
table, a plain-language "what we saw", and a "next step". Maps and numbers come
from `scripts/holds_report.py`; you write only the prose, in a notes file.

## Before you start

- The routes are already triaged (QC report read, each hold judged) and the
  accepts are merged. The report covers only what was *not* merged.
- `scripts/sync_drive_uploads.sh` has run, so `drive-uploads/<folder>/` holds the
  researcher's files. Don't trash or edit held originals on Drive.
- Notes and output contain the researcher's name and are never committed (this
  repo is public). Keep both under `ignore/holds-reports/` (git-ignored).

## Steps

1. **Draft the notes file** at `ignore/holds-reports/<researcher>-<yyyy-mm>.json`
   from the template below: `researcher`, `upload_dir`, and one entry per held
   ProjectID with just `pid` (plus `context` if neighbouring routes explain the
   problem).
2. **Get the evidence:** `python3 scripts/holds_report.py <notes> --evidence-only`.
   It prints the tracker facts and, for the repo route and the upload: parts,
   points, length, closest approach to each town, share over water, vertex
   countries, closed loops, upload parts identical to the repo route, and
   upload endpoints that meet another route's endpoint.
3. **Check every town location.** Lines marked `Nominatim '...'` are unchecked
   geocodes. Nominatim picks the wrong same-named town surprisingly often
   (Mallavaram, Vijaipur and Jafrabad all collided in India). Confirm each one
   with a state/district-qualified query, then pin the coordinates in
   `places`. Never hand-type coordinates from memory. Re-run step 2 after
   pinning.
4. **Group and write the prose.** Put each route in a group (`retrace`, `kept`,
   `split`, or custom `groups`). Write `saw` (what the map shows and why it
   wasn't merged) and `ask` (the concrete fix or question). Rules:
   - Every number and factual claim must appear in the evidence output or the
     tracker row. If you can't verify it, cut it; don't soften it.
   - Judge proximity by the line's closest approach (as printed), never by
     first/last endpoints alone. Multi-part routes mislead the endpoint view.
   - Address the researcher directly and constructively: what's wrong, then
     what would fix it. Name ProjectIDs in `<b>`. Leave out internal repo
     hygiene and tracker-cell edits.
   - Optional page fields: `title` (default "<Name>'s Held Routes"),
     `heading`, `eyebrow`, `intro`, `meta` (inline HTML allowed). Mention the
     routes that *were* merged in `intro`, and that the files are still in
     Drive in `meta`.
5. **Build and look once:** run without `--evidence-only`. The HTML lands next
   to the notes file. Take one headless screenshot and fix only what it shows:
   `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars --window-size=1200,3400 --screenshot=$PWD/ignore/holds-reports/shot.png file://$PWD/<html>`
6. **Publish** the HTML with the Artifact tool (favicon 🗺️, icon `map`, a
   one-sentence description), then give the user the link. Artifacts publish
   under whichever claude.ai account the session is using. Say which one,
   because a work colleague is the audience. It stays private until the user
   shares it.

## Notes template

```json
{
  "researcher": "<first name>",
  "upload_dir": "drive-uploads/<researcher folder>",
  "eyebrow": "Gas pipeline routes · <country> · Drive uploads",
  "intro": "<Name> — <n> of the <m> routes held back ... went into the repo on <date>: <b>P….</b> Thanks for those. The other <k> are below, each with a map, what went wrong, and what would fix it.",
  "meta": "All <k> files are still in your Drive folder. Nothing has been deleted. Upload a replacement with the same filename and it will be picked up in the next check.",
  "places": {"<Town>": [<lon>, <lat>]},
  "routes": [
    {"pid": "P0000", "group": "retrace", "places": ["<Town>"], "context": ["P1111"],
     "saw": "…", "ask": "…"}
  ]
}
```

A route's `places` defaults to the tracker's Start/EndLocation (offshore
skipped). Set it explicitly to add towns from the pipeline name, fix tracker
misspellings, or pass `[]` for none. `upload` overrides the file path for one
route.
