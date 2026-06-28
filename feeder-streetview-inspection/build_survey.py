#!/usr/bin/env python3
"""
build_survey.py — assemble the field survey document from junctions.

Fetches HIGH-RESOLUTION satellite (context z19 + close-up z21, 1280 px) and
Street View (where available) for every takeoff junction in the KMZ, reverse-
geocodes each to a street address, and builds:
  out/survey_document.html        full-res, self-contained (large)
  out/survey_document_lean.html   downscaled embeds, emailable
  out/Mescalero..._Survey.pdf     printable (if Chromium is found)
  out/hires.zip                   the full-res tiles

Risk scores come from out/verified.csv (Phase 2 output) when present; otherwise
junctions are listed as "screen pending". A SCORES.json override may also be
supplied: {"J27": [4, "note"], ...}.

    export GOOGLE_MAPS_API_KEY=...     # Maps Static + Geocoding enabled
    python build_survey.py "Mescalero_West_Phase_A_Overlay.kmz" --pdf
"""
import argparse
import base64
import csv
import glob
import html
import io
import json
import os
import subprocess
import zipfile

import gmaps

OUT = "out"
HIRES = os.path.join(OUT, "overlay")   # satellite tiles with the feeder line drawn on
SV_DIR = os.path.join(OUT, "streetview")
PDF_NAME = "Mescalero_West_PhaseA_Survey.pdf"

CHROME_CANDIDATES = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "chromium", "chromium-browser", "google-chrome",
]


def load_scores():
    scores = {}
    vp = os.path.join(OUT, "verified.csv")
    if os.path.exists(vp):
        for r in csv.DictReader(open(vp)):
            scores[r["id"]] = (int(r["combined"]),
                               (r.get("sv_reason") or r.get("overhead_reason") or "").strip())
    sp = os.path.join(OUT, "SCORES.json")
    if os.path.exists(sp):
        for k, v in json.load(open(sp)).items():
            scores[k] = (int(v[0]), v[1])
    return scores


def b64(path, max_w=None, q=82):
    if not path or not os.path.exists(path):
        return ""
    if max_w:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=q)
        data = buf.getvalue()
    else:
        data = open(path, "rb").read()
    return "data:image/jpeg;base64," + base64.standard_b64encode(data).decode()


def fetch_assets(kmz):
    os.makedirs(HIRES, exist_ok=True)
    os.makedirs(SV_DIR, exist_ok=True)
    kml = gmaps.load_kml(kmz)
    lines = gmaps.parse_lines(kml)
    tk = gmaps.parse_named_points(kml)
    rows = []
    for i, (nm, lat, lng) in enumerate(tk):
        jid = f"J{i:02d}"
        if not os.path.exists(f"{HIRES}/{jid}_z19.jpg"):
            gmaps.fetch_satellite_overlay(lat, lng, lines, f"{HIRES}/{jid}_z19.jpg", zoom=19, radius_m=150)
        if not os.path.exists(f"{HIRES}/{jid}_z21.jpg"):
            gmaps.fetch_satellite_overlay(lat, lng, lines, f"{HIRES}/{jid}_z21.jpg", zoom=21, radius_m=45, weight=5)
        ok, _, _ = gmaps.sv_metadata(lat, lng)
        if ok and not glob.glob(f"{SV_DIR}/{jid}_*.jpg"):
            for side, h in (("L", 0), ("R", 90), ("B", 180), ("R2", 270)):
                gmaps.fetch_streetview(lat, lng, h, f"{SV_DIR}/{jid}_{side}.jpg")
        rows.append((jid, nm, lat, lng))
    with open(os.path.join(OUT, "junctions.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "name", "lat", "lng"]); w.writerows(rows)
    return rows


def _sv_files(jid):
    """Prefer along-line frames (best for judging tree height vs. the line)."""
    along = sorted(glob.glob(os.path.join(OUT, "sv_along", f"{jid}_*.jpg")))
    return along[:2] if along else sorted(glob.glob(f"{SV_DIR}/{jid}_*.jpg"))[:2]


def build_html(rows, scores, lean, min_score=None):
    rows = sorted(rows, key=lambda r: (0, -scores[r[0]][0]) if r[0] in scores else (1, 0))
    if min_score is not None:
        rows = [r for r in rows if r[0] in scores and scores[r[0]][0] >= min_score]
    max_w = 820 if lean else None
    scored_n = 0
    cards = []
    for jid, nm, lat, lng in rows:
        addr = gmaps.reverse_geocode(lat, lng) or "(no street address — remote/off-road)"
        z19 = b64(f"{HIRES}/{jid}_z19.jpg", max_w)
        z21 = b64(f"{HIRES}/{jid}_z21.jpg", max_w)
        svs = [b64(f, max_w) for f in _sv_files(jid)]
        if jid in scores:
            scored_n += 1
            badge = f'<span class=score>risk {scores[jid][0]}/5</span>'
            note = scores[jid][1] or ""
        else:
            badge = '<span class=pending>screen pending</span>'
            note = "High-res imagery captured; automated screen pending."
        link = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
        sv_html = "".join(f'<img src="{s}">' for s in svs if s) or '<span class=na>no Street View (satellite-only)</span>'
        cards.append(f"""<div class=card>
 <h2>{html.escape(nm)} &nbsp;{badge}</h2>
 <p class=meta>{lat:.5f}, {lng:.5f} &middot; <a href="{link}">Google Maps</a> &middot; <b>{html.escape(addr)}</b></p>
 <p class=note>{html.escape(note)}</p>
 <div class=imgs>
  <figure><figcaption>Satellite context</figcaption><img src="{z19}"></figure>
  <figure><figcaption>Satellite close-up</figcaption><img src="{z21}"></figure>
  <figure><figcaption>Street View</figcaption>{sv_html}</figure>
 </div></div>""")
    style = """body{font-family:system-ui,Arial,sans-serif;margin:20px;color:#1a1a1a}
h1{margin-bottom:2px}.lead{color:#555;margin-top:0;max-width:62em;font-size:.95em}
.card{border:1px solid #ddd;border-radius:8px;padding:12px;margin:14px 0;break-inside:avoid}
.card h2{margin:0 0 5px;font-size:1em}
.score{color:#fff;background:#b00;padding:2px 8px;border-radius:10px;font-size:.78em}
.pending{color:#555;background:#eee;padding:2px 8px;border-radius:10px;font-size:.78em}
.meta,.note{font-size:.85em;margin:3px 0}.na{color:#888;font-size:.8em}
.imgs{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
figure{margin:0}figcaption{font-size:.75em;color:#666;margin-bottom:2px}
img{max-width:300px;width:100%;border-radius:5px;border:1px solid #ccc}"""
    if min_score is not None:
        title = "Likely Problem Areas — Vegetation Too Close to the Feeder"
        lead = (f"Mescalero West, Phase A. Filtered to the {len(rows)} location(s) scored "
                f"{min_score}/5 or higher — where vegetation is at/above conductor height and "
                f"close to the line. Feeder route drawn (cyan) on each satellite image with a "
                f"red junction marker; Street View aimed along the line. Coordinates and street "
                f"addresses included. Work these top-down. (Junctions not yet screened are "
                f"excluded; more may be added as screening completes.)")
    else:
        title = "Vegetation Proximity Survey"
        lead = (f"21.2-mi feeder, {len(rows)} lateral takeoff junctions. High-res satellite "
                f"(line overlaid) and Street View where available, with coordinates and "
                f"reverse-geocoded addresses, ranked by vegetation proximity. {scored_n}/{len(rows)} "
                f"screened. Method ranks <i>likelihood</i> for field prioritization — not a LiDAR survey.")
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Mescalero West Phase A — {html.escape(title)}</title><style>{style}</style></head><body>
<h1>Mescalero West, Phase A — {title}</h1>
<p class=lead>{lead}</p>
{''.join(cards)}</body></html>"""


def render_pdf(html_path, pdf_name):
    chrome = next((c for c in CHROME_CANDIDATES
                   if os.path.exists(c) or _on_path(c)), None)
    if not chrome:
        print("No Chromium found — skipping PDF.")
        return
    pdf = os.path.join(OUT, pdf_name)
    subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", f"file://{os.path.abspath(html_path)}"],
                   check=False, capture_output=True)
    if os.path.exists(pdf):
        print(f"Wrote {pdf}")


def _on_path(name):
    from shutil import which
    return which(name) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kmz")
    ap.add_argument("--pdf", action="store_true", help="also render a PDF via Chromium")
    ap.add_argument("--min-score", type=int, default=3,
                    help="threshold for the problem-areas-only document (default 3)")
    args = ap.parse_args()
    gmaps.key()
    os.makedirs(OUT, exist_ok=True)

    rows = fetch_assets(args.kmz)
    scores = load_scores()

    # Full survey (every junction)
    open(os.path.join(OUT, "survey_document.html"), "w").write(build_html(rows, scores, lean=False))
    lean_path = os.path.join(OUT, "survey_document_lean.html")
    open(lean_path, "w").write(build_html(rows, scores, lean=True))
    print("Wrote survey_document.html and survey_document_lean.html")

    # Problem-areas-only document (score >= min_score)
    prob_path = os.path.join(OUT, "problem_areas.html")
    open(prob_path, "w").write(build_html(rows, scores, lean=True, min_score=args.min_score))
    print(f"Wrote problem_areas.html (score >= {args.min_score})")

    with zipfile.ZipFile(os.path.join(OUT, "hires.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for f in glob.glob(f"{HIRES}/*.jpg"):
            z.write(f, os.path.relpath(f, OUT))
    print("Wrote hires.zip")

    if args.pdf:
        render_pdf(lean_path, PDF_NAME)
        render_pdf(prob_path, "Mescalero_West_PhaseA_Problem_Areas.pdf")


if __name__ == "__main__":
    main()
