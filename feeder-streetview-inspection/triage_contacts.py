#!/usr/bin/env python3
"""
triage_contacts.py — severity triage for saturated terrain (e.g. redwood country
where nearly every span scores 4-5 on the standard rubric).

Re-examines the flagged frames with a strict contact-severity rubric and
rewrites midspan_problems.csv with a finer ranking:
  severity 9-10 (contact / limbs wrapping)          -> combined 5
  severity 7-8  (direct overhang within ~4 ft)      -> combined 4
  severity <7   (encroachment / tall-nearby only)   -> combined 3 (drops out at --min 4)

    export ANTHROPIC_API_KEY=...
    python triage_contacts.py --dir out_bc
"""
import argparse
import base64
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor

import vision

TRIAGE_RUBRIC = """You are re-examining a Street View frame that was already
flagged for vegetation near electric distribution conductors, in redwood forest
where tall trees near lines are ubiquitous. Your job is TRIAGE: rate only the
direct interaction you can actually SEE between vegetation and the conductors.

severity scale (integer 0-10):
  10 = vegetation clearly IN CONTACT with a conductor (branch touching/leaning on/wrapping the wire)
  9  = contact is very likely (foliage visually merging with the wire path)
  8  = limbs directly overhang the conductor within a few feet
  7  = limbs overhang the conductor with several feet of separation
  5-6= vegetation beside (not over) the conductor inside the ~12 ft envelope
  3-4= tall trees near the line but with visible separation
  0-2= background forest only, or conductors not clearly visible

Be skeptical: dense background canopy BEHIND a wire often looks like contact.
Only rate 9-10 when the geometry genuinely supports it. If the image is foggy,
dark, or the wire path is unclear, cap severity at 6 and say so.

category: one of contact | overhang | encroach | tall_nearby | unclear"""

SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "integer", "enum": list(range(11))},
        "category": {"type": "string",
                     "enum": ["contact", "overhang", "encroach", "tall_nearby", "unclear"]},
        "reason": {"type": "string"},
    },
    "required": ["severity", "category", "reason"],
    "additionalProperties": False,
}


def triage(img_path):
    with open(img_path, "rb") as f:
        raw = f.read()
    data = base64.standard_b64encode(raw).decode()
    resp = vision._c().messages.create(
        model=vision.MODEL,
        max_tokens=1024,
        system=TRIAGE_RUBRIC,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": vision._media_type(raw), "data": data}},
            {"type": "text", "text": "Triage this flagged frame per the rubric."},
        ]}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="out_bc")
    ap.add_argument("--min-in", type=int, default=5,
                    help="triage rows at/above this existing combined score (default 5)")
    args = ap.parse_args()
    d = args.dir
    prob_csv = os.path.join(d, "midspan_problems.csv")
    rows = list(csv.DictReader(open(prob_csv)))
    targets = [r for r in rows if int(r["combined"]) >= args.min_in
               and os.path.exists(os.path.join(d, "midspan", f"{r['id']}_sv.jpg"))]
    print(f"Triaging {len(targets)} flagged locations...", flush=True)

    cache_path = os.path.join(d, "triage.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    def one(r):
        if r["id"] in cache:
            return r["id"], cache[r["id"]]
        try:
            t = triage(os.path.join(d, "midspan", f"{r['id']}_sv.jpg"))
        except Exception as e:
            msg = str(e)
            if "credit balance" in msg or "authentication_error" in msg:
                raise SystemExit(f"FATAL-BILLING: {msg[:160]}")
            t = {"severity": -1, "category": "unclear", "reason": f"err: {e}"}
        return r["id"], t

    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (rid, t) in enumerate(ex.map(one, targets), 1):
            cache[rid] = t
            if i % 25 == 0:
                json.dump(cache, open(cache_path, "w"))
                print(f"  ...{i}/{len(targets)}", flush=True)
    json.dump(cache, open(cache_path, "w"))

    n5 = n4 = 0
    for r in rows:
        t = cache.get(r["id"])
        if not t:
            continue
        sev, cat = int(t["severity"]), t["category"]
        if sev >= 9:
            r["combined"], n5 = "5", n5 + 1
        elif sev >= 7:
            r["combined"], n4 = "4", n4 + 1
        else:
            r["combined"] = "3"
        r["reason"] = f"[{cat}, severity {sev}/10] {t['reason']}"
    rows.sort(key=lambda r: -int(r["combined"]))
    with open(prob_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)
    print(f"Triage done: {n5} contact-level (5), {n4} overhang-level (4); rest demoted to 3.",
          flush=True)


if __name__ == "__main__":
    main()
