#!/usr/bin/env python3
"""
inspect_kmz.py — tell me what's actually inside a KMZ/KML.

Run this FIRST so you know whether your file contains the feeder as a line
(great — the fetcher can walk it) or only as a picture overlay (you'll need to
trace the centerline). Usage:

    python inspect_kmz.py feeder.kmz
"""
import sys
import zipfile
import xml.etree.ElementTree as ET


def strip_ns(tag: str) -> str:
    """'{http://www.opengis.net/kml/2.2}LineString' -> 'LineString'."""
    return tag.rsplit("}", 1)[-1]


def load_kml(path: str) -> str:
    if path.lower().endswith(".kml"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    with zipfile.ZipFile(path) as z:
        # The main doc is usually doc.kml; fall back to the first .kml entry.
        names = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise SystemExit("No .kml found inside the KMZ archive.")
        name = "doc.kml" if "doc.kml" in names else names[0]
        return z.read(name).decode("utf-8", errors="replace")


def walk(elem, counts):
    tag = strip_ns(elem.tag)
    counts[tag] = counts.get(tag, 0) + 1
    for child in elem:
        walk(child, counts)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python inspect_kmz.py <file.kmz|file.kml>")
    path = sys.argv[1]
    kml = load_kml(path)
    root = ET.fromstring(kml)

    counts = {}
    walk(root, counts)

    print(f"\nElements found in {path}:")
    for tag in sorted(counts):
        print(f"  {tag:24} x{counts[tag]}")

    line_count = counts.get("LineString", 0)
    overlay_count = counts.get("GroundOverlay", 0)
    point_count = counts.get("Point", 0)

    print("\nVerdict:")
    if line_count:
        print(f"  ✔ {line_count} LineString(s) — the fetcher can walk the feeder route directly.")
    if point_count and not line_count:
        print(f"  • {point_count} Point(s) but no line — fetcher will sample the points, "
              "but coverage between points won't be filled in.")
    if overlay_count and not line_count:
        print(f"  ⚠ {overlay_count} GroundOverlay (image overlay) and NO line.")
        print("    Street View needs a path to walk. Open the KMZ in Google Earth,")
        print("    trace the feeder centerline as a new Path, and save it as a KMZ.")
    if not (line_count or point_count or overlay_count):
        print("  ? No lines, points, or overlays recognized — open the KML and check its structure.")
    print()


if __name__ == "__main__":
    main()
