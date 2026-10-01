"""Cluster a page's strokes into handwritten text lines."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..config import LinesConfig
from .strokes import BBox, Stroke

MIN_HEIGHT = 1.0  # floor for median heights, so dots and dashes can't make it zero


@dataclass
class Line:
    strokes: list[Stroke] = field(default_factory=list)  # sorted by left edge after clustering
    n: int = 0
    rows: list[BBox] = field(default_factory=list)  # set when wrapped lines were merged into this one

    @property
    def row_boxes(self) -> list[BBox]:
        """One box per written row: the original lines of a merged item, else the line itself."""
        return self.rows or [self.bbox]

    @property
    def bbox(self) -> BBox:
        return (
            min(s.x0 for s in self.strokes),
            min(s.y0 for s in self.strokes),
            max(s.x1 for s in self.strokes),
            max(s.y1 for s in self.strokes),
        )

    @property
    def median_height(self) -> float:
        return median_height(self.strokes)

    @property
    def anchor_id(self) -> str:
        """The line's first-written stroke: lowest CrdtId counter, then author."""
        return min(self.strokes, key=lambda s: crdt_key(s.id)).id


def crdt_key(sid: str) -> tuple[int, int]:
    author, counter = sid.split(":")
    return int(counter), int(author)


def median_height(strokes: list[Stroke]) -> float:
    if not strokes:
        return MIN_HEIGHT
    return max(statistics.median(s.height for s in strokes), MIN_HEIGHT)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def cluster(strokes: list[Stroke], cfg: LinesConfig) -> tuple[list[Line], list[Stroke]]:
    """Return (lines top to bottom, tall strokes that overlap no line)."""
    if not strokes:
        return [], []
    h = median_height(strokes)
    tall = [s for s in strokes if s.height > cfg.tall_stroke_factor * h]
    regular = sorted((s for s in strokes if s.height <= cfg.tall_stroke_factor * h), key=lambda s: s.cy)

    lines: list[Line] = []
    cur: list[Stroke] = []
    band_y0 = band_y1 = centre_sum = 0.0
    for s in regular:
        if cur:
            mean_centre = centre_sum / len(cur)
            near = abs(s.cy - mean_centre) <= cfg.band_tolerance * h
            if s.height > 0:
                overlaps = _overlap(s.y0, s.y1, band_y0, band_y1) / s.height >= cfg.min_vertical_overlap
            else:
                overlaps = band_y0 <= s.cy <= band_y1
            if near or overlaps:
                cur.append(s)
                band_y0, band_y1 = min(band_y0, s.y0), max(band_y1, s.y1)
                centre_sum += s.cy
                continue
            lines.append(Line(cur))
        cur = [s]
        band_y0, band_y1, centre_sum = s.y0, s.y1, s.cy
    if cur:
        lines.append(Line(cur))

    unassigned: list[Stroke] = []
    bands = [ln.bbox for ln in lines]
    for s in tall:
        best, best_overlap = None, 0.0
        for ln, (_, y0, _, y1) in zip(lines, bands):
            ov = _overlap(s.y0, s.y1, y0, y1)
            if ov > best_overlap:
                best, best_overlap = ln, ov
        if best is None:
            unassigned.append(s)
        else:
            best.strokes.append(s)

    lines = merge_same_row(lines, cfg.same_row_overlap)
    lines = merge_contained(lines, cfg.merge_containment, pad=0.25 * h)
    lines.sort(key=lambda ln: ln.bbox[1])
    for i, ln in enumerate(lines, start=1):
        ln.strokes.sort(key=lambda s: s.x0)
        ln.n = i
    return lines, unassigned


def _area(b: BBox) -> float:
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _pad(b: BBox, p: float) -> BBox:
    return (b[0] - p, b[1] - p, b[2] + p, b[3] + p)


def merge_contained(lines: list[Line], threshold: float, pad: float = 0.0) -> list[Line]:
    """Merge a line into another when most of its box lies inside the other's box.

    Drawings (a box with a label, arrows between shapes) are cut into several
    "lines" by the band sweep; their pieces sit inside the drawing's outline.
    Ordinary text lines barely overlap, so they are left alone.
    """
    if threshold <= 0:
        return lines
    lines = list(lines)
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(lines):
            for j, b in enumerate(lines):
                if i == j:
                    continue
                ba, bb = _pad(a.bbox, pad), _pad(b.bbox, pad)
                inter = (max(ba[0], bb[0]), max(ba[1], bb[1]), min(ba[2], bb[2]), min(ba[3], bb[3]))
                small = min(_area(ba), _area(bb))
                if small > 0 and _area(inter) / small >= threshold:
                    a.strokes.extend(b.strokes)
                    del lines[j]
                    merged = True
                    break
            if merged:
                break
    return lines


def merge_same_row(lines: list[Line], threshold: float) -> list[Line]:
    """Join lines that sit side by side on one row.

    A letter with a long descender (the p of "prod") can have its centre far enough below
    the rest of the row to start a new line in the sweep. Such a piece overlaps the row
    vertically, and is either beside it (little horizontal overlap) or a loose letter of
    one or two strokes within it. Two real lines do neither.
    """
    if threshold <= 0:
        return lines
    lines = list(lines)
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(lines):
            for j, b in enumerate(lines):
                if i >= j:
                    continue
                (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = a.bbox, b.bbox
                small_h = min(ay1 - ay0, by1 - by0)
                small_w = min(ax1 - ax0, bx1 - bx0)
                if small_h <= 0:
                    continue
                v = _overlap(ay0, ay1, by0, by1) / small_h
                hz = _overlap(ax0, ax1, bx0, bx1) / small_w if small_w > 0 else 1.0
                loose = min(len(a.strokes), len(b.strokes)) <= 2
                if v >= threshold and (hz < 0.5 or loose):
                    a.strokes.extend(b.strokes)
                    del lines[j]
                    merged = True
                    break
            if merged:
                break
    return lines
