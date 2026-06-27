#!/usr/bin/env python3
"""
review_images.py — have Claude look at every Street View frame and score how
close vegetation is to the electrical feeder/conductors, then rank the worst
spots so you know where to send a crew.

Reads streetview_out/manifest.csv (from fetch_streetview.py) and writes:
  - streetview_out/review.csv      every frame, scored, sorted worst-first
  - streetview_out/hotspots.kml    top locations, openable in Google Earth
                                   (on your phone too) to drive straight to them

Setup:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY="your-key"
    python review_images.py

Cost note: this sends each image to the Claude API. ~960 images is a few
dollars. Use --limit while testing.
"""
import argparse
import base64
import csv
import os

import anthropic

OUT_DIR = "streetview_out"
MODEL = "claude-opus-4-8"

# The rubric Claude grades each image against. Explicit, gradeable criteria
# beat vague ones — the score drives the final ranking.
RUBRIC = """You are inspecting a Google Street View image taken along an
overhead electrical distribution feeder, looking for vegetation that could
cause an intermittent ground fault by contacting or nearly contacting the
power lines.

Look for the overhead conductors (power lines) and the poles. Judge how close
trees, branches, or other vegetation are to the PRIMARY conductors.

Score the vegetation-to-conductor proximity from 0 to 5:
  5 = branches/foliage are touching or overhanging the conductors (imminent fault risk)
  4 = vegetation within roughly a foot of the conductors
  3 = vegetation within a few feet; growing toward the lines
  2 = nearby trees but clear separation from the lines
  1 = vegetation present but well clear of any lines
  0 = no power lines visible, or no vegetation anywhere near them

If you cannot see overhead conductors in the image at all, score 0 and say so."""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [0, 1, 2, 3, 4, 5]},
        "conductors_visible": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["score", "conductors_visible", "reason"],
    "additionalProperties": False,
}


def read_manifest(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def score_image(client, img_path):
    with open(img_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=RUBRIC,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                {"type": "text", "text": "Score this image per the rubric."},
            ],
        }],
    )
    import json
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def write_hotspots_kml(rows, dest, min_score=3):
    """Group scored frames by location, keep the worst per location, emit KML."""
    by_loc = {}
    for r in rows:
        if int(r["score"]) < min_score:
            continue
        key = r["idx"]
        if key not in by_loc or int(r["score"]) > int(by_loc[key]["score"]):
            by_loc[key] = r
    placemarks = []
    for r in sorted(by_loc.values(), key=lambda x: -int(x["score"])):
        placemarks.append(f"""    <Placemark>
      <name>Score {r['score']} — pt {r['idx']} ({r['side']})</name>
      <description><![CDATA[{r['reason']}
Imagery date: {r.get('pano_date','')}]]></description>
      <Point><coordinates>{r['lng']},{r['lat']},0</coordinates></Point>
    </Placemark>""")
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <name>Feeder vegetation hotspots (score >= {min_score})</name>
{chr(10).join(placemarks)}
</Document></kml>"""
    with open(dest, "w") as f:
        f.write(kml)
    return len(placemarks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only score first N frames (testing)")
    ap.add_argument("--min-score", type=int, default=3, help="hotspot threshold for the KML")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY in your environment first.")

    manifest = read_manifest(os.path.join(OUT_DIR, "manifest.csv"))
    if args.limit:
        manifest = manifest[: args.limit]
    client = anthropic.Anthropic()

    scored = []
    for i, row in enumerate(manifest, 1):
        img_path = os.path.join(OUT_DIR, row["file"])
        if not os.path.exists(img_path):
            continue
        try:
            result = score_image(client, img_path)
        except Exception as e:  # keep going; note the failure in the row
            result = {"score": -1, "conductors_visible": False, "reason": f"error: {e}"}
        row.update({
            "score": result["score"],
            "conductors_visible": result["conductors_visible"],
            "reason": result["reason"],
        })
        scored.append(row)
        if i % 20 == 0:
            print(f"  ...scored {i}/{len(manifest)}")

    scored.sort(key=lambda r: -int(r["score"]))
    review_path = os.path.join(OUT_DIR, "review.csv")
    fields = ["score", "idx", "side", "lat", "lng", "heading",
              "pano_date", "conductors_visible", "reason", "file"]
    with open(review_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(scored)

    kml_path = os.path.join(OUT_DIR, "hotspots.kml")
    n = write_hotspots_kml(scored, kml_path, min_score=args.min_score)

    worst = [r for r in scored if int(r["score"]) >= 4]
    print(f"\nDone. Scored {len(scored)} frames.")
    print(f"  {len(worst)} frames at score 4-5 (vegetation at/near the conductors).")
    print(f"  review.csv written (sorted worst-first).")
    print(f"  hotspots.kml written with {n} locations — open it in Google Earth.")


if __name__ == "__main__":
    main()
