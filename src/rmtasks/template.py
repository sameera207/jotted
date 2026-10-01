"""The Tasks notebook as a PDF template: zones, and web changes printed under the ink.

Page geometry: 468 x 624 pt, the rM2 screen's 3:4 ratio, so one PDF point is
3 reMarkable units. Annotation coordinates run x -702..702 (centred) and y 0 down
from the top; `to_pdf` maps them to reportlab's bottom-left origin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.pdfgen.canvas import Canvas

from .config import TemplateConfig

PAGE_W, PAGE_H = 468.0, 624.0
UNITS_PER_PT = 3.0
RM_W, RM_H = PAGE_W * UNITS_PER_PT, PAGE_H * UNITS_PER_PT  # 1404 x 1872

INK = Color(0.15, 0.15, 0.15)
GUIDE = Color(0.55, 0.55, 0.55)
RULE = Color(0.85, 0.85, 0.85)
STRIKE = Color(0.1, 0.1, 0.1)
MOVED = Color(0.35, 0.35, 0.35)
MARGIN = 24.0  # pt

# Calibration marks for test W2, in reMarkable units: short segments to trace over.
CALIBRATION = [(-500.0, 445.0), (300.0, 445.0), (-500.0, 1118.0), (300.0, 1118.0)]  # between rules
CAL_LEN = 150.0  # units


def to_pdf(x: float, y: float, scale: float = 1.0) -> tuple[float, float]:
    """Annotation coordinates (tablet units) to PDF points (bottom-left origin).

    `scale` is tablet units per nominal unit: the tablet draws a PDF page slightly larger
    than 3 units per point. Measured on an rM2 (Sep 29): 1.0525, from the top-left origin,
    with under 1 unit of error left over.
    """
    x, y = x / scale, y / scale
    return (x + RM_W / 2) / UNITS_PER_PT, PAGE_H - y / UNITS_PER_PT


@dataclass(frozen=True)
class Zones:
    """Zone boundaries in reMarkable y units (0 at the top)."""
    header_bottom: float
    footer_top: float

    def of(self, y: float) -> str:
        if y < self.header_bottom:
            return "header"
        if y >= self.footer_top:
            return "footer"
        return "body"


def zones(cfg: TemplateConfig) -> Zones:
    """Zone boundaries in tablet units, as the annotations report them."""
    return Zones(header_bottom=cfg.header_height * RM_H * cfg.scale,
                 footer_top=(1 - cfg.footer_height) * RM_H * cfg.scale)


@dataclass
class PageState:
    """What to print on one page, beyond the blank template."""
    strikes: list[tuple[float, float, float]] = field(default_factory=list)  # done: (x0, x1, y) in rM units
    footer: list[str | tuple] = field(default_factory=list)  # text, (text, done) or (text, done, label)
    moved: list[tuple[list[tuple[float, float, float, float]], int]] = field(default_factory=list)
    # edited on the web: (row boxes in rM units, label); outlined and numbered, pointing to the footer
    calibration: bool = False


def _badge(c: Canvas, cx: float, cy: float, label: int) -> None:
    """A small circled number, linking a replaced line to its new version in the footer."""
    c.setStrokeColor(MOVED)
    c.setFillColor(MOVED)
    c.setLineWidth(0.9)
    c.circle(cx, cy, 6.5, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(cx, cy - 2.8, str(label))


def build(path: Path, cfg: TemplateConfig, pages: dict[int, PageState] | None = None, page_count: int | None = None) -> Path:
    """Write the template PDF. `pages` maps 1-based page numbers to what to print on them."""
    pages = pages or {}
    z = zones(TemplateConfig(**{**cfg.__dict__, "scale": 1.0}))  # drawing works in nominal units
    count = page_count or cfg.pages
    c = Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(path.stem)
    for n in range(1, count + 1):
        _draw_page(c, z, cfg, pages.get(n, PageState()))
        c.showPage()
    c.save()
    return path


def _hline(c: Canvas, y_rm: float, x0_pt: float = MARGIN, x1_pt: float = PAGE_W - MARGIN) -> None:
    y = PAGE_H - y_rm / UNITS_PER_PT
    c.line(x0_pt, y, x1_pt, y)


def _draw_page(c: Canvas, z: Zones, cfg: TemplateConfig, state: PageState) -> None:
    header_y = PAGE_H - z.header_bottom / UNITS_PER_PT
    footer_y = PAGE_H - z.footer_top / UNITS_PER_PT

    # Header: a date label and a divider.
    c.setFillColor(GUIDE)
    c.setFont("Helvetica", 10)
    c.drawString(MARGIN, header_y + 12, "Date:")
    c.setStrokeColor(GUIDE)
    c.setLineWidth(0.8)
    _hline(c, z.header_bottom)

    # Body: optional faint ruling.
    if cfg.line_spacing > 0:
        c.setStrokeColor(RULE)
        c.setLineWidth(0.4)
        step = cfg.line_spacing * RM_H
        y = z.header_bottom + step
        while y < z.footer_top - step / 2:
            _hline(c, y)
            y += step

    # Footer: a divider, a label, and web tasks.
    c.setStrokeColor(GUIDE)
    c.setLineWidth(0.8)
    _hline(c, z.footer_top)
    c.setFillColor(GUIDE)
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, footer_y - 14, "From web")
    c.setFillColor(INK)
    c.setFont("Helvetica", 12)
    row_y = footer_y - 34
    labelled = any(not isinstance(i, str) and len(i) > 2 and i[2] for i in state.footer)
    for item in state.footer:
        text, done, label = (item, False, None) if isinstance(item, str) else (*item, None)[:3]
        if row_y < MARGIN:
            break  # overflow is a deferred edge case
        x = MARGIN
        if labelled:  # keep every row aligned whether or not it carries a number
            if label:
                _badge(c, x + 6.5, row_y + 2.5, label)
            x += 18
        c.setStrokeColor(INK)
        c.setFillColor(INK)
        c.setFont("Helvetica", 12)
        c.setLineWidth(0.8)
        c.rect(x, row_y - 2, 9, 9, stroke=1, fill=0)
        c.drawString(x + 16, row_y, text)
        if done:
            width = c.stringWidth(text, "Helvetica", 12)
            c.setLineWidth(cfg.strike_width)
            c.line(x + 14, row_y + 4, x + 18 + width, row_y + 4)
        row_y -= 20

    # Edited on the web: the handwriting is outlined, not struck, with the number of its new version.
    for rows, label in state.moved:
        x0 = min(r[0] for r in rows)
        pts = [to_pdf(r[0], r[1], cfg.scale) for r in rows] + [to_pdf(r[2], r[3], cfg.scale) for r in rows]
        left, right = min(p[0] for p in pts) - 5, max(p[0] for p in pts) + 5
        bottom, top = min(p[1] for p in pts) - 4, max(p[1] for p in pts) + 4
        c.setStrokeColor(MOVED)
        c.setLineWidth(0.8)
        c.setDash(2, 2)
        c.roundRect(left, bottom, right - left, top - bottom, 4, stroke=1, fill=0)
        c.setDash()
        first_y = (to_pdf(x0, rows[0][1], cfg.scale)[1] + to_pdf(x0, rows[0][3], cfg.scale)[1]) / 2
        bx = left - 10 if left - 10 > 8 else right + 10  # the margin, or after the line if there is no room
        _badge(c, bx, first_y, label)

    # Done: a line through the handwriting.
    c.setStrokeColor(STRIKE)
    c.setLineWidth(cfg.strike_width)
    for x0, x1, y in state.strikes:
        (px0, py), (px1, _) = to_pdf(x0, y, cfg.scale), to_pdf(x1, y, cfg.scale)
        c.line(px0, py, px1, py)

    if state.calibration:  # marks are placed in nominal units, so tracing them measures the scale
        c.setStrokeColor(Color(0.8, 0.1, 0.1))
        c.setFillColor(Color(0.8, 0.1, 0.1))
        c.setLineWidth(1)
        c.setFont("Helvetica", 7)
        for x, y in CALIBRATION:
            (px0, py), (px1, _) = to_pdf(x, y), to_pdf(x + CAL_LEN, y)
            c.line(px0, py, px1, py)
            c.drawString(px0, py + 4, "trace this line")


# ---------------------------------------------------------------- preview (SVG)
# What the tablet shows: the template, your ink, and everything we print, in tablet
# units. Used by the web app; mirrors _draw_page, so keep the two in step.

def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(strokes, state: PageState, cfg: TemplateConfig, templated: bool = True,
               highlight: list | None = None, crop: tuple | None = None) -> str:
    """`highlight`: boxes (tablet units) to mark, e.g. the line an action came from.
    `crop`: show only this box (tablet units), e.g. an image of one handwritten line."""
    s = cfg.scale
    u = UNITS_PER_PT * s  # tablet units per PDF point

    def X(pt: float) -> float:  # PDF x (points) -> tablet x
        return pt * u - RM_W / 2 * s

    def Y(pt: float) -> float:  # PDF y (points, from the bottom) -> tablet y
        return (PAGE_H - pt) * u

    w, h = RM_W * s, RM_H * s
    vx, vy, vw, vh = (-w / 2, 0, w, h) if crop is None else (crop[0], crop[1], crop[2] - crop[0], crop[3] - crop[1])
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}" '
           f'font-family="Helvetica, Arial, sans-serif">',
           f'<rect x="{vx:.0f}" y="{vy:.0f}" width="{vw:.0f}" height="{vh:.0f}" fill="#fff"/>']
    for b in highlight or []:
        out.append(f'<rect x="{b[0] - 14:.0f}" y="{b[1] - 10:.0f}" width="{b[2] - b[0] + 28:.0f}" '
                   f'height="{b[3] - b[1] + 20:.0f}" rx="10" fill="#ffe066" fill-opacity="0.55"/>')
    left, right = X(MARGIN), X(PAGE_W - MARGIN)
    if templated:
        z = zones(cfg)
        out.append(f'<text x="{left:.0f}" y="{z.header_bottom - 12 * u:.0f}" font-size="{10 * u:.0f}" '
                   f'fill="#8c8c8c">Date:</text>')
        if cfg.line_spacing > 0:
            step = cfg.line_spacing * RM_H * s
            y = z.header_bottom + step
            while y < z.footer_top - step / 2:
                out.append(f'<line x1="{left:.0f}" y1="{y:.0f}" x2="{right:.0f}" y2="{y:.0f}" '
                           f'stroke="#d9d9d9" stroke-width="{0.4 * u:.1f}"/>')
                y += step
        for y in (z.header_bottom, z.footer_top):
            out.append(f'<line x1="{left:.0f}" y1="{y:.0f}" x2="{right:.0f}" y2="{y:.0f}" '
                       f'stroke="#8c8c8c" stroke-width="{0.8 * u:.1f}"/>')
        footer_pt = PAGE_H - z.footer_top / u
        out.append(f'<text x="{left:.0f}" y="{Y(footer_pt - 14):.0f}" font-size="{9 * u:.0f}" '
                   f'fill="#8c8c8c">From web</text>')
        labelled = any(not isinstance(i, str) and len(i) > 2 and i[2] for i in state.footer)
        row = footer_pt - 34
        for item in state.footer:
            text, done, label = (item, False, None) if isinstance(item, str) else (*item, None)[:3]
            if row < MARGIN:
                break
            x = MARGIN
            if labelled:
                if label:
                    out.append(_svg_badge(X(x + 6.5), Y(row + 2.5), label, u))
                x += 18
            out.append(f'<rect x="{X(x):.0f}" y="{Y(row + 7):.0f}" width="{9 * u:.0f}" height="{9 * u:.0f}" '
                       f'fill="none" stroke="#262626" stroke-width="{0.8 * u:.1f}"/>')
            out.append(f'<text x="{X(x + 16):.0f}" y="{Y(row):.0f}" font-size="{12 * u:.0f}" fill="#262626"'
                       + (' text-decoration="line-through"' if done else "") + f'>{_xml(text)}</text>')
            row -= 20

    for st in strokes:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in st.points)
        out.append(f'<polyline points="{pts}" fill="none" stroke="#1f3f8f" stroke-width="3" '
                   f'stroke-linecap="round" stroke-linejoin="round"/>')

    for x0, x1, y in state.strikes:
        out.append(f'<line x1="{x0:.0f}" y1="{y:.0f}" x2="{x1:.0f}" y2="{y:.0f}" stroke="#1a1a1a" '
                   f'stroke-width="{cfg.strike_width * u:.1f}"/>')
    for rows, label in state.moved:
        x0 = min(r[0] for r in rows) - 5 * u
        x1 = max(r[2] for r in rows) + 5 * u
        y0 = min(r[1] for r in rows) - 4 * u
        y1 = max(r[3] for r in rows) + 4 * u
        out.append(f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{x1 - x0:.0f}" height="{y1 - y0:.0f}" rx="{4 * u:.0f}" '
                   f'fill="none" stroke="#595959" stroke-width="{0.8 * u:.1f}" stroke-dasharray="{2 * u:.0f} {2 * u:.0f}"/>')
        first_y = (rows[0][1] + rows[0][3]) / 2
        bx = x0 - 10 * u if x0 - 10 * u > -w / 2 + 8 * u else x1 + 10 * u
        out.append(_svg_badge(bx, first_y, label, u))
    out.append("</svg>")
    return "\n".join(out)


def _svg_badge(cx: float, cy: float, label: int, u: float) -> str:
    return (f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{6.5 * u:.0f}" fill="none" stroke="#595959" '
            f'stroke-width="{0.9 * u:.1f}"/>'
            f'<text x="{cx:.0f}" y="{cy + 2.8 * u:.0f}" font-size="{8 * u:.0f}" font-weight="bold" '
            f'text-anchor="middle" fill="#595959">{label}</text>')
