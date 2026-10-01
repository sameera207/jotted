"""Handwriting recognition: render each line to a PNG and have a model read it.

One request per page carries every uncached line, each image labelled with its
line number. Structured output returns {n, drawing, checkbox, text} per line.

The model is the configured LLM (`jotted.llm`); its adapter's `read()` sends
`SYSTEM`, the images and `SCHEMA`. The prompt, schema, rendering and cache live
here, so every provider shares them.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw

from .aicache import AICache
from .config import LLMConfig
from .lines import Line
from .llm import LLM, LineImage, Transcript, llm_for

log = logging.getLogger(__name__)

PROMPT_VERSION = 2
STROKE_PX = 50  # rendered height of a median stroke
MAX_PX = 1500
PAD = 12.0  # page units around the line

SYSTEM = """You transcribe handwritten lines from a reMarkable notebook used as a to-do list.
Each image is usually one line of handwriting, labelled with its line number. Some images are
drawings instead: diagrams, boxes, arrows, shapes or doodles, possibly with a few labels.

For each image report:
- drawing: true if the image is mainly a drawing or diagram rather than a line of writing.
- checkbox: "empty" if the line starts with an empty checkbox (drawn as [ ], a small square, or similar),
  "checked" if that checkbox is ticked or crossed, otherwise "none".
- text: the words written after any checkbox, exactly as written. Keep the writer's spelling,
  abbreviations and numbers. Do not include the checkbox itself. Use "" if there are no words.
  If a word is illegible, give your best reading; do not add commentary.
  For a drawing, text holds its labels in reading order, joined with " → " where arrows connect them
  (for example "aws → CI"), or "" if it has none.

Return every line you were given, once each."""

SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "drawing": {"type": "boolean"},
                    "checkbox": {"type": "string", "enum": ["empty", "checked", "none"]},
                    "text": {"type": "string"},
                },
                "required": ["n", "drawing", "checkbox", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}


def render_line(line: Line) -> bytes:
    """Scale so a typical stroke is STROKE_PX tall, whatever the line's size; a drawing
    spanning several lines keeps its labels legible instead of being squashed."""
    x0, y0, x1, y1 = line.bbox
    x0, y0, x1, y1 = x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD
    scale = STROKE_PX / line.median_height
    scale = min(scale, MAX_PX / max(x1 - x0, 1.0), MAX_PX / max(y1 - y0, 1.0))
    width, height = max(int((x1 - x0) * scale), 1), max(int((y1 - y0) * scale), 1)
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    pen = max(2, round(3 * scale / 2))
    for s in line.strokes:
        pts = [((x - x0) * scale, (y - y0) * scale) for x, y in s.points]
        draw.line(pts, fill=0, width=pen, joint="curve")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def line_key(line: Line, cfg: LLMConfig) -> str:
    # The model name tells providers apart, so the provider itself is left out (older keys stay valid).
    return AICache.key("transcript", PROMPT_VERSION, cfg.model, sorted(s.id for s in line.strokes))


def transcribe(lines: list[Line], cfg: LLMConfig, cache: AICache,
               reader: LLM | None = None) -> dict[int, Transcript]:
    """Transcripts for `lines`, from the cache where possible. Only uncached lines are sent,
    and the reader is only created when there are some (so a cached page needs no API key)."""
    out: dict[int, Transcript] = {}
    todo: list[Line] = []
    for line in lines:
        hit = cache.get("transcripts", line_key(line, cfg))
        if hit is not None:
            out[line.n] = Transcript(n=line.n, checkbox=hit["checkbox"], text=hit["text"],
                                     drawing=hit.get("drawing", False), cached=True)
        else:
            todo.append(line)
    if not todo:
        return out

    reader = reader or llm_for(cfg)
    got = reader.read([LineImage(line.n, render_line(line)) for line in todo])
    for line in todo:
        t = got.get(line.n)
        if t is None:
            log.warning("line %d missing from transcription", line.n)
            continue
        t = Transcript(n=line.n, checkbox=t.checkbox, text=t.text.strip(), drawing=t.drawing)
        cache.put("transcripts", line_key(line, cfg), {"checkbox": t.checkbox, "text": t.text, "drawing": t.drawing})
        out[line.n] = t
    return out
