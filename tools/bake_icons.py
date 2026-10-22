"""Bake PNG copies of every SVG icon at multiple-of-32 raster resolution.

Run from the package root:
    python tools/bake_icons.py
Requires: pip install cairosvg
"""
from pathlib import Path
import cairosvg

ROOT = Path(__file__).resolve().parents[1]
for svg in sorted((ROOT / "icons").glob("*.svg")):
    target = svg.with_suffix(".png")
    cairosvg.svg2png(url=str(svg), write_to=str(target), output_width=96, output_height=96)
    print(f"{svg.name} -> {target.name}")
