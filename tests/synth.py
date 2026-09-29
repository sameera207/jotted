"""Build synthetic v6 pages and .rmdoc archives for tests.

Real fixtures from your own handwriting go in tests/fixtures/; these synthetic
pages only exercise the pipeline with tidy, predictable geometry.
"""

from __future__ import annotations

import io
import json
import random
import uuid
import zipfile
from pathlib import Path

import rmscene
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.tagged_block_common import CrdtId

Pts = list[tuple[float, float]]
LAYER = CrdtId(0, 11)


def bracket_pair(x: float, cy: float, h: float = 40.0) -> list[Pts]:
    w, top, bot = 0.28 * h, cy - h / 2, cy + h / 2
    gap = 0.7 * h
    left = [(x + w, top), (x, top), (x, bot), (x + w, bot)]
    rx = x + w + gap
    right = [(rx, top), (rx + w, top), (rx + w, bot), (rx, bot)]
    return [left, right]


def single_box(x: float, cy: float, h: float = 40.0) -> list[Pts]:
    top, bot = cy - h / 2, cy + h / 2
    return [[(x, top), (x + h, top), (x + h, bot), (x, bot), (x + 2, top + 3)]]


def word(x: float, cy: float, letters: int, h: float = 32.0, rng: random.Random | None = None) -> tuple[list[Pts], float]:
    """Zig-zag 'letters', one stroke each. Returns (strokes, x after the word)."""
    rng = rng or random.Random(0)
    out = []
    for _ in range(letters):
        lh = h * rng.uniform(0.8, 1.2)
        w = lh * rng.uniform(0.45, 0.7)
        top = cy - lh / 2
        out.append([(x, top + lh), (x + w * 0.3, top), (x + w * 0.6, top + lh), (x + w, top + lh * 0.3)])
        x += w + h * 0.15
    return out, x + h * 0.5


def text(x: float, cy: float, words: int = 3, rng: random.Random | None = None) -> list[Pts]:
    rng = rng or random.Random(0)
    out = []
    for _ in range(words):
        ws, x = word(x, cy, rng.randint(2, 6), rng=rng)
        out.extend(ws)
    return out


def rm_bytes(strokes: list[Pts], first_id: int = 100, tool: si.Pen = si.Pen.FINELINER_2,
             ids: list[int] | None = None, deleted: list[int] | None = None) -> bytes:
    """A minimal v6 page: one layer holding one line item per stroke."""
    blocks: list = [
        rmscene.AuthorIdsBlock(author_uuids={1: uuid.uuid4()}),
        rmscene.MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        rmscene.PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        rmscene.SceneTreeBlock(tree_id=LAYER, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1)),
        rmscene.TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
        rmscene.TreeNodeBlock(si.Group(node_id=LAYER, label=rmscene.LwwValue(CrdtId(0, 12), "Layer 1"))),
        rmscene.SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(item_id=CrdtId(0, 13), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
                                  deleted_length=0, value=LAYER),
        ),
    ]
    ids = ids or [first_id + i for i in range(len(strokes))]
    for sid, pts in zip(ids, strokes):
        line = si.Line(
            color=si.PenColor.BLACK,
            tool=tool,
            points=[si.Point(x=x, y=y, speed=0, direction=0, width=2, pressure=100) for x, y in pts],
            thickness_scale=1.0,
            starting_length=0.0,
        )
        blocks.append(rmscene.SceneLineItemBlock(
            parent_id=LAYER,
            item=CrdtSequenceItem(item_id=CrdtId(1, sid), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
                                  deleted_length=0, value=line),
        ))
    for sid in deleted or []:
        blocks.append(rmscene.SceneLineItemBlock(
            parent_id=LAYER,
            item=CrdtSequenceItem(item_id=CrdtId(1, sid), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
                                  deleted_length=1, value=None),
        ))
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks, options={"version": "3.1"})
    return buf.getvalue()


def rmdoc(path: Path, pages: list[bytes], doc_id: str = "doc-0001", name: str = "Tasks",
          file_type: str = "notebook") -> Path:
    page_ids = [f"page-{i:04d}" for i in range(len(pages))]
    content = {
        "formatVersion": 2,
        "fileType": file_type,
        "cPages": {"pages": [{"id": pid, "idx": {"timestamp": "1:2", "value": "b" + chr(ord("a") + i)}}
                             for i, pid in enumerate(page_ids)]},
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{doc_id}.content", json.dumps(content))
        z.writestr(f"{doc_id}.metadata", json.dumps({"visibleName": name, "version": 7}))
        for pid, data in zip(page_ids, pages):
            z.writestr(f"{doc_id}/{pid}.rm", data)
    return path


def demo_page(rng: random.Random | None = None) -> tuple[list[Pts], list[str]]:
    """A page of mixed lines. Returns strokes and the expected kind per line."""
    rng = rng or random.Random(1)
    strokes: list[Pts] = []
    kinds = []
    x0, y = -600.0, 200.0
    layout = ["bracket", "note", "box", "bracket", "note", "box", "bracket", "note", "box", "empty", "bracket", "note"]
    for kind in layout:
        if kind == "note":
            strokes += text(x0, y, rng.randint(3, 5), rng)
            kinds.append("note")
        else:
            cb = {"bracket": bracket_pair, "box": single_box, "empty": bracket_pair}[kind](x0, y)
            strokes += cb
            if kind != "empty":
                strokes += text(x0 + 110, y, rng.randint(2, 4), rng)
            kinds.append("empty_checkbox" if kind == "empty" else "task")
        y += 90
    return strokes, kinds
