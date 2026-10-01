"""Read a v6 .rm page with rmscene and turn its line items into ink strokes."""

from __future__ import annotations

import logging
from pathlib import Path

import rmscene
from rmscene import scene_items as si

from ...ink.strokes import Stroke, make_stroke
from .settings import StrokesConfig

log = logging.getLogger(__name__)

def crdt_str(cid: rmscene.CrdtId) -> str:
    return f"{cid.part1}:{cid.part2}"


def tool_name(value: int) -> str:
    """Pen enum value to a family name: FINELINER_2 -> "fineliner"."""
    try:
        name = si.Pen(value).name.lower()
    except ValueError:
        return f"tool_{value}"
    return name.removesuffix("_1").removesuffix("_2")


def load_strokes(rm_path: Path, cfg: StrokesConfig) -> list[Stroke]:
    with open(rm_path, "rb") as f:
        return load_strokes_from(f, rm_path.name, cfg)


def load_strokes_from(f, name: str, cfg: StrokesConfig) -> list[Stroke]:
    """Like load_strokes, from an open binary stream (e.g. a file inside the .rmdoc zip)."""
    ignore = set(cfg.ignore_tools)
    anchored_groups: set[str] = set()
    raw: list[tuple[str, str, si.Line]] = []  # (id, parent id, line)
    unreadable = 0

    for block in rmscene.read_blocks(f):
        if isinstance(block, rmscene.UnreadableBlock):
            unreadable += 1
            log.warning("%s: unreadable block (type %s): %s", name, block.info.block_type, block.error)
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
            name,
            grouped,
        )
    log.debug(
        "%s: %d strokes kept, %d dropped by tool, %d too short, %d unreadable blocks",
        name,
        len(strokes),
        dropped_tool,
        dropped_short,
        unreadable,
    )
    return strokes
