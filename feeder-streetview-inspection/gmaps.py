#!/usr/bin/env python3
"""
gmaps.py — shared helpers for the feeder inspection pipeline.

Geometry (KMZ parsing, sampling, bearings) + thin wrappers over the Google
Maps endpoints used across the three phases:
  - Street View Static API + its free metadata endpoint
  - Maps Static API (satellite tiles)
  - Geocoding API (reverse geocode -> street address)

The Google API key is read from GOOGLE_MAPS_API_KEY. It is never written to
disk by these scripts.
"""
import math
import os
import zipfile
import xml.etree.ElementTree as ET

import requests

SV_META_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
SV_IMG_URL = "https://maps.googleapis.com/maps/api/streetview"
STATIC_URL = "https://maps.googleapis.com/maps/api/staticmap"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def key() -> str:
    k = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not k:
        raise SystemExit("Set GOOGLE_MAPS_API_KEY in your environment first.")
    return k


# ---- KML / geometry -------------------------------------------------------

def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_kml(path: str) -> str:
    if path.lower().endswith(".kml"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise SystemExit("No .kml found inside the KMZ archive.")
        name = "doc.kml" if "doc.kml" in names else names[0]
        return z.read(name).decode("utf-8", errors="replace")


def parse_lines(kml: str):
    """List of polylines; each is a list of (lat, lng)."""
    root = ET.fromstring(kml)
    lines = []
    for elem in root.iter():
        if strip_ns(elem.tag) != "coordinates":
            continue
        pts = []
        for tok in (elem.text or "").split():
            parts = tok.split(",")
            if len(parts) < 2:
                continue
            pts.append((float(parts[1]), float(parts[0])))
        if len(pts) >= 2:
            lines.append(pts)
    return lines


def parse_named_points(kml: str):
    """List of (name, lat, lng) for Point placemarks (takeoffs/devices)."""
    root = ET.fromstring(kml)
    out = []
    for pm in root.iter():
        if strip_ns(pm.tag) != "Placemark":
            continue
        pt = pm.find(".//{*}Point/{*}coordinates")
        if pt is None or not (pt.text or "").strip():
            continue
        parts = pt.text.strip().split(",")
        nm = pm.find("./{*}name")
        out.append(((nm.text if nm is not None else "point"),
                    float(parts[1]), float(parts[0])))
    return out


def haversine(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def bearing(a, b):
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlo = math.radians(b[1] - a[1])
    x = math.sin(dlo) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _interp(p, q, frac):
    return (p[0] + (q[0] - p[0]) * frac, p[1] + (q[1] - p[1]) * frac)


def sample_line(line, step_m):
    """Yield (lat, lng, path_bearing) every step_m meters along one polyline."""
    carry = 0.0
    for p, q in zip(line, line[1:]):
        seg = haversine(p, q)
        if seg == 0:
            continue
        brg = bearing(p, q)
        d = carry
        while d < seg:
            lat, lng = _interp(p, q, d / seg)
            yield (lat, lng, brg)
            d += step_m
        carry = d - seg


def total_miles(lines):
    return sum(haversine(p, q) for ln in lines for p, q in zip(ln, ln[1:])) / 1609.34


# ---- Google endpoints -----------------------------------------------------

def sv_metadata(lat, lng):
    j = requests.get(SV_META_URL, params={"location": f"{lat},{lng}", "key": key()},
                     timeout=30).json()
    return j.get("status") == "OK", j.get("date", ""), j.get("pano_id", "")


def fetch_streetview(lat, lng, heading, dest, size="640x640", pitch=10, fov=75):
    r = requests.get(SV_IMG_URL, params={
        "size": size, "location": f"{lat},{lng}", "heading": round(heading),
        "pitch": pitch, "fov": fov, "key": key(), "return_error_code": "true",
    }, timeout=60)
    if r.ok and r.headers.get("content-type", "").startswith("image"):
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    return False


def fetch_satellite(lat, lng, dest, zoom=20, size="640x640", scale=2):
    r = requests.get(STATIC_URL, params={
        "center": f"{lat},{lng}", "zoom": zoom, "size": size,
        "scale": scale, "maptype": "satellite", "key": key(),
    }, timeout=60)
    if r.ok and r.headers.get("content-type", "").startswith("image"):
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    return False


def reverse_geocode(lat, lng):
    try:
        j = requests.get(GEOCODE_URL, params={"latlng": f"{lat},{lng}", "key": key()},
                         timeout=30).json()
        if j.get("status") == "OK" and j.get("results"):
            return j["results"][0]["formatted_address"]
    except Exception:
        pass
    return ""
