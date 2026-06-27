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

import anthropic

MODEL = "claude-opus-4-8"

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
an overhead electrical distribution feeder, looking for vegetation that could
cause an intermittent ground fault by contacting or nearly contacting the lines
in high wind.

Find the overhead conductors and poles. Judge how close trees/branches are to
the PRIMARY conductors.

Score 0 to 5:
  5 = branches/foliage touching or overhanging the conductors (imminent risk)
  4 = vegetation within roughly a foot of the conductors
  3 = within a few feet; growing toward the lines
  2 = nearby trees, clear separation
  1 = vegetation present, well clear
  0 = no conductors visible, or nothing near them"""

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


def _score(img_path, rubric):
    with open(img_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    resp = _c().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=rubric,
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
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def score_overhead(img_path):
    return _score(img_path, OVERHEAD_RUBRIC)


def score_streetview(img_path):
    return _score(img_path, STREETVIEW_RUBRIC)
