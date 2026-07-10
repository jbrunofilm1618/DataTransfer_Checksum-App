#!/usr/bin/env python3
"""
PHASE 2 — Street View verification + final ranking.

Takes the worst overhead candidates from Phase 1, and for each one (that has
Street View imagery) pulls side-view frames so Claude can confirm vegetation is
actually close to the conductors, then computes a combined rank. Candidates with
no Street View imagery are kept (flagged 'satellite-only') so off-road spans
aren't silently dropped.

    export GOOGLE_MAPS_API_KEY=...   # Street View Static API
    export ANTHROPIC_API_KEY=...
    python phase2_streetview_verify.py --top 80

Output (in out/):
  streetview/<id>_<side>.jpg
  verified.csv     final ranked list (overhead + street view + combined score)
"""
import argparse
import csv
import os

import gmaps
import vision

OUT = "out"
SV_DIR = os.path.join(OUT, "streetview")


def load_candidates():
    with open(os.path.join(OUT, "overhead_candidates.csv"), newline="") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=80, help="verify the top N overhead candidates")
    ap.add_argument("--min-overhead", type=int, default=3, help="only verify candidates at/above this overhead score")
    args = ap.parse_args()
    gmaps.key()
    os.makedirs(SV_DIR, exist_ok=True)

    cands = [c for c in load_candidates() if int(c["score"]) >= args.min_overhead][: args.top]
    print(f"Verifying {len(cands)} candidates with Street View...")

    rows = []
    for i, c in enumerate(cands, 1):
        lat, lng = float(c["lat"]), float(c["lng"])
        ok, date, _ = gmaps.sv_metadata(lat, lng)
        sv_score, sv_reason, files = -1, "no Street View imagery (satellite-only)", []
        if ok:
            best = -1
            reasons = []
            for side, h in (("L", 0), ("R", 90), ("B", 180), ("R2", 270)):
                dest = os.path.join(SV_DIR, f"{c['id']}_{side}.jpg")
                if not os.path.exists(dest):
                    if not gmaps.fetch_streetview(lat, lng, h, dest):
                        continue
                files.append(os.path.basename(dest))
                try:
                    r = vision.score_streetview(dest)
                    if int(r["score"]) > best:
                        best, sv_reason = int(r["score"]), r.get("reason", "")
                    reasons.append(f"{side}:{r['score']}")
                except Exception as e:
                    reasons.append(f"{side}:err")
            sv_score = best
        oh = int(c["score"])
        combined = max(oh, sv_score) + (1 if (oh >= 3 and sv_score >= 3) else 0)
        rows.append({
            "combined": combined, "overhead_score": oh, "sv_score": sv_score,
            "id": c["id"], "label": c["label"], "lat": c["lat"], "lng": c["lng"],
            "sv_date": date, "overhead_reason": c["reason"], "sv_reason": sv_reason,
            "overhead_file": c["file"], "sv_files": ";".join(files),
        })
        if i % 10 == 0:
            print(f"  ...{i}/{len(cands)}")

    rows.sort(key=lambda x: (-int(x["combined"]), -int(x["sv_score"])))
    fields = ["combined", "overhead_score", "sv_score", "id", "label", "lat", "lng",
              "sv_date", "overhead_reason", "sv_reason", "overhead_file", "sv_files"]
    with open(os.path.join(OUT, "verified.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nDone. verified.csv written ({len(rows)} spots).")
    print("Next: python phase3_build_report.py")


if __name__ == "__main__":
    main()
