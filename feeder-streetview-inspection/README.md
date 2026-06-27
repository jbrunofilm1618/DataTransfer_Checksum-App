# Feeder Street View Vegetation Inspection

Find spots where trees/vegetation are close to an overhead electrical feeder by
walking a route from a KMZ file through Google Street View and having Claude
look at every frame. Built to help locate the likely source of an intermittent
ground fault on a long feeder run.

```
KMZ (feeder route)  →  fetch_streetview.py  →  hundreds of images + manifest.csv
                                                        │
                                          review_images.py (Claude vision)
                                                        │
                                   review.csv (ranked)  +  hotspots.kml (map)
```

## One-time setup

You need Python 3.10+ and two API keys.

1. **Google Maps key** (for the images):
   - Go to the Google Cloud Console → create a project → enable **billing**.
   - APIs & Services → enable the **Street View Static API**.
   - Credentials → create an **API key**.
2. **Anthropic key** (for the image review): from the Anthropic Console.

Then install the dependencies:

```bash
cd feeder-streetview-inspection
pip install -r requirements.txt
export GOOGLE_MAPS_API_KEY="your-google-key"
export ANTHROPIC_API_KEY="your-anthropic-key"
```

## Run it

**Step 0 — check your KMZ** (what's inside it?):

```bash
python inspect_kmz.py feeder.kmz
```

- If it reports **LineString(s)**, you're good — that's the feeder route.
- If it's only a **GroundOverlay** (a picture), Street View has nothing to walk.
  Open the KMZ in Google Earth, trace the feeder centerline as a **Path**, and
  save that as a new KMZ.

**Step 1 — pull the Street View frames:**

```bash
python fetch_streetview.py feeder.kmz
```

Walks the line every 40 m, checks the free metadata endpoint, and downloads two
images per point (one each side). Safe to re-run — it resumes. Output lands in
`streetview_out/`.

**Step 2 — score and rank with Claude:**

```bash
python review_images.py            # full run
python review_images.py --limit 20 # cheap test on the first 20 frames
```

Produces:
- `streetview_out/review.csv` — every frame, scored 0–5, sorted worst-first.
- `streetview_out/hotspots.kml` — the high-risk locations. **Open this in
  Google Earth (works on your phone)** and each pin drops you on a spot to go
  inspect, with Claude's note and the imagery date.

## What the score means

| Score | Meaning |
|------:|---------|
| 5 | Branches touching / overhanging the conductors — imminent fault risk |
| 4 | Vegetation within ~a foot of the conductors |
| 3 | Within a few feet, growing toward the lines |
| 2 | Nearby trees, clear separation |
| 1 | Vegetation present, well clear |
| 0 | No lines visible, or nothing near them |

Start your field search at the score-5 and score-4 locations.

## Tuning

In `fetch_streetview.py`:
- `INTERVAL_M` — sampling spacing (smaller = more coverage, more images/cost).
- `FOV` — lower zooms in tighter on the lines; `PITCH` tilts the camera up.

In `review_images.py`:
- `--min-score` — threshold for what counts as a hotspot in the KML (default 3).

## Two things to keep in mind

- **Off-road spans:** Street View only exists along roads. Points with no
  imagery are skipped for free; you'll need aerial/satellite or a field walk for
  any cross-country segments.
- **Imagery age:** `review.csv` and each KML pin show the imagery date.
  Vegetation grows, so a spot already encroaching in an old image is a strong
  lead, but recent clearance isn't guaranteed today.

## Rough cost

12 miles at 40 m spacing ≈ 480 points → up to ~960 images. Street View Static
is $7 / 1,000 images (metadata checks are free), and Google's monthly credit
usually covers it. The Claude review is a few dollars on top.
