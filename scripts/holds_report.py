#!/usr/bin/env python3
"""Build a shareable HTML report explaining held (not merged) route uploads.

When triage decides some of a researcher's Drive uploads shouldn't replace the
route in the repo, this renders one page for the batch: for each ProjectID a
map (repo route vs upload, tracker towns, optional context routes), the tracker
facts, a computed points / length / closest-approach table, and the
maintainer's explanation and next step. Maps and numbers are computed here;
only the prose comes from the notes file.

    python3 scripts/holds_report.py ignore/holds-reports/<name>.json --evidence-only
    python3 scripts/holds_report.py ignore/holds-reports/<name>.json [--out PATH]

`--evidence-only` prints what the script can verify (lengths, closest approach
to each town, share over water, vertex countries, closed loops, parts identical
to the repo route, endpoints shared with other routes) -- draft the prose from
that. The notes file holds researcher names and draft wording, so keep it out
of git: `ignore/` is git-ignored, and the HTML lands next to the notes file.
Workflow, template and tone guidance: .claude/skills/holds-report/SKILL.md.
"""
import argparse
import csv
import html
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qc_routes import (CACHE, DB_TABS, HEADER_ROW, REPO_ROOT, ROUTES_DIR,  # noqa: E402
                       Countries, Geocoder, geodesic_km, line_coords, load_db,
                       read_geojson)

E = html.escape
W, H = 480, 340                  # map viewBox
NEAR_KM = 0.05                   # endpoints this close count as the same point
NICE_KM = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
NUMBER_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()
DEFAULT_GROUPS = [
    {"key": "retrace", "title": "Retrace needed", "chip": "Retrace",
     "blurb": "Both the upload and what it would replace have problems. A new trace is the fix."},
    {"key": "kept", "title": "Existing route kept", "chip": "Kept existing",
     "blurb": "The route in the repo is closer to the tracker than the upload. "
              "Nothing is needed unless the upload was meant as something else."},
    {"key": "split", "title": "Split or source needed", "chip": "Split / source",
     "blurb": "The upload may be right, but it doesn’t match how the tracker defines the project."},
]


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_rows(refresh=False):
    """{ProjectID: full tracker row} across the DB tabs (cached by qc_routes)."""
    load_db(refresh)                      # fetches/caches every tab via gws
    rows = {}
    for fuel, title in DB_TABS.items():
        grid = list(csv.reader((CACHE / f"db_{fuel}.csv").open(encoding="utf-8")))
        hdr = grid[HEADER_ROW]
        for r in grid[HEADER_ROW + 1:]:
            rec = {h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(hdr) if h}
            if rec.get("ProjectID"):
                rows[rec["ProjectID"]] = {**rec, "_fuel": fuel, "_tab": title}
    return rows


def parts_of(path):
    if not path or not Path(path).exists():
        return []
    return [[(float(c[0]), float(c[1])) for c in ln] for ln in line_coords(read_geojson(path))]


def repo_endpoints():
    """[(ProjectID, (lon, lat))] for both ends of every line in the repo."""
    out = []
    for f in ROUTES_DIR.glob("*/*.geojson"):
        pid = f.stem.split("-")[0]
        try:
            lines = line_coords(read_geojson(f))
        except Exception:
            continue
        for ln in lines:
            out += [(pid, tuple(ln[0][:2])), (pid, tuple(ln[-1][:2]))]
    return out


def resolve_places(route, row, pinned, geo):
    """[(name, (lon, lat), source)] -- notes `places` win over Nominatim."""
    names = route.get("places")
    if names is None:
        names = [n for n in (row.get("StartLocation"), row.get("EndLocation"))
                 if n and not n.lower().startswith("offshore")]
    out = []
    for n in dict.fromkeys(names):
        if n in pinned:
            out.append((n, tuple(pinned[n]), "notes"))
            continue
        state = row.get("EndState/Province") if n == row.get("EndLocation") else row.get("StartState/Province")
        country = (row.get("CountriesOrAreas") or "").split(",")[0].strip()
        q = ", ".join(x for x in (n, state, country) if x and not x.lower().startswith("offshore"))
        ll = geo.geocode(q)
        if ll is None:
            print(f"  ! could not geocode {q!r}; pin it in notes.places", file=sys.stderr)
            continue
        out.append((n, tuple(ll), f"Nominatim {q!r}"))
    return out


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
def hav(a, b):
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def densify(parts, step=0.5):
    out = []
    for p in parts:
        for a, b in zip(p, p[1:]):
            n = max(1, int(hav(a, b) / step))
            out += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n)]
        out.append(tuple(p[-1]))
    return out


def closest_km(parts, pt):
    """Closest approach of the drawn line to a point (not just its endpoints)."""
    d = densify(parts)
    return min(hav(q, pt) for q in d) if d else None


def country_at(C, pt):
    P = Point(pt)
    for i in C.tree.query(P, predicate="intersects"):
        if C.geoms[i].covers(P):
            return C.names[i]
    return None


def stats(parts, places):
    if not parts:
        return None
    return {"parts": len(parts), "pts": sum(map(len, parts)),
            "km": sum(geodesic_km(p) for p in parts),
            "part_km": [geodesic_km(p) for p in parts],
            "near": [closest_km(parts, ll) for _, ll, _ in places]}


def evidence(pid, repo, up, places, C, endpoints):
    """Plain-text facts the prose may lean on."""
    out = [f"  place {n}: {ll[0]:.4f}, {ll[1]:.4f} ({src})" for n, ll, src in places]
    for label, parts in (("repo", repo), ("upload", up)):
        s = stats(parts, places)
        if not s:
            out.append(f"  {label}: no geometry")
            continue
        d = densify(parts)
        water = sum(1 for q in d if country_at(C, q) is None) / len(d)
        verts = Counter(country_at(C, v) or "sea" for p in parts for v in p)
        out.append(f"  {label}: {s['parts']} part(s), {s['pts']} pts, {s['km']:,.1f} km "
                   f"[{', '.join(f'{k:,.1f}' for k in s['part_km'])}], {100 * water:.0f}% over water, "
                   f"vertices {dict(verts)}")
        for (n, _, _), km in zip(places, s["near"]):
            out.append(f"    closest to {n}: {km:,.1f} km")
        for i, p in enumerate(parts, 1):
            if len(p) > 2 and hav(p[0], p[-1]) < NEAR_KM:
                out.append(f"    part {i} is a closed loop ({s['part_km'][i - 1]:,.1f} km)")
    key = lambda p: tuple((round(x, 5), round(y, 5)) for x, y in p)  # noqa: E731
    repo_keys = {key(p) for p in repo} | {key(p)[::-1] for p in repo}
    for i, p in enumerate(up, 1):
        if key(p) in repo_keys:
            out.append(f"  upload part {i} is identical to a part of the repo route")
        for end in (p[0], p[-1]):
            hits = sorted({o for o, e in endpoints if o != pid and hav(e, end) < NEAR_KM})
            if hits:
                out.append(f"  upload part {i} endpoint {end[0]:.5f},{end[1]:.5f} "
                           f"meets an endpoint of {', '.join(hits)}")
    return out


# --------------------------------------------------------------------------- #
# map
# --------------------------------------------------------------------------- #
def build_map(repo, up, places, context, C, primary):
    """Equirectangular (cos-lat scaled) SVG data in a W x H viewBox."""
    pts = [v for p in repo + up for v in p] + [ll for _, ll, _ in places]
    pts += [v for parts in context.values() for p in parts for v in p]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    k = math.cos(math.radians(cy))
    sx, sy = max((x1 - x0) * k, 0.25) * 1.3, max(y1 - y0, 0.25) * 1.3
    if sx / sy < W / H:
        sx = sy * W / H
    else:
        sy = sx * H / W
    bx0, bx1, by0, by1 = cx - sx / k / 2, cx + sx / k / 2, cy - sy / 2, cy + sy / 2
    X = lambda lon: (lon - bx0) / (bx1 - bx0) * W  # noqa: E731
    Y = lambda lat: (by1 - lat) / (by1 - by0) * H  # noqa: E731

    def ring(r):
        return "M" + "L".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in r.coords) + "Z"

    B, tol, land = box(bx0, by0, bx1, by1), (bx1 - bx0) / 700, []
    for i in C.tree.query(B):
        g = C.geoms[i]
        if not g.intersects(B):
            continue
        cg = g.intersection(B).simplify(tol)
        polys = [cg] if cg.geom_type == "Polygon" else [q for q in getattr(cg, "geoms", []) if q.geom_type == "Polygon"]
        d = "".join(ring(q.exterior) + "".join(ring(r) for r in q.interiors) for q in polys)
        if d:
            c = cg.representative_point()
            land.append({"name": C.names[i], "d": d, "area": cg.area, "lx": X(c.x), "ly": Y(c.y),
                         "primary": int(i) == primary})

    def line(parts):
        return "".join("M" + "L".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in p) for p in parts)

    def verts(parts):
        return [(round(X(a), 1), round(Y(b), 1)) for p in parts for a, b in p]

    kmpp = (bx1 - bx0) * 111.32 * k / W
    sb = min(NICE_KM, key=lambda n: abs(n / kmpp - W * 0.22))
    st = next(s for s in (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20) if (bx1 - bx0) / s <= 6)
    return {"land": land, "repo": line(repo), "upload": line(up),
            "repo_v": verts(repo), "upload_v": verts(up),
            "places": [{"name": n, "x": round(X(ll[0]), 1), "y": round(Y(ll[1]), 1)} for n, ll, _ in places],
            "ref": [{"id": c, "d": line(p)} for c, p in context.items() if p],
            "scale": {"km": sb, "px": round(sb / kmpp, 1)},
            "glon": [{"v": round(i * st, 3), "x": round(X(i * st), 1)}
                     for i in range(math.ceil(bx0 / st), math.floor(bx1 / st) + 1)],
            "glat": [{"v": round(i * st, 3), "y": round(Y(i * st), 1)}
                     for i in range(math.ceil(by0 / st), math.floor(by1 / st) + 1)]}


def fmt_lon(v):
    return f"{abs(v):g}°{'E' if v >= 0 else 'W'}"


def fmt_lat(v):
    return f"{abs(v):g}°{'N' if v >= 0 else 'S'}"


def svg(pid, m):
    o = [f'<svg class="map" viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="Map of {pid}: route in repo in blue, upload in orange">',
         f'<rect class="sea" x="0" y="0" width="{W}" height="{H}"/>']
    for ld in sorted(m["land"], key=lambda ld: -ld["area"]):
        o.append(f'<path class="{"land" if ld["primary"] else "land alt"}" d="{ld["d"]}"/>')
    for g in m["glon"]:
        if 8 < g["x"] < W - 44:
            o.append(f'<line class="grat" x1="{g["x"]}" y1="0" x2="{g["x"]}" y2="{H}"/>'
                     f'<text class="tick" x="{g["x"] + 3}" y="11">{fmt_lon(g["v"])}</text>')
    for g in m["glat"]:
        if 14 < g["y"] < H - 8:
            o.append(f'<line class="grat" x1="0" y1="{g["y"]}" x2="{W}" y2="{g["y"]}"/>'
                     f'<text class="tick" x="4" y="{g["y"] - 3}">{fmt_lat(g["v"])}</text>')
    if len(m["land"]) > 1:
        for ld in m["land"]:
            if ld["area"] > 0.02 and 30 < ld["lx"] < W - 30 and 20 < ld["ly"] < H - 20:
                o.append(f'<text class="country" x="{ld["lx"]:.0f}" y="{ld["ly"]:.0f}" '
                         f'text-anchor="middle">{E(ld["name"].upper())}</text>')
    for r in m["ref"]:
        x, y = r["d"][1:].split("L")[0].split(",")
        o.append(f'<path class="ref" d="{r["d"]}"/>'
                 f'<text class="reflabel" x="{float(x) + 5:.0f}" y="{float(y) - 5:.0f}">{r["id"]}</text>')
    o.append(f'<path class="repo" d="{m["repo"]}"/>')
    o += [f'<circle class="repo-v" cx="{x}" cy="{y}" r="1.9"/>' for x, y in m["repo_v"]]
    o.append(f'<path class="upload" d="{m["upload"]}"/>')
    o += [f'<circle class="upload-v" cx="{x}" cy="{y}" r="2.4"/>' for x, y in m["upload_v"]]
    for p in m["places"]:
        right = p["x"] < W - 90
        o.append(f'<circle class="place" cx="{p["x"]}" cy="{p["y"]}" r="4"/>'
                 f'<text class="plabel" x="{p["x"] + (7 if right else -7)}" y="{p["y"] + 4}" '
                 f'text-anchor="{"start" if right else "end"}">{E(p["name"])}</text>')
    s, x0, y0 = m["scale"], 14, H - 16
    o.append(f'<g class="scale"><rect class="scalebg" x="{x0 - 6}" y="{y0 - 17}" width="{s["px"] + 44}" height="25" rx="2"/>'
             f'<line x1="{x0}" y1="{y0}" x2="{x0 + s["px"]}" y2="{y0}"/>'
             f'<line x1="{x0}" y1="{y0 - 4}" x2="{x0}" y2="{y0 + 3}"/>'
             f'<line x1="{x0 + s["px"]}" y1="{y0 - 4}" x2="{x0 + s["px"]}" y2="{y0 + 3}"/>'
             f'<text x="{x0}" y="{y0 - 7}">0</text>'
             f'<text x="{x0 + s["px"]}" y="{y0 - 7}" text-anchor="middle">{s["km"]} km</text></g>')
    o.append(f'<rect class="frame" x="0.5" y="0.5" width="{W - 1}" height="{H - 1}"/></svg>')
    return "".join(o)


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #
def pretty_name(name):
    return re.sub(r"(?<=[A-Za-z)])-(?=[A-Z])", "–", name)


def facts(row):
    try:
        length = f"{float(row.get('LengthKnownKm') or row.get('LengthMergedKm')):,.0f} km"
    except ValueError:
        length = "not listed"
    s, e = row.get("StartLocation", ""), row.get("EndLocation", "")
    states = [x for x in (row.get("StartState/Province", ""), row.get("EndState/Province", ""))
              if x and not x.lower().startswith("offshore")]
    where = " → ".join(dict.fromkeys(states)) or row.get("CountriesOrAreas", "")
    return [("Tracker length", length),
            ("Start → end", f"{s or '—'} → {e or '—'}" if (s or e) else "not listed"),
            ("Region", " · ".join(x for x in (where, row.get("Status", "")) if x))]


def km_cell(v):
    return "—" if v is None else f"{v:,.0f} km"


def route_block(b):
    row, r = b["row"], b["route"]
    seg = f' <span class="seg">{E(row["SegmentName"])}</span>' if row.get("SegmentName") else ""
    fx = "".join(f"<div><dt>{E(k)}</dt><dd>{E(v)}</dd></div>" for k, v in facts(row))
    ph = "".join(f'<th scope="col">to {E(n)}</th>' for n, _, _ in b["places"])
    body = ""
    for label, cls, s in (("In repo", "r-repo", b["repo_s"]), ("Your upload", "r-up", b["up_s"])):
        if s is None:
            cells = '<td>—</td><td>no route</td>' + "<td>—</td>" * len(b["places"])
        else:
            cells = f'<td>{s["pts"]:,}</td><td>{km_cell(s["km"])}</td>' + "".join(f"<td>{km_cell(d)}</td>" for d in s["near"])
        body += f'<tr class="{cls}"><th scope="row"><span class="sw"></span>{label}</th>{cells}</tr>'
    note = '<p class="tnote">Distances are the closest the line gets to each town.</p>' if b["places"] else ""
    return f'''
<article class="route" id="{b["pid"]}">
  <header class="rhead"><span class="pid">{b["pid"]}</span><h3>{E(pretty_name(row["PipelineName"]))}{seg}</h3></header>
  <div class="rbody">
    <figure>{svg(b["pid"], b["map"])}</figure>
    <div class="rtext">
      <dl class="facts">{fx}</dl>
      <div class="tablewrap"><table class="cmp">
        <thead><tr><th scope="col"></th><th scope="col">Points</th><th scope="col">Length</th>{ph}</tr></thead>
        <tbody>{body}</tbody>
      </table></div>
      {note}
      <p class="saw">{r["saw"]}</p>
      <div class="ask"><span class="asklabel">Next step</span><p>{r["ask"]}</p></div>
    </div>
  </div>
</article>'''


def lg_line(tok, dash=False):
    w = 1.6 if tok == "ref" else 2.6
    return (f'<svg width="34" height="10" aria-hidden="true"><line x1="2" y1="5" x2="32" y2="5" '
            f'style="stroke:var(--{tok});stroke-width:{w};stroke-linecap:round{";stroke-dasharray:4 3" if dash else ""}"/></svg>')


def render(N, built, groups, db_date, tab):
    chips = {g["key"]: g.get("chip", g["title"]) for g in groups}
    idx = "".join(
        f'<li><a href="#{b["pid"]}"><span class="pid">{b["pid"]}</span><span class="iname">'
        f'{E(pretty_name(b["row"]["PipelineName"]))}{(" · " + E(b["row"]["SegmentName"])) if b["row"].get("SegmentName") else ""}'
        f'</span><span class="chip c-{b["route"]["group"]}">{E(chips[b["route"]["group"]])}</span></a></li>'
        for g in groups for b in built if b["route"]["group"] == g["key"])
    sections = ""
    for g in groups:
        bs = [b for b in built if b["route"]["group"] == g["key"]]
        if bs:
            sections += (f'<section class="group"><div class="ghead"><h2>{E(g["title"])} <span class="count">{len(bs)}</span></h2>'
                         f'<p>{E(g["blurb"])}</p></div>' + "".join(route_block(b) for b in bs) + "</section>")
    n = len(built)
    heading = N.get("heading") or f"{(NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)).capitalize()} routes to revisit"
    ctx = any(b["map"]["ref"] for b in built)
    legend = (f'<div class="lg">{lg_line("repo")}<span>Route in the repo now</span></div>'
              f'<div class="lg">{lg_line("upload")}<span>Your upload</span></div>'
              '<div class="lg"><svg width="34" height="12" aria-hidden="true"><circle cx="17" cy="6" r="4" '
              'style="fill:var(--halo);stroke:var(--ink);stroke-width:1.6"/></svg><span>Town named in the tracker or pipeline name</span></div>'
              + (f'<div class="lg">{lg_line("ref", True)}<span>Other pipelines, for context</span></div>' if ctx else ""))
    return f'''<title>{E(N.get("title") or N["researcher"] + "'s Held Routes")}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<div class="intro">
  <div>
    <p class="eyebrow">{E(N.get("eyebrow") or "Pipeline routes · Drive uploads")}</p>
    <h1>{E(heading)}</h1>
    <p class="lede">{N.get("intro", "")}</p>
    <p class="meta">{N.get("meta", "")}</p>
  </div>
  <aside class="legend" aria-label="Map key"><h2>Reading the maps</h2>{legend}</aside>
</div>
<ul class="index">{idx}</ul>
{sections}
<footer>Tracker values are from the {E(tab)} tab as of {db_date.day} {db_date:%B %Y}. Town locations are from
OpenStreetMap; distances are the closest the line comes to the town. Country outlines are from Natural Earth (1:10m).</footer>
</div>'''


CSS = r'''
:root{
  --paper:#F3F5F4; --surface:#FFFFFF; --ink:#172329; --muted:#56666C; --rule:#CCD5D8;
  --sea:#DCE7EC; --land:#FBFBF7; --land-alt:#ECEEE6; --coast:#9FB1B8; --grat:#C3D2D8;
  --repo:#1D5C96; --upload:#D2561B; --ref:#7D8A8F;
  --ask-bg:#E6EEF1; --chip-retrace:#F6DDD0; --chip-retrace-ink:#8A3310;
  --chip-kept:#D6E4F0; --chip-kept-ink:#18466F; --chip-split:#E7E4D4; --chip-split-ink:#5B5323;
  --halo:#FBFBF7;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --paper:#101719; --surface:#161F22; --ink:#E1E7E9; --muted:#95A5AA; --rule:#2A363A;
  --sea:#14222A; --land:#1E282C; --land-alt:#1A2225; --coast:#3C4E55; --grat:#22333B;
  --repo:#6AA7E0; --upload:#F08A4B; --ref:#7F8D92;
  --ask-bg:#1B2A30; --chip-retrace:#3A2419; --chip-retrace-ink:#F4B08A;
  --chip-kept:#1B2E40; --chip-kept-ink:#A9CBEA; --chip-split:#2D2B1E; --chip-split-ink:#D9CF97;
  --halo:#1E282C;
}}
:root[data-theme="dark"]{
  --paper:#101719; --surface:#161F22; --ink:#E1E7E9; --muted:#95A5AA; --rule:#2A363A;
  --sea:#14222A; --land:#1E282C; --land-alt:#1A2225; --coast:#3C4E55; --grat:#22333B;
  --repo:#6AA7E0; --upload:#F08A4B; --ref:#7F8D92;
  --ask-bg:#1B2A30; --chip-retrace:#3A2419; --chip-retrace-ink:#F4B08A;
  --chip-kept:#1B2E40; --chip-kept-ink:#A9CBEA; --chip-split:#2D2B1E; --chip-split-ink:#D9CF97;
  --halo:#1E282C;
}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font:400 15px/1.6 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;padding-inline:20px;padding-block:40px 72px}
.wrap{max-width:1000px;margin:0 auto}
a{color:inherit}
:focus-visible{outline:2px solid var(--repo);outline-offset:2px}
.eyebrow{font:500 12px/1.4 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
h1{font:600 clamp(34px,6vw,50px)/1.02 "Barlow Condensed","Arial Narrow",sans-serif;letter-spacing:.005em;margin:0 0 18px;text-wrap:balance}
.lede{max-width:64ch;margin:0 0 12px;font-size:16px}
.lede b{font-weight:600}
.meta{max-width:64ch;color:var(--muted);font-size:13.5px;margin:0}
.intro{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,340px);gap:36px;align-items:start;padding-bottom:32px;border-bottom:1px solid var(--rule)}
.legend{background:var(--surface);border:1px solid var(--rule);padding:16px 18px;display:grid;gap:10px;font-size:13.5px}
.legend h2{font:600 13px/1.2 "IBM Plex Mono",monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 2px}
.lg{display:flex;gap:10px;align-items:center}
.lg svg{flex:none}
.index{list-style:none;margin:28px 0 0;padding:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 28px}
.index li{border-bottom:1px solid var(--rule)}
.index a{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:center;padding:9px 2px;text-decoration:none}
.index a:hover .iname{text-decoration:underline;text-underline-offset:3px}
.iname{font-size:14px;overflow-wrap:anywhere}
.pid{font:500 13px/1 "IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums;padding:5px 7px;border:1px solid var(--ink);border-radius:2px;white-space:nowrap}
.chip{font:500 11px/1 "IBM Plex Mono",monospace;letter-spacing:.04em;text-transform:uppercase;padding:5px 7px;border-radius:2px;white-space:nowrap;background:var(--chip-split);color:var(--chip-split-ink)}
.c-retrace{background:var(--chip-retrace);color:var(--chip-retrace-ink)}
.c-kept{background:var(--chip-kept);color:var(--chip-kept-ink)}
.group{padding-top:48px}
.ghead{display:grid;gap:4px;margin-bottom:8px}
.ghead h2{font:600 30px/1.1 "Barlow Condensed","Arial Narrow",sans-serif;letter-spacing:.01em;margin:0;display:flex;gap:12px;align-items:baseline}
.count{font:500 14px/1 "IBM Plex Mono",monospace;color:var(--muted)}
.ghead p{margin:0;color:var(--muted);max-width:70ch}
.route{padding-block:28px;border-bottom:1px solid var(--rule)}
.group .route:last-child{border-bottom:0}
.rhead{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.rhead h3{font:600 20px/1.3 "IBM Plex Sans",sans-serif;margin:0;text-wrap:balance}
.seg{font-weight:400;color:var(--muted)}
.rbody{display:grid;grid-template-columns:minmax(0,480px) minmax(0,1fr);gap:28px;align-items:start}
figure{margin:0}
.map{display:block;width:100%;height:auto;max-width:100%}
.sea{fill:var(--sea)}
.land{fill:var(--land);stroke:var(--coast);stroke-width:.7;stroke-linejoin:round}
.land.alt{fill:var(--land-alt)}
.grat{stroke:var(--grat);stroke-width:.6;fill:none}
.tick{font:400 9px "IBM Plex Mono",monospace;fill:var(--muted)}
.country{font:500 10px "IBM Plex Mono",monospace;letter-spacing:.14em;fill:var(--muted)}
.ref{fill:none;stroke:var(--ref);stroke-width:1.6;stroke-dasharray:4 3}
.reflabel{font:500 10px "IBM Plex Mono",monospace;fill:var(--ref);paint-order:stroke;stroke:var(--halo);stroke-width:3px}
.repo{fill:none;stroke:var(--repo);stroke-width:2.4;stroke-linejoin:round;stroke-linecap:round}
.repo-v{fill:var(--repo)}
.upload{fill:none;stroke:var(--upload);stroke-width:2.6;stroke-linejoin:round;stroke-linecap:round}
.upload-v{fill:var(--upload);stroke:var(--halo);stroke-width:1}
.place{fill:var(--halo);stroke:var(--ink);stroke-width:1.6}
.plabel{font:600 11.5px "IBM Plex Sans",sans-serif;fill:var(--ink);paint-order:stroke;stroke:var(--halo);stroke-width:3.5px;stroke-linejoin:round}
.scale line{stroke:var(--ink);stroke-width:1.4}
.scale text{font:500 9.5px "IBM Plex Mono",monospace;fill:var(--ink)}
.scalebg{fill:var(--halo);opacity:.85}
.frame{fill:none;stroke:var(--rule)}
.rtext{display:grid;gap:14px;min-width:0}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 16px;margin:0}
.facts div{display:grid;gap:2px}
.facts dt{font:500 10.5px/1.3 "IBM Plex Mono",monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.facts dd{margin:0;font-size:14px;font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto}
.cmp{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
.cmp th,.cmp td{padding:6px 10px 6px 0;text-align:right;white-space:nowrap;border-bottom:1px solid var(--rule)}
.cmp thead th{font:500 10.5px/1.3 "IBM Plex Mono",monospace;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.cmp th[scope=row],.cmp thead th:first-child{text-align:left;font-weight:500}
.sw{display:inline-block;width:18px;height:3px;border-radius:2px;vertical-align:middle;margin-right:8px}
.r-repo .sw{background:var(--repo)} .r-up .sw{background:var(--upload)}
.tnote{margin:-6px 0 0;font-size:12.5px;color:var(--muted)}
.saw{margin:0;max-width:62ch}
.saw b{font-weight:600}
.ask{background:var(--ask-bg);padding:12px 16px 13px;border-radius:2px;max-width:62ch}
.asklabel{display:block;font:500 10.5px/1.3 "IBM Plex Mono",monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.ask p{margin:0}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--rule);color:var(--muted);font-size:13px;max-width:78ch}
@media (max-width:860px){
  .intro{grid-template-columns:1fr;gap:22px}
  .rbody{grid-template-columns:1fr}
  .index{grid-template-columns:1fr}
}
@media (max-width:480px){
  .facts{grid-template-columns:1fr 1fr}
  .index a{grid-template-columns:auto minmax(0,1fr)}
  .index .chip{grid-column:2;justify-self:start}
}
'''


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Report on held route uploads for a researcher.")
    ap.add_argument("notes", help="notes JSON (keep under ignore/ -- it holds names)")
    ap.add_argument("--out", help="HTML path (default: next to the notes file)")
    ap.add_argument("--evidence-only", action="store_true",
                    help="print the computed evidence and stop (use while drafting)")
    ap.add_argument("--refresh", action="store_true", help="re-download the tracker tabs")
    a = ap.parse_args(argv)

    notes_path = Path(a.notes)
    N = json.loads(notes_path.read_text(encoding="utf-8"))
    groups = N.get("groups") or DEFAULT_GROUPS
    gkeys = {g["key"] for g in groups}
    rows = load_rows(a.refresh)
    C, geo, endpoints = Countries(), Geocoder(), repo_endpoints()
    upload_dir = REPO_ROOT / N["upload_dir"]

    built = []
    for r in N["routes"]:
        pid = r["pid"]
        if pid not in rows:
            sys.exit(f"{pid}: not in the tracker")
        if not a.evidence_only and (r.get("group") not in gkeys or not r.get("saw") or not r.get("ask")):
            sys.exit(f"{pid}: needs a group from {sorted(gkeys)} plus 'saw' and 'ask' text")
        row = rows[pid]
        up_path = REPO_ROOT / r["upload"] if r.get("upload") else upload_dir / f"{pid}.geojson"
        if not up_path.exists():
            sys.exit(f"{pid}: upload not found at {up_path}")
        repo, up = parts_of(ROUTES_DIR / row["_fuel"] / f"{pid}.geojson"), parts_of(up_path)
        places = resolve_places(r, row, N.get("places", {}), geo)
        context = {c: parts_of(ROUTES_DIR / rows[c]["_fuel"] / f"{c}.geojson")
                   for c in r.get("context", []) if c in rows}
        print(f"{pid} {row['PipelineName']} {row.get('SegmentName', '')}".rstrip())
        print("  tracker: " + "; ".join(f"{k} {v}" for k, v in facts(row)))
        print("\n".join(evidence(pid, repo, up, places, C, endpoints)))
        built.append({"pid": pid, "row": row, "route": r, "places": places,
                      "repo_s": stats(repo, places), "up_s": stats(up, places),
                      "map": build_map(repo, up, places, context, C,
                                       C.resolve(row.get("StartCountryOrArea") or row.get("CountriesOrAreas", "")))})
    geo.save()
    if a.evidence_only:
        return

    db_file = CACHE / f"db_{built[0]['row']['_fuel']}.csv"
    page = render(N, built, groups, datetime.fromtimestamp(db_file.stat().st_mtime), built[0]["row"]["_tab"])
    out = Path(a.out) if a.out else notes_path.with_suffix(".html")
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
