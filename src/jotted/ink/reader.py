"""From strokes to read lines: cluster, transcribe with the LLM, merge wrapped lines.

Plugins get one through `plugins.Host.ink`, so a handwriting plugin only has to turn its
pages into strokes. Answers are cached by stroke IDs, so a line is read once.
"""

from __future__ import annotations

import hashlib

from .. import classify, recognise
from ..aicache import AICache
from ..config import Config
from ..core.model import SourceLine
from .lines import Line, cluster
from .strokes import Stroke


def line_key(strokes: list[Stroke]) -> str:
    """Changes whenever the line's strokes do."""
    return hashlib.sha256(",".join(sorted(s.id for s in strokes)).encode()).hexdigest()[:24]


class InkReader:
    def __init__(self, cfg: Config, cache: AICache):
        self.cfg, self.cache = cfg, cache

    def lines(self, strokes: list[Stroke]) -> list[SourceLine]:
        """A page's strokes as lines, in reading order. Drawings come back marked as such."""
        page_lines, _ = cluster(strokes, self.cfg.lines)
        if not page_lines:
            return []
        transcripts = recognise.transcribe(page_lines, self.cfg.llm, self.cache)
        pairs = sorted(classify.adjacent_drawings(page_lines, transcripts, self.cfg.judging)
                       + classify.continuations(page_lines, transcripts, self.cfg, self.cache))
        page_lines, transcripts, _ = classify.merge_continuations(page_lines, transcripts, pairs)
        out = []
        for ln in sorted(page_lines, key=lambda x: x.n):
            t = transcripts.get(ln.n)
            out.append(SourceLine(
                anchor=ln.anchor_id, key=line_key(ln.strokes), text=t.text if t else "", bbox=ln.bbox,
                rows=list(ln.row_boxes), drawing=bool(t and t.drawing), checkbox=t.checkbox if t else "none",
                marks=tuple(sorted(s.id for s in ln.strokes)),
            ))
        return out

    def line(self, strokes: list[Stroke]) -> SourceLine | None:
        """Strokes known to be one line (a row of a printed list) read as one; None if they
        hold no words."""
        line = Line(strokes=sorted(strokes, key=lambda s: s.x0), n=1)
        t = recognise.transcribe([line], self.cfg.llm, self.cache).get(1)
        if t is None or t.drawing or not t.text.strip():
            return None
        return SourceLine(anchor=line.anchor_id, key=line_key(strokes), text=t.text, bbox=line.bbox,
                          rows=[line.bbox], marks=tuple(sorted(s.id for s in strokes)))
