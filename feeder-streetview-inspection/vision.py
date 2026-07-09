#!/usr/bin/env python3
"""
vision.py — Claude image scoring for the feeder inspection pipeline.

Two scorers:
  - score_overhead(): top-down satellite tile -> canopy/vegetation over the corridor
  - score_streetview(): side-view Street View frame -> vegetation near the conductors

Both return a dict: {"score": 0-5, "reason": str, ...}. Uses the Anthropic API
(claude-opus-4-8) and reads ANTHROPIC_API_KEY from the environment.
"""
import base64
import json

import os
import anthropic

MODEL = os.environ.get("VISION_MODEL", "claude-opus-4-8")
FAST_MODEL = "claude-haiku-4-5"   # cheap/fast bulk screen; verify hits with MODEL

OVERHEAD_RUBRIC = """You are looking straight down at a satellite image centered
on an overhead electrical line corridor (the right-of-way for a distribution
feeder) in forested, mountainous terrain. A power line runs through this tile;
you usually cannot see the wire itself from above, but you CAN see the cleared
corridor, the road/track it follows, and the tree canopy around it.

Judge how much tall tree canopy is directly over or pressing into the line
corridor — the kind of vegetation that could contact the conductors and fault
to ground in high wind.

Score 0 to 5:
  5 = dense tall canopy directly over the corridor; trees clearly overhang the route
  4 = canopy encroaching into the corridor from one or both sides, little clearance
  3 = trees close to the corridor edge, moderate encroachment
  2 = some trees nearby but the corridor looks reasonably clear
  1 = sparse/low vegetation, well clear
  0 = open ground, cleared right-of-way, or no vegetation near the corridor"""

STREETVIEW_RUBRIC = """You are inspecting a Google Street View image taken along
an overhead electrical distribution feeder. The goal is to find vegetation that
is TALLER THAN the conductors — trees whose crowns rise to or above the height
of the power lines, close enough to the line that they could fall, lean, or grow
into it and cause a ground fault in high wind.

Find the overhead conductors / the pole crossarm height. Then judge the trees
NEAR the line by HEIGHT relative to the conductors:

Score 0 to 5:
  5 = tree crowns clearly ABOVE the conductors AND directly over/touching the line — overtopping, imminent risk
  4 = trees as tall or taller than the line, right at the line corridor (within a span-width)
  3 = trees reaching close to conductor height beside the line, leaning/growing toward it
  2 = trees present near the line but clearly shorter than the conductors
  1 = only low vegetation / short brush near the line
  0 = no conductors visible, or no trees near the line

Report in 'reason' whether the nearest trees are ABOVE, AT, or BELOW conductor
height, and roughly how far they are from the line."""

COARSE_RUBRIC = """You are looking at a WIDE satellite view (~1 km across) of
terrain in southern New Mexico. An overhead electric line route is drawn on the
image as a bright CYAN line. This is a coarse first-pass screen: decide whether
the drawn line passes through or beside TREE COVER anywhere in this tile —
forest, woodland, tree rows, riparian trees — as opposed to open desert,
grassland, scrub, or bare ground.

Score 0 to 5:
  5 = line runs through dense forest/woodland for much of its path here
  4 = line passes through or directly beside substantial tree cover in places
  3 = scattered trees/woodland patches close to the line
  2 = sparse low shrubs near the line; a few isolated trees not clearly near it
  1 = open desert/grass/scrub along the line; vegetation clearly low
  0 = no drawn line visible, or barren ground

This is a recall pass — when unsure between two scores, pick the higher."""

ROADWALK_RUBRIC = """You are inspecting a Google Street View image taken on a road
in the Santa Cruz Mountains, California — a CPUC High Fire-Threat District. You are
looking for overhead electric DISTRIBUTION lines (wood poles, crossarms, uninsulated
conductors) and judging vegetation clearance against California standards
(CPUC General Order 95, Rule 35): minimum 4 ft radial clearance in HFTD, 12 ft
recommended at time of trim, plus fall-in risk from trees taller than the line.

First determine whether overhead distribution conductors are visible AT ALL.
Ignore service drops to single houses and telecom-only lines where you can tell.

Score 0 to 5:
  5 = vegetation touching/overhanging the conductors, or clearly within ~4 ft (violation-level)
  4 = vegetation within the ~12 ft recommended envelope, or tree crowns at/above conductor height directly beside the line
  3 = trees taller than the line within falling distance, or vegetation growing toward the line
  2 = trees near the corridor but below conductor height and outside the envelope
  1 = only low vegetation near the line
  0 = NO distribution conductors visible in this image, or nothing near them

In 'reason', state whether conductors are visible, and whether the nearest
vegetation is ABOVE, AT, or BELOW conductor height and roughly how far away."""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [0, 1, 2, 3, 4, 5]},
        "conductors_or_corridor_visible": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["score", "conductors_or_corridor_visible", "reason"],
    "additionalProperties": False,
}

_client = None


def _c():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _media_type(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:2] == b"\xff\xd8":
        return "image/jpeg"
    if raw[:4] == b"GIF8":
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _score(img_path, rubric, model=None):
    with open(img_path, "rb") as f:
        raw = f.read()
    media = _media_type(raw)
    data = base64.standard_b64encode(raw).decode("utf-8")
    resp = _c().messages.create(
        model=model or MODEL,
        max_tokens=1024,
        system=rubric,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media, "data": data}},
                {"type": "text", "text": "Score this image per the rubric."},
            ],
        }],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def score_overhead(img_path, model=None):
    return _score(img_path, OVERHEAD_RUBRIC, model)


def score_streetview(img_path, model=None):
    return _score(img_path, STREETVIEW_RUBRIC, model)


def score_roadwalk(img_path, model=None):
    return _score(img_path, ROADWALK_RUBRIC, model)


def score_coarse(img_path, model=None):
    return _score(img_path, COARSE_RUBRIC, model or FAST_MODEL)
