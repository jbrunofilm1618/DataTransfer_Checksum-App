#!/usr/bin/env python3
"""
PHASE 3 — Build the report.

Takes the ranked spots from Phase 2, reverse-geocodes each to a street address,
pulls a clean satellite close-up, and assembles a single self-contained HTML
report (images embedded) you can open anywhere or print to PDF. Top spots first.

    export GOOGLE_MAPS_API_KEY=...   # Geocoding API + Maps Static API
    python phase3_build_report.py --top 25

Output:
  out/report.html        the deliverable (coords, addresses, screenshots, notes)
  out/report_satellite/  clean satellite close-ups used in the report
"""
import argparse
import base64
import csv
import html
import os

import gmaps

OUT = "out"
REPORT_SAT = os.path.join(OUT, "report_satellite")


def b64img(path):
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.standard_b64encode(f.read()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="include the top N spots in the report")
    args = ap.parse_args()
    gmaps.key()
    os.makedirs(REPORT_SAT, exist_ok=True)

    with open(os.path.join(OUT, "verified.csv"), newline="") as f:
        rows = list(csv.DictReader(f))[: args.top]

    cards = []
    for rank, r in enumerate(rows, 1):
        lat, lng = float(r["lat"]), float(r["lng"])
        addr = gmaps.reverse_geocode(lat, lng) or "(no address — remote/off-road)"
        sat_dest = os.path.join(REPORT_SAT, f"{r['id']}.jpg")
        if not os.path.exists(sat_dest):
            gmaps.fetch_satellite(lat, lng, sat_dest, zoom=19)

        sv_imgs = ""
        for fn in (r.get("sv_files") or "").split(";"):
            if fn:
                src = b64img(os.path.join(OUT, "streetview", fn))
                if src:
                    sv_imgs += f'<img src="{src}" alt="street view">'
        sat_src = b64img(sat_dest)
        gmaps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"

        cards.append(f"""
<div class="card">
  <h2>#{rank} &nbsp; Combined score {html.escape(r['combined'])}
      <span class="sub">(overhead {html.escape(r['overhead_score'])} /
      street view {html.escape(r['sv_score'])})</span></h2>
  <p class="meta"><b>{html.escape(r['label'])}</b> &middot;
     {lat:.6f}, {lng:.6f} &middot;
     <a href="{gmaps_link}">open in Google Maps</a><br>
     <b>Address:</b> {html.escape(addr)} &middot;
     <b>SV imagery date:</b> {html.escape(r.get('sv_date') or 'n/a')}</p>
  <p class="reason"><b>Overhead:</b> {html.escape(r.get('overhead_reason',''))}</p>
  <p class="reason"><b>Street view:</b> {html.escape(r.get('sv_reason',''))}</p>
  <div class="imgs">
    <figure><figcaption>Satellite</figcaption>{f'<img src="{sat_src}">' if sat_src else '(none)'}</figure>
    <figure><figcaption>Street View</figcaption>{sv_imgs or '(no Street View imagery — satellite-only span)'}</figure>
  </div>
</div>""")

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Feeder Vegetation Inspection Report</title>
<style>
 body{{font-family:system-ui,Arial,sans-serif;margin:24px;color:#1a1a1a}}
 h1{{margin-bottom:4px}} .lead{{color:#555;margin-top:0}}
 .card{{border:1px solid #ddd;border-radius:10px;padding:16px;margin:18px 0}}
 .card h2{{margin:0 0 6px}} .sub{{font-weight:normal;color:#777;font-size:.8em}}
 .meta,.reason{{font-size:.92em;margin:4px 0}}
 .imgs{{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px}}
 figure{{margin:0}} figcaption{{font-size:.8em;color:#666;margin-bottom:4px}}
 img{{max-width:420px;width:100%;border-radius:6px;border:1px solid #ccc}}
</style></head><body>
<h1>Feeder Vegetation Inspection — Ranked Trouble Spots</h1>
<p class="lead">Mescalero West, Phase A. Candidates found by overhead (satellite)
screen, verified and ranked with Street View. Higher score = vegetation closer
to the conductors / more likely high-wind contact point. Investigate from the
top down.</p>
{''.join(cards)}
</body></html>"""

    out_path = os.path.join(OUT, "report.html")
    with open(out_path, "w") as f:
        f.write(doc)
    print(f"Wrote {out_path} with {len(rows)} spots. Open it in a browser or print to PDF.")


if __name__ == "__main__":
    main()
