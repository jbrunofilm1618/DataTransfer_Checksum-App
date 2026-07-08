#!/usr/bin/env python3
"""
roadwalk.py — general-area survey with NO line geometry required.

Instead of walking a KMZ, this walks the Street View network itself:
  A. Grid the bounding box with FREE metadata calls -> unique panos (the road
     network where imagery exists).
  B. Thin panos to ~45 m spacing, estimate the road bearing from neighbors,
     fetch two frames per pano (along-road, both directions).
  C. Two-tier scoring: Haiku screens every frame for "distribution conductors
     visible + vegetation at/above them" (CA GO 95 HFTD-tuned rubric); hits are
     re-verified with the main model. Problems get a satellite context tile.

Outputs (in --dir, default out_roadwalk/) use the same schema as the mid-span
sweep, so build_master.py can consume them directly:
  panos.csv, midspan_all.csv, midspan_problems.csv, midspan/<id>{,_sv}.jpg

    export GOOGLE_MAPS_API_KEY=...  ANTHROPIC_API_KEY=...
    python roadwalk.py --bbox 37.105 37.145 -122.145 -122.095 --dir out_bc

Resumable: re-running skips completed grid/frames/scores.
"""
import argparse
import csv
import math
import os
import shutil
from concurrent.futures import ThreadPoolExecutor

import gmaps
import vision

GRID_STEP_M = 50
THIN_M = 45
MIN = 3               # problem threshold
FRAME_KW = dict(pitch=10, fov=90)


def grid(lat0, lat1, lng0, lng1, step_m):
    dlat = step_m / 111000.0
    dlng = step_m / (111000.0 * math.cos(math.radians((lat0 + lat1) / 2)))
    la = lat0
    while la <= lat1:
        ln = lng0
        while ln <= lng1:
            yield (la, ln)
            ln += dlng
        la += dlat


def phase_a_panos(args, d):
    """Free metadata grid -> unique panos."""
    path = os.path.join(d, "panos.csv")
    if os.path.exists(path):
        rows = list(csv.DictReader(open(path)))
        print(f"A: panos.csv exists ({len(rows)} panos) — skipping grid.", flush=True)
        return rows
    pts = list(grid(args.bbox[0], args.bbox[1], args.bbox[2], args.bbox[3], GRID_STEP_M))
    print(f"A: probing {len(pts)} grid points (free metadata)...", flush=True)
    panos = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, m in enumerate(ex.map(lambda p: gmaps.sv_metadata_full(*p), pts), 1):
            if m and m["pano_id"] and m["lat"] is not None:
                panos[m["pano_id"]] = m
            if i % 1000 == 0:
                print(f"   ...{i}/{len(pts)} probed, {len(panos)} panos", flush=True)
    rows = list(panos.values())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["pano_id", "date", "lat", "lng"])
        w.writeheader(); w.writerows(rows)
    print(f"A: {len(rows)} unique panos.", flush=True)
    return rows


def phase_b_frames(args, d, panos):
    """Thin panos, estimate bearings, fetch two along-road frames each."""
    for p in panos:
        p["lat"] = float(p["lat"]); p["lng"] = float(p["lng"])
    panos.sort(key=lambda p: (p["lat"], p["lng"]))
    kept = []
    for p in panos:
        if all(gmaps.haversine((p["lat"], p["lng"]), (k["lat"], k["lng"])) >= THIN_M
               for k in kept[-60:]):        # local window is enough after sorting
            kept.append(p)
    if args.max_panos and len(kept) > args.max_panos:
        kept = kept[: args.max_panos]
        print(f"B: capped at {args.max_panos} panos (--max-panos).", flush=True)
    print(f"B: {len(kept)} panos after {THIN_M} m thinning; fetching 2 frames each.", flush=True)

    frames_dir = os.path.join(d, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    jobs = []
    for i, p in enumerate(kept):
        # Road bearing ~ direction to the nearest other kept pano.
        best, bd = None, 1e9
        for q in kept[max(0, i - 30): i + 30]:
            if q is p:
                continue
            dist = gmaps.haversine((p["lat"], p["lng"]), (q["lat"], q["lng"]))
            if dist < bd:
                bd, best = dist, q
        brg = gmaps.bearing((p["lat"], p["lng"]), (best["lat"], best["lng"])) if best else 0
        pid = f"R{i:05d}"
        p["id"] = pid
        for tag, h in (("a", brg % 360), ("b", (brg + 180) % 360)):
            dest = os.path.join(frames_dir, f"{pid}_{tag}.jpg")
            if not os.path.exists(dest):
                jobs.append((p["lat"], p["lng"], h, dest))
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda j: gmaps.fetch_streetview(j[0], j[1], j[2], j[3], **FRAME_KW), jobs))
    print(f"B: fetched {len(jobs)} new frames.", flush=True)
    return kept


def phase_c_score(args, d, kept):
    frames_dir = os.path.join(d, "frames")
    prob_img_dir = os.path.join(d, "midspan")
    os.makedirs(prob_img_dir, exist_ok=True)
    all_csv = os.path.join(d, "midspan_all.csv")
    done = set()
    if os.path.exists(all_csv):
        done = {r["id"] for r in csv.DictReader(open(all_csv))}
        print(f"C: resuming — {len(done)} panos already scored.", flush=True)
    f_all = open(all_csv, "a", newline="")
    w = csv.writer(f_all)
    if not done:
        w.writerow(["id", "lat", "lng", "fast", "overhead", "sv", "combined", "has_sv", "reason"])

    todo = [p for p in kept if p["id"] not in done]
    print(f"C: screening {len(todo)} panos x 2 frames (fast model)...", flush=True)

    def screen(p):
        best, reason = -1, ""
        for tag in ("a", "b"):
            fp = os.path.join(frames_dir, f"{p['id']}_{tag}.jpg")
            if not os.path.exists(fp):
                continue
            try:
                r = vision.score_roadwalk(fp, model=vision.FAST_MODEL)
                if int(r["score"]) > best:
                    best, reason = int(r["score"]), r.get("reason", "")
            except Exception as e:
                reason = reason or f"err: {e}"
        return p, best, reason

    n_prob = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (p, fast, reason) in enumerate(ex.map(screen, todo), 1):
            final, freason = fast, reason
            if fast >= MIN:                     # verify hit with the main model
                vbest = -1
                for tag in ("a", "b"):
                    fp = os.path.join(frames_dir, f"{p['id']}_{tag}.jpg")
                    if not os.path.exists(fp):
                        continue
                    try:
                        r = vision.score_roadwalk(fp)
                        if int(r["score"]) > vbest:
                            vbest, freason = int(r["score"]), r.get("reason", "")
                            vfile = fp
                    except Exception:
                        pass
                final = vbest if vbest >= 0 else fast
                if final >= MIN:
                    n_prob += 1
                    gmaps.fetch_satellite_overlay(p["lat"], p["lng"], [],
                        os.path.join(prob_img_dir, f"{p['id']}.jpg"), zoom=20, radius_m=1)
                    try:
                        shutil.copyfile(vfile, os.path.join(prob_img_dir, f"{p['id']}_sv.jpg"))
                    except Exception:
                        pass
            w.writerow([p["id"], f"{p['lat']:.6f}", f"{p['lng']:.6f}", fast, -1,
                        final, final, True, freason])
            f_all.flush()
            if i % 50 == 0:
                print(f"   ...{i}/{len(todo)} screened, {n_prob} problems", flush=True)

    rows = [r for r in csv.DictReader(open(all_csv)) if int(r["combined"]) >= MIN]
    rows.sort(key=lambda r: -int(r["combined"]))
    with open(os.path.join(d, "midspan_problems.csv"), "w", newline="") as f:
        dw = csv.DictWriter(f, fieldnames=["combined", "id", "lat", "lng", "overhead",
                                           "sv", "has_sv", "reason"], extrasaction="ignore")
        dw.writeheader(); dw.writerows(rows)
    print(f"DONE. {len(rows)} problem locations (>= {MIN}) in {d}/midspan_problems.csv", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("LAT0", "LAT1", "LNG0", "LNG1"))
    ap.add_argument("--dir", default="out_roadwalk")
    ap.add_argument("--max-panos", type=int, default=3000)
    ap.add_argument("--fetch-only", action="store_true",
                    help="run phases A+B (Google imagery) only; skip scoring")
    args = ap.parse_args()
    gmaps.key()
    os.makedirs(args.dir, exist_ok=True)
    panos = phase_a_panos(args, args.dir)
    kept = phase_b_frames(args, args.dir, panos)
    if args.fetch_only:
        print("Fetch-only: imagery collected; run again without --fetch-only to score.", flush=True)
        return
    phase_c_score(args, args.dir, kept)


if __name__ == "__main__":
    main()
