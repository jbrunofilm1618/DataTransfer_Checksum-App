#!/usr/bin/env python3
"""
build_master.py — ONE master document of the highly-likely trouble areas.

Merges junction problems (out/SCORES.json) and mid-span problems
(out/midspan_problems.csv), ranks by score, de-dupes nearby points, and emits a
single HTML + PDF with, per location: rank, score, validation flag, coordinates,
street address, Google Maps link, and hi-res photos (line-overlaid satellite +
Street View) for visual validation.

    export GOOGLE_MAPS_API_KEY=...
    python build_master.py "Mescalero_West_Phase_A_Overlay.kmz" --min 4 --pdf
"""
import argparse, base64, csv, glob, html, io, json, os, subprocess
import gmaps

OUT = "out"
TITLE = "Mescalero West, Phase A"
PDF_NAME = "Mescalero_West_PhaseA_MASTER_Trouble_Areas.pdf"
CHROME = ["/opt/pw-browsers/chromium-1194/chrome-linux/chrome", "chromium", "google-chrome"]


def b64(path, max_w=900, q=82):
    if not path or not os.path.exists(path):
        return ""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=q)
    return "data:image/jpeg;base64," + base64.standard_b64encode(buf.getvalue()).decode()


def junction_items(kmz, min_score):
    if not os.path.exists(f"{OUT}/SCORES.json") or not os.path.exists(f"{OUT}/junctions.csv"):
        return []
    scores = json.load(open(f"{OUT}/SCORES.json"))
    jrows = {r["id"]: r for r in csv.DictReader(open(f"{OUT}/junctions.csv"))}
    items = []
    for jid, (sc, reason) in scores.items():
        if sc < min_score or jid not in jrows:
            continue
        r = jrows[jid]
        imgs = [p for p in (f"{OUT}/overlay/{jid}_z19.jpg", f"{OUT}/overlay/{jid}_z21.jpg") if os.path.exists(p)]
        imgs += sorted(glob.glob(f"{OUT}/sv_along/{jid}_*.jpg"))
        has_sv = bool(glob.glob(f"{OUT}/sv_along/{jid}_*.jpg"))
        items.append(dict(kind="Junction", label=r["name"], lat=float(r["lat"]),
                          lng=float(r["lng"]), score=int(sc), reason=reason,
                          imgs=imgs, has_sv=has_sv))
    return items


def midspan_items(min_score):
    p = f"{OUT}/midspan_problems.csv"
    if not os.path.exists(p):
        return []
    items = []
    for r in csv.DictReader(open(p)):
        if int(r["combined"]) < min_score:
            continue
        mid = r["id"]
        imgs = [x for x in (f"{OUT}/midspan/{mid}.jpg", f"{OUT}/midspan/{mid}_sv.jpg") if os.path.exists(x)]
        items.append(dict(kind="Mid-span", label=f"Mid-span {mid}", lat=float(r["lat"]),
                          lng=float(r["lng"]), score=int(r["combined"]), reason=r.get("reason", ""),
                          imgs=imgs, has_sv=(r.get("has_sv") == "True")))
    return items


def dedupe(items, radius_m=40):
    items.sort(key=lambda x: -x["score"])
    kept = []
    for it in items:
        if any(gmaps.haversine((it["lat"], it["lng"]), (k["lat"], k["lng"])) < radius_m for k in kept):
            continue
        kept.append(it)
    return kept


def main():
    global OUT, PDF_NAME, TITLE
    ap = argparse.ArgumentParser()
    ap.add_argument("kmz", nargs="?", help="optional — omit for road-walk (no junction) runs")
    ap.add_argument("--dir", default="out", help="results directory (default out)")
    ap.add_argument("--title", default="Mescalero West, Phase A")
    ap.add_argument("--min", type=int, default=4, help="include locations at/above this score (default 4)")
    ap.add_argument("--pdf", action="store_true")
    args = ap.parse_args()
    gmaps.key()
    OUT = args.dir
    TITLE = args.title
    PDF_NAME = args.title.replace(",", "").replace(" ", "_") + "_MASTER_Trouble_Areas.pdf"

    items = junction_items(args.kmz, args.min) if args.kmz else []
    items = dedupe(items + midspan_items(args.min))
    cards = []
    for rank, it in enumerate(items, 1):
        addr = gmaps.reverse_geocode(it["lat"], it["lng"]) or "(no street address — remote/off-road)"
        if it["has_sv"]:
            val = '<span class=ok>✓ Street View available — validate height/clearance in the photos</span>'
        else:
            val = '<span class=warn>⚠ Overhead-only — no Street View; confirm by field/drone</span>'
        imgs = "".join(f'<img src="{b64(p)}">' for p in it["imgs"][:4])
        link = f"https://www.google.com/maps/search/?api=1&query={it['lat']},{it['lng']}"
        cards.append(f"""<div class=card>
 <h2>#{rank} &nbsp;<span class=score>{it['score']}/5</span> &nbsp;{html.escape(it['label'])}
   <span class=kind>{it['kind']}</span></h2>
 <p class=meta>{it['lat']:.5f}, {it['lng']:.5f} &middot; <a href="{link}">Google Maps</a> &middot; <b>{html.escape(addr)}</b></p>
 <p class=val>{val}</p>
 <p class=note>{html.escape(it['reason'])}</p>
 <div class=imgs>{imgs or '(no imagery)'}</div></div>""")

    n_sv = sum(1 for it in items if it["has_sv"])
    style = """body{font-family:system-ui,Arial,sans-serif;margin:22px;color:#1a1a1a}
h1{margin-bottom:2px}.lead{color:#555;margin-top:0;max-width:64em;font-size:.95em}
.card{border:1px solid #ccc;border-radius:9px;padding:13px;margin:15px 0;break-inside:avoid}
.card h2{margin:0 0 5px;font-size:1.02em}
.score{color:#fff;background:#b00;padding:1px 8px;border-radius:9px}
.kind{font-size:.72em;color:#777;border:1px solid #ddd;border-radius:8px;padding:1px 7px;margin-left:6px}
.meta,.note,.val{font-size:.86em;margin:3px 0}.ok{color:#176d2c}.warn{color:#9a6a00}
.imgs{display:flex;gap:9px;flex-wrap:wrap;margin-top:6px}
img{max-width:330px;width:100%;border-radius:5px;border:1px solid #ccc}"""
    doc = f"""<!doctype html><html><head><meta charset=utf-8>
<title>{html.escape(TITLE)} — Master Trouble Areas</title><style>{style}</style></head><body>
<h1>{html.escape(TITLE)} — Highly-Likely Vegetation Trouble Areas</h1>
<p class=lead>Master list for the intermittent ground-fault search. {len(items)} location(s)
scored {args.min}/5 or higher across the surveyed area — ranked
worst-first, de-duplicated. Each has the feeder line drawn (cyan) on a high-res satellite
image plus Street View where it exists, with coordinates and street address for the crew.
{n_sv} are Street-View-validatable; the rest are remote (overhead-only) and flagged for a
field/drone check. Ranks <i>likelihood</i> — not a LiDAR clearance survey.</p>
{''.join(cards)}</body></html>"""
    path = os.path.join(OUT, "MASTER_trouble_areas.html")
    open(path, "w").write(doc)
    print(f"Wrote {path} — {len(items)} locations (score >= {args.min}).")

    if args.pdf:
        chrome = next((c for c in CHROME if os.path.exists(c) or __import__("shutil").which(c)), None)
        if chrome:
            subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu",
                            "--no-pdf-header-footer", f"--print-to-pdf={OUT}/{PDF_NAME}",
                            f"file://{os.path.abspath(path)}"], check=False, capture_output=True)
            print(f"Wrote {OUT}/{PDF_NAME}")


if __name__ == "__main__":
    main()
