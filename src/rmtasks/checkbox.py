"""Decide whether a line starts with a checkbox, from the geometry of its leading strokes.

Every threshold is a ratio of the line's median stroke height `lh`.
Confidence is the weighted share of checks passed; size and closure weigh double.
A check listed in `required_checks` must pass, or the style is not a candidate at all:
without that gate, ordinary letters pass enough of the weak checks to clear 0.5.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import CheckboxConfig
from .lines import MIN_HEIGHT, Line
from .strokes import BBox, Stroke

WEIGHTS = {"size": 2.0, "closure": 2.0}
EPS = 1e-6


@dataclass
class Checkbox:
    style: str
    strokes: list[Stroke]
    confidence: float
    checks: dict[str, bool]
    text: list[Stroke] = field(default_factory=list)  # strokes to the right of the box

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.strokes]

    @property
    def bbox(self) -> BBox:
        return (
            min(s.x0 for s in self.strokes),
            min(s.y0 for s in self.strokes),
            max(s.x1 for s in self.strokes),
            max(s.y1 for s in self.strokes),
        )


def _within(value: float, bounds: list[float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _confidence(checks: dict[str, bool]) -> float:
    total = sum(WEIGHTS.get(k, 1.0) for k in checks)
    passed = sum(WEIGHTS.get(k, 1.0) for k, ok in checks.items() if ok)
    return passed / total if total else 0.0


def _bracket_pair(line: Line, lead: list[Stroke], lh: float, cfg: CheckboxConfig) -> Checkbox | None:
    if len(lead) < 2:
        return None
    a, b = lead[0], lead[1]
    bh = (a.height + b.height) / 2
    x0, x1 = a.x1, b.x0
    y0, y1 = min(a.y0, b.y0), max(a.y1, b.y1)
    between = [s for s in line.strokes if s is not a and s is not b and x0 < s.cx < x1 and y0 <= s.cy <= y1]
    checks = {
        "bracket_aspect": all(s.height / max(s.width, EPS) >= cfg.bracket_aspect_min for s in (a, b)),
        "height_ratio": _within(a.height / max(b.height, EPS), cfg.bracket_height_ratio),
        "overlap": (min(a.y1, b.y1) - max(a.y0, b.y0)) / max(min(a.height, b.height), EPS) >= cfg.bracket_overlap_min,
        "gap": _within((b.x0 - a.x1) / max(bh, EPS), cfg.bracket_gap),
        "clear_between": not between,
        "size": _within(bh / lh, [cfg.size_min, cfg.size_max]),
    }
    return Checkbox("bracket_pair", [a, b], _confidence(checks), checks)


def _single_box(line: Line, lead: list[Stroke], lh: float, cfg: CheckboxConfig) -> Checkbox | None:
    if not lead:
        return None
    s = lead[0]
    diag = math.hypot(s.width, s.height)
    perimeter = 2 * (s.width + s.height)
    inside = [o for o in line.strokes if o is not s and s.x0 < o.cx < s.x1 and s.y0 < o.cy < s.y1]
    checks = {
        "box_aspect": _within(s.height / max(s.width, EPS), cfg.box_aspect),
        "size": _within(s.height / lh, [cfg.size_min, cfg.size_max]),
        "closure": math.dist(s.start, s.end) <= cfg.box_closure_max * diag,
        "path_ratio": _within(s.length / max(perimeter, EPS), cfg.box_path_ratio),
        "clear_inside": not inside,
    }
    return Checkbox("single_box", [s], _confidence(checks), checks)


DETECTORS = {"bracket_pair": _bracket_pair, "single_box": _single_box}


def candidate(line: Line, cfg: CheckboxConfig) -> Checkbox | None:
    """Best-scoring checkbox candidate for the line, whatever its confidence."""
    if not line.strokes:
        return None
    lh = max(line.median_height, MIN_HEIGHT)
    left = line.bbox[0]
    lead = [s for s in line.strokes if s.x0 - left <= cfg.lead_zone * lh]
    required = set(cfg.required_checks)

    best: Checkbox | None = None
    for style in cfg.styles:
        cb = DETECTORS[style](line, lead, lh, cfg)
        if cb is None or not all(ok for k, ok in cb.checks.items() if k in required):
            continue
        if best is None or cb.confidence > best.confidence:
            best = cb
    if best is not None:
        right = best.bbox[2]
        box_ids = set(best.ids)
        best.text = [s for s in line.strokes if s.id not in box_ids and s.cx > right]
    return best


def detect(line: Line, cfg: CheckboxConfig) -> Checkbox | None:
    """The line's checkbox if its confidence clears min_confidence, else None."""
    cb = candidate(line, cfg)
    if cb is None or cb.confidence < cfg.min_confidence:
        return None
    return cb
