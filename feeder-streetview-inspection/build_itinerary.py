#!/usr/bin/env python3
"""
build_itinerary.py — field driving itinerary for the hotspot list.

Clusters hotspots that are walkable from one parking spot (<=250 m), orders the
clusters with nearest-neighbor + 2-opt starting from a given start point, and
writes a phone-friendly HTML itinerary (tap-to-navigate Google Maps links per
stop and per leg) plus a numbered KML for Google Earth.

    export GOOGLE_MAPS_API_KEY=...   # geocoding for stop addresses
    python build_itinerary.py --dir out_alamo --start 32.8995 -105.9603 \
        --title "OCEC Alamogordo Feeder"
"""
import argparse
import csv
import html
import os
import re

import gmaps

CLUSTER_M = 250
WINDING = 1.35        # mountain-road factor on straight-line distance
MPH = 25
MIN_PER_STOP = 8


def load_hotspots(d):
    rows = list(csv.DictReader(open(os.path.join(d, "midspan_problems.csv"))))
    spots = []
    for rank, r in enumerate(rows, 1):
        m = re.search(r"nearest recloser: ([^ ]+)", r["reason"])
        spots.append(dict(rank=rank, id=r["id"], lat=float(r["lat"]), lng=float(r["lng"]),
                          score=int(r["combined"]),
                          sv=r["reason"].startswith("[SV VERIFIED"),
                          has_sv=r.get("has_sv") == "True",
                          recloser=m.group(1) if m else "?"))
    return spots


def cluster(spots):
    clusters = []
    for s in sorted(spots, key=lambda x: -x["score"]):
        for c in clusters:
            if gmaps.haversine((s["lat"], s["lng"]), (c["lat"], c["lng"])) <= CLUSTER_M:
                c["spots"].append(s)
                break
        else:
            clusters.append(dict(lat=s["lat"], lng=s["lng"], spots=[s]))
    for c in clusters:
        c["lat"] = sum(s["lat"] for s in c["spots"]) / len(c["spots"])
        c["lng"] = sum(s["lng"] for s in c["spots"]) / len(c["spots"])
        c["score"] = max(s["score"] for s in c["spots"])
    return clusters


def route(clusters, start):
    # nearest neighbor
    todo = clusters[:]
    cur = start
    order = []
    while todo:
        nxt = min(todo, key=lambda c: gmaps.haversine(cur, (c["lat"], c["lng"])))
        todo.remove(nxt)
        order.append(nxt)
        cur = (nxt["lat"], nxt["lng"])

    def tour_len(seq):
        tot, cur = 0.0, start
        for c in seq:
            tot += gmaps.haversine(cur, (c["lat"], c["lng"]))
            cur = (c["lat"], c["lng"])
        return tot

    # 2-opt
    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 2, len(order)):
                cand = order[:i + 1] + order[i + 1:j + 1][::-1] + order[j + 1:]
                if tour_len(cand) < tour_len(order) - 1:
                    order = cand
                    improved = True
    return order


def maps_point(lat, lng):
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"


def maps_leg(points):
    """Navigation link from current location through up to 9 points."""
    dest = points[-1]
    url = f"https://www.google.com/maps/dir/?api=1&destination={dest[0]:.6f},{dest[1]:.6f}&travelmode=driving"
    if len(points) > 1:
        wps = "|".join(f"{p[0]:.6f},{p[1]:.6f}" for p in points[:-1])
        url += f"&waypoints={wps}"
    return url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="out_alamo")
    ap.add_argument("--start", nargs=2, type=float, required=True, metavar=("LAT", "LNG"))
    ap.add_argument("--title", default="Feeder Hotspot Field Itinerary")
    args = ap.parse_args()
    gmaps.key()
    start = tuple(args.start)

    spots = load_hotspots(args.dir)
    ordered = route(cluster(spots), start)
    print(f"{len(spots)} hotspots -> {len(ordered)} driving stops")

    cards, kml_marks = [], []
    cur, drive_km, total_min = start, 0.0, 0.0
    leg_points = []
    for n, c in enumerate(ordered, 1):
        d = gmaps.haversine(cur, (c["lat"], c["lng"])) * WINDING / 1000
        drive_km += d
        total_min += (d / 1.609) / MPH * 60 + MIN_PER_STOP * 0 + len(c["spots"]) * MIN_PER_STOP
        cur = (c["lat"], c["lng"])
        leg_points.append((c["lat"], c["lng"]))
        addr = gmaps.reverse_geocode(c["lat"], c["lng"]) or "(no address — off-road)"
        rows = []
        for s in sorted(c["spots"], key=lambda x: -x["score"]):
            badge = "✓ SV-verified" if s["sv"] else ("SV available" if s["has_sv"] else "⚠ satellite-only")
            rows.append(f"<li>Report #{s['rank']} — <b>{s['score']}/5</b> ({badge}, {s['recloser']}) "
                        f"&middot; <a href='{maps_point(s['lat'], s['lng'])}'>{s['lat']:.5f}, {s['lng']:.5f}</a></li>")
        cards.append(f"""<div class=stop>
 <h2>Stop {n} <span class=sub>~{d:.1f} km from previous &middot; {len(c['spots'])} spot(s) &middot; worst {c['score']}/5</span></h2>
 <p class=meta><a href="{maps_leg([(c['lat'], c['lng'])])}"><b>▶ Navigate here</b></a> &middot; {html.escape(addr)}</p>
 <ul>{''.join(rows)}</ul></div>""")
        kml_marks.append(f"""  <Placemark><name>Stop {n} ({c['score']}/5)</name>
   <description>{len(c['spots'])} hotspot(s)</description>
   <Point><coordinates>{c['lng']:.6f},{c['lat']:.6f},0</coordinates></Point></Placemark>""")

    # per-leg multi-waypoint nav links (chunks of 9)
    legs = []
    for i in range(0, len(ordered), 9):
        chunk = [(c["lat"], c["lng"]) for c in ordered[i:i + 9]]
        legs.append(f"<li><a href='{maps_leg(chunk)}'>Leg {i//9 + 1}: stops {i+1}–{i+len(chunk)}</a></li>")

    hours = total_min / 60 + drive_km / 1.609 / MPH * 0  # stop time already added per spot
    doc = f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(args.title)} — Field Itinerary</title>
<style>body{{font-family:system-ui,Arial,sans-serif;margin:16px;color:#1a1a1a;max-width:720px}}
h1{{font-size:1.3em;margin-bottom:2px}}.lead{{color:#555;margin-top:0;font-size:.92em}}
.stop{{border:1px solid #ccc;border-radius:8px;padding:10px 12px;margin:10px 0}}
.stop h2{{margin:0;font-size:1em}}.sub{{font-weight:normal;color:#777;font-size:.8em}}
.meta{{font-size:.88em;margin:6px 0}}ul{{margin:4px 0 2px;padding-left:20px;font-size:.86em}}
li{{margin:3px 0}}a{{color:#0645ad}}</style></head><body>
<h1>{html.escape(args.title)} — Field Itinerary</h1>
<p class=lead>Start: Alamogordo ({start[0]:.4f}, {start[1]:.4f}). {len(ordered)} driving stops
covering {len(spots)} hotspots, ordered for shortest route. ~{drive_km:.0f} km driving
(winding-road estimate), roughly {total_min/60:.1f} h including ~{MIN_PER_STOP} min per spot.
Tap <b>▶ Navigate here</b> at each stop, or use the multi-stop leg links below.</p>
<p class=meta><b>Multi-stop navigation:</b></p><ul>{''.join(legs)}</ul>
{''.join(cards)}
</body></html>"""
    out_html = os.path.join(args.dir, "field_itinerary.html")
    open(out_html, "w").write(doc)

    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
           f'<name>{html.escape(args.title)} itinerary</name>\n' + "\n".join(kml_marks) + "\n</Document></kml>\n")
    out_kml = os.path.join(args.dir, "field_itinerary.kml")
    open(out_kml, "w").write(kml)
    print(f"Wrote {out_html} and {out_kml}")


if __name__ == "__main__":
    main()
