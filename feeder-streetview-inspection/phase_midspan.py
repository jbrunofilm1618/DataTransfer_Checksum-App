#!/usr/bin/env python3
"""
Mid-span sweep — vegetation BETWEEN junctions, along all 403 spans.

Two-tier to keep it cheap/fast:
  1. bulk overhead screen with a fast model (Haiku) on a line-overlaid satellite
     tile at every sample point;
  2. for hits (>= MIN), re-score overhead with the main model (Opus) and, where
     Street View exists, pull an along-line frame and score height/proximity.

Writes incrementally (resumable): out/midspan_all.csv (every point) and
out/midspan_problems.csv (combined >= MIN). Skips points near a junction (those
are already covered).

    export GOOGLE_MAPS_API_KEY=...  ANTHROPIC_API_KEY=...
    python phase_midspan.py "Mescalero_West_Phase_A_Overlay.kmz"
"""
import csv, os, sys
import gmaps, vision

OUT = "out"
TILES = os.path.join(OUT, "midspan")
INTERVAL_M = 50          # mid-span sample spacing
JUNCTION_SKIP_M = 35     # don't re-screen points this close to a junction
MIN = 3                  # keep as a problem at/above this combined score
ALL_CSV = os.path.join(OUT, "midspan_all.csv")
PROB_CSV = os.path.join(OUT, "midspan_problems.csv")


def main():
    gmaps.key()
    os.makedirs(TILES, exist_ok=True)
    kml = gmaps.load_kml(sys.argv[1])
    lines = gmaps.parse_lines(kml)
    junctions = [(lat, lng) for _, lat, lng in gmaps.parse_named_points(kml)]

    # Build the mid-span point list (with local bearing for Street View aim).
    pts = []
    for li, line in enumerate(lines):
        for lat, lng, brg in gmaps.sample_line(line, INTERVAL_M):
            if any(gmaps.haversine((lat, lng), j) < JUNCTION_SKIP_M for j in junctions):
                continue
            pts.append((f"M{len(pts):05d}", lat, lng, brg))
    print(f"{len(pts)} mid-span sample points (~{INTERVAL_M} m).", flush=True)

    done = set()
    if os.path.exists(ALL_CSV):
        done = {r["id"] for r in csv.DictReader(open(ALL_CSV))}
        print(f"Resuming — {len(done)} already done.", flush=True)

    all_f = open(ALL_CSV, "a", newline="")
    aw = csv.writer(all_f)
    if not done:
        aw.writerow(["id", "lat", "lng", "fast", "overhead", "sv", "combined", "has_sv", "reason"])

    n_prob = 0
    for i, (mid, lat, lng, brg) in enumerate(pts, 1):
        if mid in done:
            continue
        tile = os.path.join(TILES, f"{mid}.jpg")
        if not os.path.exists(tile):
            if not gmaps.fetch_satellite_overlay(lat, lng, lines, tile, zoom=20, radius_m=70):
                continue
        try:
            fast = int(vision.score_overhead(tile, model=vision.FAST_MODEL)["score"])
        except Exception:
            fast = -1
        oh, sv, reason, has_sv = fast, -1, "", False
        if fast >= MIN:                       # verify the hit with the main model
            try:
                d = vision.score_overhead(tile)
                oh, reason = int(d["score"]), d.get("reason", "")
            except Exception:
                pass
            if oh >= MIN:
                ok, _, _ = gmaps.sv_metadata(lat, lng)
                if ok:
                    has_sv = True
                    f = os.path.join(TILES, f"{mid}_sv.jpg")
                    if gmaps.fetch_streetview(lat, lng, brg, f, pitch=8, fov=80):
                        try:
                            sd = vision.score_streetview(f)
                            sv = int(sd["score"])
                            if sv >= oh:
                                reason = sd.get("reason", reason)
                        except Exception:
                            pass
        combined = max(oh, sv)
        if oh >= MIN and sv >= MIN:
            combined = min(5, combined + 1)
        aw.writerow([mid, f"{lat:.6f}", f"{lng:.6f}", fast, oh, sv, combined, has_sv, reason])
        all_f.flush()
        if combined >= MIN:
            n_prob += 1
        if i % 50 == 0:
            print(f"  ...{i}/{len(pts)} screened, {n_prob} problems so far", flush=True)

    # Rebuild the problems CSV from the full results.
    rows = [r for r in csv.DictReader(open(ALL_CSV)) if int(r["combined"]) >= MIN]
    rows.sort(key=lambda r: -int(r["combined"]))
    with open(PROB_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["combined", "id", "lat", "lng", "overhead",
                                          "sv", "has_sv", "reason"], extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"DONE. {len(rows)} mid-span problem points (>= {MIN}) written to {PROB_CSV}", flush=True)


if __name__ == "__main__":
    main()
