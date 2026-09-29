"""Handwriting recognition: render each line to a PNG and transcribe with Claude.

One request per page carries every uncached line, each image labelled with its
line number. Structured output returns {n, checkbox, text} per line.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from dataclasses import dataclass

import anthropic
from PIL import Image, ImageDraw

from .aicache import AICache
from .config import RecognitionConfig
from .lines import Line

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


class RecognitionError(Exception):
    pass


@dataclass
class Transcript:
    n: int
    checkbox: str  # "empty", "checked" or "none"
    text: str
    drawing: bool = False
    cached: bool = False


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


def line_key(line: Line, cfg: RecognitionConfig) -> str:
    return AICache.key("transcript", PROMPT_VERSION, cfg.model, sorted(s.id for s in line.strokes))


def transcribe(lines: list[Line], cfg: RecognitionConfig, cache: AICache) -> dict[int, Transcript]:
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

    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise RecognitionError(f"{cfg.api_key_env} is not set; export it or set recognition.enabled = false")

    content: list[dict] = [{"type": "text", "text": f"Transcribe these {len(todo)} handwritten lines."}]
    for line in todo:
        content.append({"type": "text", "text": f"Line {line.n}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(render_line(line)).decode()},
        })

    client = anthropic.Anthropic(api_key=api_key, timeout=float(cfg.timeout_s))
    try:
        resp = client.beta.messages.create(
            model=cfg.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise RecognitionError(f"Anthropic rejected the key in {cfg.api_key_env}") from e
    except anthropic.APIStatusError as e:
        raise RecognitionError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RecognitionError(f"could not reach the Anthropic API: {e}") from e

    if resp.stop_reason == "refusal":
        raise RecognitionError(f"transcription declined ({getattr(resp.stop_details, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise RecognitionError("transcription was cut off (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        got = {item["n"]: item for item in json.loads(text)["lines"]}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise RecognitionError(f"unexpected transcription output: {text[:200]!r}") from e
    log.debug("transcribed %d line(s): %d in / %d out tokens (request %s)",
              len(todo), resp.usage.input_tokens, resp.usage.output_tokens, resp._request_id)

    for line in todo:
        item = got.get(line.n)
        if item is None:
            log.warning("line %d missing from transcription", line.n)
            continue
        t = Transcript(n=line.n, checkbox=item["checkbox"], text=item["text"].strip(), drawing=item["drawing"])
        cache.put("transcripts", line_key(line, cfg), {"checkbox": t.checkbox, "text": t.text, "drawing": t.drawing})
        out[line.n] = t
    return out


