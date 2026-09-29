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
    strikes: list[tuple[float, float, float]] = field(default_factory=list)  # (x0, x1, y) in rM units
    footer: list[str] = field(default_factory=list)  # web tasks, one per row
    calibration: bool = False


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
    for text in state.footer:
        if row_y < MARGIN:
            break  # overflow is a deferred edge case
        c.rect(MARGIN, row_y - 2, 9, 9, stroke=1, fill=0)
        c.drawString(MARGIN + 16, row_y, text)
        row_y -= 20

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
