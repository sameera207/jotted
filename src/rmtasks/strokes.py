"""Read a v6 .rm page with rmscene and turn its line items into Strokes."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import rmscene
from rmscene import scene_items as si

from .config import StrokesConfig

log = logging.getLogger(__name__)

Point = tuple[float, float]
BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class Stroke:
    id: str  # rmscene CrdtId as "part1:part2"
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


def crdt_str(cid: rmscene.CrdtId) -> str:
    return f"{cid.part1}:{cid.part2}"


def tool_name(value: int) -> str:
    """Pen enum value to a family name: FINELINER_2 -> "fineliner"."""
    try:
        name = si.Pen(value).name.lower()
    except ValueError:
        return f"tool_{value}"
    return name.removesuffix("_1").removesuffix("_2")


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


def load_strokes(rm_path: Path, cfg: StrokesConfig) -> list[Stroke]:
    ignore = set(cfg.ignore_tools)
    anchored_groups: set[str] = set()
    raw: list[tuple[str, str, si.Line]] = []  # (id, parent id, line)
    unreadable = 0

    with open(rm_path, "rb") as f:
        for block in rmscene.read_blocks(f):
            if isinstance(block, rmscene.UnreadableBlock):
                unreadable += 1
                log.warning("%s: unreadable block (type %s): %s", rm_path.name, block.info.block_type, block.error)
            elif isinstance(block, rmscene.TreeNodeBlock):
                if block.group.anchor_id is not None:
                    anchored_groups.add(crdt_str(block.group.node_id))
            elif isinstance(block, rmscene.SceneLineItemBlock):
                value = block.item.value
                if value is None:  # deleted stroke
                    continue
                raw.append((crdt_str(block.item.item_id), crdt_str(block.parent_id), value))

    strokes: list[Stroke] = []
    grouped = dropped_tool = dropped_short = 0
    for sid, parent, line in raw:
        tool = tool_name(line.tool)
        if tool in ignore:
            dropped_tool += 1
            continue
        if len(line.points) < cfg.min_points:
            dropped_short += 1
            continue
        if parent in anchored_groups:
            grouped += 1
        strokes.append(make_stroke(sid, tool, [(p.x, p.y) for p in line.points]))

    if grouped:
        log.warning(
            "%s: %d stroke(s) sit in text-anchored groups; their positions may be offset",
            rm_path.name,
            grouped,
        )
    log.debug(
        "%s: %d strokes kept, %d dropped by tool, %d too short, %d unreadable blocks",
        rm_path.name,
        len(strokes),
        dropped_tool,
        dropped_short,
        unreadable,
    )
    return strokes
