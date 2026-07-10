# Feeder Vegetation Inspection (KMZ + Google Maps + Claude)

Find where vegetation is most likely contacting an overhead distribution feeder
in high wind — to locate the source of an intermittent ground fault. Built for
the **Mescalero West, Phase A** feeder (21.2 mi: a 14.76 mi ABC trunk + a
6.41 mi Phase-A line + 28 lateral takeoff junctions) in forested mountain
terrain near Mescalero/Ruidoso, NM.

## How it works — 3 phases

```
KMZ (feeder geometry)
   │
   ├─ PHASE 1  overhead screen     satellite tile at every point on trunk +
   │  phase1_overhead_scan.py      offshoots + junctions → Claude scores canopy
   │                               over the corridor → ranked CANDIDATES
   │                               (covers 100% of the line, incl. off-road)
   │
   ├─ PHASE 2  street-view verify   for the worst candidates, pull Street View
   │  phase2_streetview_verify.py   side views → Claude confirms proximity to the
   │                                conductors → COMBINED rank (off-road spans
   │                                kept, flagged satellite-only)
   │
   └─ PHASE 3  report               reverse-geocode addresses + clean satellite
      phase3_build_report.py        close-ups → single HTML report with coords,
                                    addresses, Street View + satellite shots
```

## Before you can run it — prerequisites

**1. Enable three Google APIs** on your key (Google Cloud Console → APIs &
Services → Enable APIs), with billing on:

| API | Used for | Status on your key |
|---|---|---|
| **Street View Static API** | side-view frames (Phase 2) | ✅ already enabled |
| **Maps Static API** | satellite tiles (Phase 1 + report) | ❌ **enable this** |
| **Geocoding API** | street addresses (Phase 3) | ❌ **enable this** |

**2. An Anthropic API key** for the image scoring (Phases 1–2).

**3. Restrict your key.** The key was shared in chat, so anyone who sees that
message can use it. In the Cloud Console, add an **API restriction** (limit it
to the three APIs above) and an **application restriction** (your IP), and
consider **rotating** it when the project is done. These scripts only ever read
the key from the `GOOGLE_MAPS_API_KEY` environment variable — it is never
written to a file or committed.

## Run it

```bash
cd feeder-streetview-inspection
pip install -r requirements.txt
export GOOGLE_MAPS_API_KEY="your-google-key"
export ANTHROPIC_API_KEY="your-anthropic-key"

# optional: confirm what's in the KMZ
python inspect_kmz.py Mescalero_West_Phase_A_Overlay.kmz

# Phase 1 — overhead screen (use --limit 30 for a cheap first test)
python phase1_overhead_scan.py Mescalero_West_Phase_A_Overlay.kmz

# Phase 2 — verify the worst candidates with Street View
python phase2_streetview_verify.py --top 80

# Phase 3 — build the report
python phase3_build_report.py --top 25
```

Everything lands in `out/`. The deliverable is **`out/report.html`** (open in a
browser or print to PDF). `out/overhead_candidates.kml` opens in Google Earth on
your phone so you can see candidates on a map.

## Scoring scale (both phases)

| Score | Overhead (satellite) | Street View (side) |
|------:|----------------------|--------------------|
| 5 | dense canopy directly over the corridor | branches touching/overhanging the lines |
| 4 | canopy encroaching, little clearance | within ~a foot of the conductors |
| 3 | trees close to the corridor edge | within a few feet, growing toward lines |
| 2 | some trees, corridor mostly clear | nearby trees, clear separation |
| 1 | sparse/low vegetation | present but well clear |
| 0 | open/cleared right-of-way | no lines/nothing near them |

## What this can and can't tell you

- **Street View coverage is ~56% of this feeder.** The rest is off-road
  mountain spans. Phase 1 (satellite) covers all of it; Phase 2 verifies only
  where Street View exists. Off-road candidates stay in the report flagged
  *satellite-only* — walk or fly those.
- **Neither view measures true vertical clearance.** Satellite is top-down (sees
  canopy over the corridor, not wire-to-tree gap); Street View shows lean and
  height where it exists. This ranks *likelihood* to prioritize field work — it
  is not a LiDAR clearance survey.
- **Imagery age:** each spot shows the Street View imagery date. Old encroachment
  is a strong lead; recent clearance isn't guaranteed today.

## Rough cost (full 21 mi)

~1,200 satellite tiles ($2/1k ≈ $2.40) + a few hundred Street View frames
($7/1k) + geocoding ($5/1k, tiny) on the Google side — well within Google's
monthly credit — plus a few dollars of Claude scoring.

## Tuning

- `INTERVAL_M` in `phase1_overhead_scan.py` — satellite spacing (default 35 m).
- `ZOOM` — 20 is tight on the corridor; 19 is wider context.
- `--top` / `--min-overhead` on Phases 2–3 — how deep to verify/report.

## Files

| File | Role |
|---|---|
| `gmaps.py` | shared geometry + Google endpoint wrappers |
| `vision.py` | Claude scorers (overhead + street-view rubrics) |
| `inspect_kmz.py` | report what's inside a KMZ |
| `phase1_overhead_scan.py` | Phase 1 — satellite screen → candidates |
| `phase2_streetview_verify.py` | Phase 2 — Street View verify → ranked |
| `phase3_build_report.py` | Phase 3 — HTML report |
| `fetch_streetview.py`, `review_images.py` | simpler "walk the whole line in Street View" alternative |
