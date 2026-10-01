"""Digital ink, whatever device it came from: strokes as point lists with their geometry.

A source plugin turns its own file format into these (the reMarkable plugin reads .rm
pages with rmscene); everything after that, line clustering and reading, is shared.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Point = tuple[float, float]
BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class Stroke:
    id: str  # stable across edits, unique on the page (reMarkable: the CrdtId as "part1:part2")
    tool: str
    points: tuple[Point, ...]
    bbox: BBox
    length: float
    start: Point
    end: Point

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


def make_stroke(sid: str, tool: str, points: list[Point]) -> Stroke:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
    return Stroke(
        id=sid,
        tool=tool,
        points=tuple(points),
        bbox=(min(xs), min(ys), max(xs), max(ys)),
        length=length,
        start=points[0],
        end=points[-1],
    )
