#!/usr/bin/env python3
"""
PHASE 1 — Overhead (satellite) screen.

Walk the whole feeder (trunk + every lateral offshoot) plus every takeoff
junction, pull a top-down Google satellite tile at each sample point, and have
Claude score how much tree canopy is over/into the line corridor. This is the
first pass: it covers 100% of the route (including off-road spans Street View
can't see) and produces a ranked list of candidate trouble spots.

    export GOOGLE_MAPS_API_KEY=...      # Maps Static API must be enabled
    export ANTHROPIC_API_KEY=...
    python phase1_overhead_scan.py "Mescalero_West_Phase_A_Overlay.kmz"

Output (in out/):
  overhead/<id>.jpg            satellite tiles
  overhead_candidates.csv      every point, scored, sorted worst-first
  overhead_candidates.kml      score>=3 points, openable in Google Earth
"""
import argparse
import csv
import os

import gmaps
import vision

OUT = "out"
TILES = os.path.join(OUT, "overhead")
INTERVAL_M = 35          # satellite sample spacing along every line
ZOOM = 20                # ~ tight enough to judge canopy over the corridor


def kml_path_from_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("kmz")
    ap.add_argument("--limit", type=int, default=0, help="cap points (testing)")
    ap.add_argument("--min-score", type=int, default=3)
    return ap.parse_args()


def main():
    args = kml_path_from_args()
    gmaps.key()
    os.makedirs(TILES, exist_ok=True)

    kml = gmaps.load_kml(args.kmz)
    lines = gmaps.parse_lines(kml)
    takeoffs = gmaps.parse_named_points(kml)
    print(f"{len(lines)} segments (~{gmaps.total_miles(lines):.1f} mi), "
          f"{len(takeoffs)} junctions.")

    # Build the point list: junctions first (highest interest), then line samples.
    points = []  # (id, label, lat, lng)
    for nm, lat, lng in takeoffs:
        points.append((f"J_{len(points):05d}", nm, lat, lng))
    for li, line in enumerate(lines):
        for lat, lng, _ in gmaps.sample_line(line, INTERVAL_M):
            points.append((f"L_{len(points):05d}", f"seg{li}", lat, lng))
    if args.limit:
        points = points[: args.limit]
    print(f"{len(points)} overhead sample points to scan.")

    rows = []
    for i, (pid, label, lat, lng) in enumerate(points, 1):
        dest = os.path.join(TILES, f"{pid}.jpg")
        if not os.path.exists(dest):
            if not gmaps.fetch_satellite(lat, lng, dest, zoom=ZOOM):
                continue
        try:
            r = vision.score_overhead(dest)
        except Exception as e:
            r = {"score": -1, "reason": f"error: {e}"}
        rows.append({"id": pid, "label": label, "lat": f"{lat:.6f}",
                     "lng": f"{lng:.6f}", "score": r["score"],
                     "reason": r.get("reason", ""), "file": os.path.basename(dest)})
        if i % 25 == 0:
            print(f"  ...{i}/{len(points)} scanned")

    rows.sort(key=lambda x: -int(x["score"]))
    with open(os.path.join(OUT, "overhead_candidates.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["score", "id", "label", "lat", "lng",
                                          "reason", "file"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    hot = [r for r in rows if int(r["score"]) >= args.min_score]
    _write_kml(hot, os.path.join(OUT, "overhead_candidates.kml"), args.min_score)
    print(f"\nDone. {len(rows)} points scored, {len(hot)} candidates >= {args.min_score}.")
    print("Next: python phase2_streetview_verify.py")


def _write_kml(rows, dest, min_score):
    marks = []
    for r in rows:
        marks.append(f"""  <Placemark><name>OH {r['score']} — {r['label']}</name>
    <description><![CDATA[{r['reason']}]]></description>
    <Point><coordinates>{r['lng']},{r['lat']},0</coordinates></Point></Placemark>""")
    with open(dest, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
                f'<name>Overhead candidates (score &gt;= {min_score})</name>\n'
                + "\n".join(marks) + "\n</Document></kml>\n")


if __name__ == "__main__":
    main()
