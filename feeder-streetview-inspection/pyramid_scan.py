#!/usr/bin/env python3
"""
pyramid_scan.py — two-level satellite vegetation screen for LARGE systems
(hundreds to thousands of line-miles), satellite-first, before any Street View.

Level 1 (wide, cheap):  sample all OVERHEAD lines every ~500 m (underground
  folders are excluded), fetch a zoom-16 tile with the line drawn on, cull
  obviously-barren desert tiles with a free local greenness check, and Haiku-
  screen only the green-ish ones for "line passes through/near tree cover".

Level 2 (fine, targeted):  around every Level-1 hit, sample the same line at
  ~60 m, fetch zoom-20 overlay tiles, Haiku-screen, Opus-verify. Output uses
  the mid-span schema so build_master.py consumes it directly.

    export GOOGLE_MAPS_API_KEY=...  ANTHROPIC_API_KEY=...
    python pyramid_scan.py FILE.kmz --dir out_ocec --level 1
    python pyramid_scan.py FILE.kmz --dir out_ocec --level 2

Both levels are resumable and abort loudly on billing errors.
"""
import argparse
import csv
import math
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import gmaps
import vision

L1_STEP_M = 500
L1_DEDUPE_M = 420
L1_FLAG = 3            # L1 score at/above this goes to Level 2
GREEN_MIN = 0.04       # greenness fraction below this = free desert cull
L2_STEP_M = 60
L2_RADIUS_M = 300      # how far around each L1 hit Level 2 sweeps
L2_MIN = 3


def parse_overhead_lines(kml, exclude_re=r"underground"):
    """Folder-aware line extraction: skip Placemarks inside excluded folders."""
    root = ET.fromstring(kml)
    sn = gmaps.strip_ns
    lines = []

    def walk(el, excluded):
        for ch in el:
            tag = sn(ch.tag)
            if tag == "Folder":
                nm = ch.find("./{*}name")
                name = (nm.text or "") if nm is not None else ""
                walk(ch, excluded or bool(re.search(exclude_re, name, re.I)))
            elif tag in ("Document", "kml"):
                walk(ch, excluded)
            elif tag == "Placemark" and not excluded:
                for coords in ch.findall(".//{*}LineString/{*}coordinates"):
                    pts = []
                    for tok in (coords.text or "").split():
                        parts = tok.split(",")
                        if len(parts) >= 2:
                            pts.append((float(parts[1]), float(parts[0])))
                    if len(pts) >= 2:
                        lines.append(pts)

    walk(root, False)
    return lines


def green_fraction(img_path):
    """Fraction of vegetation-green pixels; free desert cull."""
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    im = im.crop((0, 0, im.width, int(im.height * 0.95)))  # drop logo band
    im.thumbnail((200, 200))
    hsv = im.convert("HSV")
    px = hsv.getdata()
    green = sum(1 for h, s, v in px if 40 <= h <= 130 and s > 50 and v > 35)
    return green / max(1, len(px))


def billing_guard(e):
    msg = str(e)
    if "credit balance" in msg or "authentication_error" in msg:
        raise SystemExit(f"FATAL-BILLING: {msg[:160]}")


def dedupe_key(lat, lng, cell_m):
    return (int(lat * 111000 / cell_m),
            int(lng * 111000 * math.cos(math.radians(lat)) / cell_m))


def level1(args, lines, d):
    tiles = os.path.join(d, "l1_tiles")
    os.makedirs(tiles, exist_ok=True)
    all_csv = os.path.join(d, "l1_all.csv")
    done = set()
    if os.path.exists(all_csv):
        for r in csv.DictReader(open(all_csv)):
            if not r["reason"].startswith("err:"):
                done.add(r["id"])
        print(f"L1: resuming — {len(done)} cells already scored.", flush=True)

    # Build globally-deduped sample cells.
    seen, cells = set(), []
    for li, line in enumerate(lines):
        for lat, lng, _ in gmaps.sample_line(line, L1_STEP_M):
            k = dedupe_key(lat, lng, L1_DEDUPE_M)
            if k in seen:
                continue
            seen.add(k)
            cells.append((f"C{len(cells):06d}", li, lat, lng))
    print(f"L1: {len(cells)} wide cells cover the overhead system.", flush=True)

    f = open(all_csv, "a", newline="")
    w = csv.writer(f)
    if not done:
        w.writerow(["id", "line_idx", "lat", "lng", "green", "score", "reason"])

    todo = [c for c in cells if c[0] not in done]

    def fetch(c):
        cid, li, lat, lng = c
        dest = os.path.join(tiles, f"{cid}.jpg")
        if not os.path.exists(dest):
            gmaps.fetch_satellite_overlay(lat, lng, lines, dest, zoom=16, radius_m=800,
                                          weight=3, max_runs=12, marker=False)
        return c, dest

    def score(item):
        c, dest = item
        cid, li, lat, lng = c
        if not os.path.exists(dest):
            return c, -1.0, -1, "err: tile fetch failed"
        g = green_fraction(dest)
        if g < GREEN_MIN:
            return c, g, 0, "desert cull (greenness below threshold)"
        try:
            r = vision.score_coarse(dest)
            return c, g, int(r["score"]), r.get("reason", "")
        except Exception as e:
            billing_guard(e)
            return c, g, -1, f"err: {e}"

    n_flag = n_cull = 0
    with ThreadPoolExecutor(max_workers=8) as fx, ThreadPoolExecutor(max_workers=4) as sx:
        for i, (c, g, sc, reason) in enumerate(sx.map(score, fx.map(fetch, todo)), 1):
            cid, li, lat, lng = c
            w.writerow([cid, li, f"{lat:.6f}", f"{lng:.6f}", f"{g:.3f}", sc, reason])
            f.flush()
            if sc >= L1_FLAG:
                n_flag += 1
            if sc == 0 and g >= 0 and g < GREEN_MIN:
                n_cull += 1
            if i % 200 == 0:
                print(f"  ...{i}/{len(todo)} cells | {n_cull} desert-culled | {n_flag} flagged",
                      flush=True)
    rows = list(csv.DictReader(open(all_csv)))
    flagged = [r for r in rows if r["score"].lstrip("-").isdigit() and int(r["score"]) >= L1_FLAG]
    print(f"L1 DONE. {len(rows)} cells: {len(flagged)} flagged (>= {L1_FLAG}) for Level 2.",
          flush=True)


def level2(args, lines, d):
    flagged = [r for r in csv.DictReader(open(os.path.join(d, "l1_all.csv")))
               if r["score"].lstrip("-").isdigit() and int(r["score"]) >= L1_FLAG]
    print(f"L2: {len(flagged)} flagged cells to sweep at {L2_STEP_M} m.", flush=True)
    tiles = os.path.join(d, "midspan")
    os.makedirs(tiles, exist_ok=True)
    all_csv = os.path.join(d, "midspan_all.csv")
    done = set()
    if os.path.exists(all_csv):
        for r in csv.DictReader(open(all_csv)):
            if not (r["fast"] == "-1" and r["reason"].startswith("err:")):
                done.add(r["id"])
        print(f"L2: resuming — {len(done)} points already scored.", flush=True)

    # Fine sample points: along each flagged cell's source line, within radius.
    seen, pts = set(), []
    for r in flagged:
        li, clat, clng = int(r["line_idx"]), float(r["lat"]), float(r["lng"])
        for lat, lng, _ in gmaps.sample_line(lines[li], L2_STEP_M):
            if gmaps.haversine((lat, lng), (clat, clng)) > L2_RADIUS_M:
                continue
            k = dedupe_key(lat, lng, L2_STEP_M * 0.8)
            if k in seen:
                continue
            seen.add(k)
            pts.append((f"M{len(pts):06d}", lat, lng))
    print(f"L2: {len(pts)} fine points.", flush=True)

    f = open(all_csv, "a", newline="")
    w = csv.writer(f)
    if not done:
        w.writerow(["id", "lat", "lng", "fast", "overhead", "sv", "combined", "has_sv", "reason"])

    todo = [p for p in pts if p[0] not in done]

    def work(p):
        mid, lat, lng = p
        dest = os.path.join(tiles, f"{mid}.jpg")
        if not os.path.exists(dest):
            if not gmaps.fetch_satellite_overlay(lat, lng, lines, dest, zoom=20,
                                                 radius_m=80, max_runs=8):
                return p, -1, -1, "err: tile fetch failed"
        try:
            fast = int(vision.score_overhead(dest, model=vision.FAST_MODEL)["score"])
        except Exception as e:
            billing_guard(e)
            return p, -1, -1, f"err: {e}"
        oh, reason = fast, ""
        if fast >= L2_MIN:
            try:
                rr = vision.score_overhead(dest)
                oh, reason = int(rr["score"]), rr.get("reason", "")
            except Exception as e:
                billing_guard(e)
        return p, fast, oh, reason

    n_hot = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (p, fast, oh, reason) in enumerate(ex.map(work, todo), 1):
            mid, lat, lng = p
            w.writerow([mid, f"{lat:.6f}", f"{lng:.6f}", fast, oh, -1, oh, False, reason])
            f.flush()
            if oh >= L2_MIN:
                n_hot += 1
            if i % 100 == 0:
                print(f"  ...{i}/{len(todo)} | {n_hot} hotspots", flush=True)

    rows = [r for r in csv.DictReader(open(all_csv)) if int(r["combined"]) >= L2_MIN]
    rows.sort(key=lambda r: -int(r["combined"]))
    with open(os.path.join(d, "midspan_problems.csv"), "w", newline="") as pf:
        dw = csv.DictWriter(pf, fieldnames=["combined", "id", "lat", "lng", "overhead",
                                            "sv", "has_sv", "reason"], extrasaction="ignore")
        dw.writeheader(); dw.writerows(rows)
    print(f"L2 DONE. {len(rows)} verified hotspots (>= {L2_MIN}).", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kmz")
    ap.add_argument("--dir", default="out_pyramid")
    ap.add_argument("--level", type=int, choices=(1, 2), required=True)
    args = ap.parse_args()
    gmaps.key()
    os.makedirs(args.dir, exist_ok=True)
    lines = parse_overhead_lines(gmaps.load_kml(args.kmz))
    print(f"{len(lines)} overhead segments, {gmaps.total_miles(lines):.0f} miles "
          f"(underground excluded).", flush=True)
    (level1 if args.level == 1 else level2)(args, lines, args.dir)


if __name__ == "__main__":
    main()
