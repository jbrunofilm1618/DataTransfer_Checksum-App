#!/usr/bin/env python3
"""
fetch_streetview.py — sample a feeder route from a KMZ/KML and pull Google
Street View frames of BOTH sides of the line at a fixed spacing.

What it does:
  1. Reads the LineString(s) from your KMZ (the feeder route).
  2. Walks the route every INTERVAL_M meters.
  3. At each point, checks the FREE Street View metadata endpoint (so you don't
     pay for points with no imagery — common on off-road spans).
  4. Where imagery exists, downloads two images aimed perpendicular to the
     route (left + right) so you're looking at the poles/conductors and the
     vegetation beside them.
  5. Writes everything to OUT_DIR plus a manifest.csv you can feed to the
     reviewer.

Setup:
    pip install -r requirements.txt
    export GOOGLE_MAPS_API_KEY="your-key"   # Street View Static API enabled
    python fetch_streetview.py feeder.kmz

Re-running is safe: already-downloaded frames are skipped (resume).
"""
import csv
import math
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

# ---- Tunables -------------------------------------------------------------
INTERVAL_M = 40          # spacing between samples along the line, in meters
OUT_DIR = "streetview_out"
SIZE = "640x640"         # max free size; 640x640 is plenty for spotting trees
PITCH = 10               # tilt up slightly to catch conductor height
FOV = 75                 # field of view (lower = more zoomed in)
SLEEP_S = 0.05           # be polite to the API between calls
META_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMG_URL = "https://maps.googleapis.com/maps/api/streetview"
# ---------------------------------------------------------------------------


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_kml(path: str) -> str:
    if path.lower().endswith(".kml"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise SystemExit("No .kml found inside the KMZ archive.")
        name = "doc.kml" if "doc.kml" in names else names[0]
        return z.read(name).decode("utf-8", errors="replace")


def parse_lines(kml: str):
    """Return a list of polylines; each polyline is a list of (lat, lng)."""
    root = ET.fromstring(kml)
    lines = []
    for elem in root.iter():
        if strip_ns(elem.tag) != "coordinates":
            continue
        # Only take coordinates that belong to a LineString/LinearRing.
        pts = []
        for tok in elem.text.split():
            parts = tok.split(",")
            if len(parts) < 2:
                continue
            lng, lat = float(parts[0]), float(parts[1])
            pts.append((lat, lng))
        if len(pts) >= 2:
            lines.append(pts)
    if not lines:
        raise SystemExit(
            "No LineString found. Run inspect_kmz.py — if your KMZ is an image "
            "overlay only, trace the feeder centerline as a Path in Google Earth first."
        )
    return lines


def haversine(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def bearing(a, b):
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlo = math.radians(b[1] - a[1])
    x = math.sin(dlo) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def interpolate(p, q, frac):
    return (p[0] + (q[0] - p[0]) * frac, p[1] + (q[1] - p[1]) * frac)


def sample(line, step):
    """Yield (lat, lng, path_bearing) every `step` meters along one polyline."""
    carry = 0.0
    for p, q in zip(line, line[1:]):
        seg = haversine(p, q)
        if seg == 0:
            continue
        brg = bearing(p, q)
        d = carry
        while d < seg:
            lat, lng = interpolate(p, q, d / seg)
            yield (lat, lng, brg)
            d += step
        carry = d - seg


def meta_ok(lat, lng, key):
    r = requests.get(META_URL, params={"location": f"{lat},{lng}", "key": key}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("status") == "OK", j.get("date", ""), j.get("pano_id", "")


def fetch_image(lat, lng, heading, key, dest):
    r = requests.get(
        IMG_URL,
        params={
            "size": SIZE,
            "location": f"{lat},{lng}",
            "heading": round(heading),
            "pitch": PITCH,
            "fov": FOV,
            "key": key,
            "return_error_code": "true",
        },
        timeout=60,
    )
    if r.ok and r.headers.get("content-type", "").startswith("image"):
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    return False


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python fetch_streetview.py <file.kmz|file.kml>")
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise SystemExit("Set GOOGLE_MAPS_API_KEY in your environment first.")

    kml = load_kml(sys.argv[1])
    lines = parse_lines(kml)
    total_m = sum(haversine(p, q) for line in lines for p, q in zip(line, line[1:]))
    print(f"Loaded {len(lines)} line(s), ~{total_m/1609.34:.1f} miles total.")
    print(f"Sampling every {INTERVAL_M} m → roughly {int(total_m/INTERVAL_M)} points x 2 sides.")

    os.makedirs(OUT_DIR, exist_ok=True)
    manifest_path = os.path.join(OUT_DIR, "manifest.csv")
    # Resume: remember which files already exist.
    existing = set(os.listdir(OUT_DIR))

    rows = []
    idx = 0
    no_imagery = 0
    fetched = 0
    for line in lines:
        for lat, lng, brg in sample(line, INTERVAL_M):
            idx += 1
            ok, date, pano = meta_ok(lat, lng, key)
            if not ok:
                no_imagery += 1
                continue
            for side, h in (("L", (brg - 90) % 360), ("R", (brg + 90) % 360)):
                fn = f"{idx:05d}_{side}.jpg"
                rows.append([idx, f"{lat:.6f}", f"{lng:.6f}", side, round(h), date, pano, fn])
                if fn in existing:
                    continue
                if fetch_image(lat, lng, h, key, os.path.join(OUT_DIR, fn)):
                    fetched += 1
            if idx % 25 == 0:
                print(f"  ...{idx} points checked, {fetched} new images, {no_imagery} with no imagery")
            time.sleep(SLEEP_S)

    with open(manifest_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "lat", "lng", "side", "heading", "pano_date", "pano_id", "file"])
        w.writerows(rows)

    print(f"\nDone. {fetched} images downloaded this run, {len(rows)} manifest rows.")
    print(f"{no_imagery} sample points had no Street View imagery (skipped, free).")
    print(f"Output in {OUT_DIR}/ — next: python review_images.py")


if __name__ == "__main__":
    main()
