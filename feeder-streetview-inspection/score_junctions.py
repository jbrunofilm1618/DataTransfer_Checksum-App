#!/usr/bin/env python3
"""Score every junction from already-fetched imagery (overhead overlay +
along-line Street View) and write out/SCORES.json for build_survey.py."""
import csv, glob, json, os
import vision

OUT = "out"

def main():
    rows = list(csv.DictReader(open(os.path.join(OUT, "junctions.csv"))))
    scores = {}
    for i, r in enumerate(rows, 1):
        jid = r["id"]
        oh_s, oh_r = -1, ""
        ohp = f"{OUT}/overlay/{jid}_z19.jpg"
        if os.path.exists(ohp):
            try:
                d = vision.score_overhead(ohp); oh_s, oh_r = int(d["score"]), d.get("reason","")
            except Exception as e:
                oh_r = f"overhead err: {e}"
        sv_s, sv_r = -1, ""
        for f in sorted(glob.glob(f"{OUT}/sv_along/{jid}_*.jpg")):
            try:
                d = vision.score_streetview(f)
                if int(d["score"]) > sv_s:
                    sv_s, sv_r = int(d["score"]), d.get("reason","")
            except Exception as e:
                sv_r = sv_r or f"sv err: {e}"
        combined = max(oh_s, sv_s)
        if oh_s >= 3 and sv_s >= 3:
            combined = min(5, combined + 1)
        reason = " | ".join(p for p in
                            [f"overhead {oh_s}/5: {oh_r}" if oh_s >= 0 else "",
                             f"street view {sv_s}/5: {sv_r}" if sv_s >= 0 else "no Street View"] if p)
        scores[jid] = [combined, reason]
        print(f"  [{i}/{len(rows)}] {r['name']}: overhead={oh_s} sv={sv_s} -> {combined}")
    json.dump(scores, open(os.path.join(OUT, "SCORES.json"), "w"), indent=1)
    print(f"Wrote {OUT}/SCORES.json for {len(scores)} junctions.")

if __name__ == "__main__":
    main()
