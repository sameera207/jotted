"""reMarkable page geometry, and pages drawn as SVG for the web app.

Page geometry: 468 x 624 pt, the rM2 screen's 3:4 ratio, so one PDF point is
3 reMarkable units. Ink coordinates run x -702..702 (centred) and y 0 down from the
top. The tablet draws a PDF page slightly larger than that: `template.scale` tablet
units per nominal unit, measured on an rM2 (1.0525).
"""

from __future__ import annotations

PAGE_W, PAGE_H = 468.0, 624.0
UNITS_PER_PT = 3.0
RM_W, RM_H = PAGE_W * UNITS_PER_PT, PAGE_H * UNITS_PER_PT  # 1404 x 1872


def render_svg(strokes, scale: float, highlight: list | None = None, crop: tuple | None = None) -> str:
    """A page's ink. `highlight`: boxes (tablet units) to mark, e.g. the line an action came from.
    `crop`: show only this box (tablet units), e.g. an image of one handwritten line."""
    w, h = RM_W * scale, RM_H * scale
    vx, vy, vw, vh = (-w / 2, 0, w, h) if crop is None else (crop[0], crop[1], crop[2] - crop[0], crop[3] - crop[1])
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}">',
           f'<rect x="{vx:.0f}" y="{vy:.0f}" width="{vw:.0f}" height="{vh:.0f}" fill="#fff"/>']
    for b in highlight or []:
        out.append(f'<rect x="{b[0] - 14:.0f}" y="{b[1] - 10:.0f}" width="{b[2] - b[0] + 28:.0f}" '
                   f'height="{b[3] - b[1] + 20:.0f}" rx="10" fill="#ffe066" fill-opacity="0.55"/>')
    for st in strokes:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in st.points)
        out.append(f'<polyline points="{pts}" fill="none" stroke="#1f3f8f" stroke-width="3" '
                   f'stroke-linecap="round" stroke-linejoin="round"/>')
    out.append("</svg>")
    return "\n".join(out)
