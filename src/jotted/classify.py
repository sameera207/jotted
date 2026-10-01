"""Judging lines: whether each one continues the line above, and whether it is an
action (and whose).

The model is behind a port, `LineJudge`, with an adapter per provider
(`adapters/typesafe_judge.py` for Jev). Policy stays here and is shared: which lines
are judged and with what context, the criteria, thresholds, geometry, and a cache per
question. To add a provider: write an adapter with `continues()` and `actions()`,
then register it in `PROVIDERS` and `config.CLASSIFICATION_PROVIDERS`.
"""

from __future__ import annotations

import importlib
import logging
import statistics
from dataclasses import dataclass
from typing import Protocol

from .aicache import AICache
from .config import ClassificationConfig
from .core.model import Judgment as ActionJudgment
from .lines import Line
from .recognise import Transcript

log = logging.getLogger(__name__)

# Part of the cache keys: bump when the questions change, in any adapter.
PROMPT_VERSION = 3  # continuations
ACTION_PROMPT_VERSION = 1  # actions and owners

NOTEBOOK = (
    "A handwritten reMarkable notebook the writer uses as a to-do list. It holds tasks, "
    "some with a checkbox and some without, mixed with notes, dates and headings. "
    "Lines were transcribed from handwriting, so expect abbreviations and small reading errors."
)

ACTION = {
    "true": "Something someone should do, follow up, decide or deliver: a task, a request, a commitment, "
            "a next step, or a reminder. Short notes count when they clearly name something to get done "
            "(\"book flights\", \"Simon → send the deck\").",
    "false": "Information rather than an action: a fact, a discussion point, an observation, a decision "
             "already made, a heading, a date, or a question with nothing to do.",
}

OWNER = {
    "me": "The writer of the notes is to do it, or no one else is named (\"I'll…\", \"check the bill\", "
          "\"book the room\").",
    "someone_else": "A named person, team or group is to do it (\"Simon to send the deck\", \"@Jo: review\", "
                    "\"infra team will…\").",
    "unclear": "It is an action but who should do it cannot be told from the notes.",
}


class ClassificationError(Exception):
    pass


# ---------------------------------------------------------------- the port


@dataclass(frozen=True)
class Continuation:
    """Does lines[below] continue lines[above]? Positions are in the judged lines."""
    above: int
    below: int
    spacing: float  # their distance as a share of the page's usual line spacing
    indented: bool  # below is indented under the text of a bulleted or boxed line


@dataclass(frozen=True)
class Document:
    name: str
    folder: str
    page: int


class LineJudge(Protocol):
    """A model that judges transcribed lines. Every method gets the page's written lines in
    order and the positions to judge; the others are context. Raises ClassificationError.
    Answers it cannot give are simply absent.

    Adapter classes also carry LABEL and KEY_URL (where to get a key), for setup."""

    model: str

    def verify(self) -> None:
        """Check the key works, without judging anything (setup calls this)."""

    def continues(self, lines: list[Transcript], pairs: list[Continuation]) -> dict[tuple[int, int], float]:
        """Per (above, below): the probability that below continues above."""

    def actions(self, document: Document, lines: list[Transcript], targets: list[int],
                context: int) -> dict[int, ActionJudgment]:
        """Per target: the probability it is an action (ACTION) and who owns it (OWNER)."""


# provider name -> "module:class", imported only when used.
PROVIDERS = {"typesafe": "jotted.adapters.typesafe_judge:TypeSafeJudge"}


def judge_class(cfg: ClassificationConfig) -> type:
    target = PROVIDERS.get(cfg.provider)
    if target is None:
        raise ClassificationError(f"unknown classification provider {cfg.provider!r}; known: {sorted(PROVIDERS)}")
    module, cls = target.split(":")
    return getattr(importlib.import_module(module), cls)


def judge_for(cfg: ClassificationConfig) -> LineJudge:
    return judge_class(cfg)(cfg)


# ---------------------------------------------------------------- shared policy

BULLETS = ("-", "–", "—", "•", "*", "·", ">")


def state_line(t: Transcript) -> dict:
    """A line as the judge sees it (also part of the cache keys)."""
    line = {"n": t.n, "checkbox": t.checkbox, "text": t.text}
    if t.drawing:
        line["drawing"] = True
    return line


def _judgeable(transcripts: dict[int, Transcript]) -> list[Transcript]:
    """Lines the judge sees: written text only. Drawings and empty lines are left out;
    with them in, Jev read them as odd blank lines and neighbouring judgments
    dropped (P(todo) 0.92 -> 0.45 for the same line)."""
    return [transcripts[n] for n in sorted(transcripts) if transcripts[n].text and not transcripts[n].drawing]


def _centre(line: Line) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2


def line_pitch(lines: list[Line], transcripts: dict[int, Transcript]) -> float | None:
    """The page's usual distance between the centres of consecutive written lines."""
    written = [ln for ln in sorted(lines, key=lambda x: x.n)
               if (t := transcripts.get(ln.n)) and t.text and not t.drawing]
    steps = [_centre(b) - _centre(a) for a, b in zip(written, written[1:])]
    return statistics.median(steps) if steps else None


def continuation_candidates(lines: list[Line], transcripts: dict[int, Transcript],
                            cfg: ClassificationConfig) -> list[tuple[int, int, float, bool]]:
    """(above, below, spacing, indented) where `below` might be `above` wrapped onto a new line.

    `below` starts with no checkbox or bullet, is not outdented, and either:
    - sits closer under `above` than the page's usual line spacing (`continuation_spacing`
      of it or less), or
    - is indented under an `above` that starts with a bullet or checkbox, at no more than
      the usual spacing (a list item wrapped at normal spacing).
    `spacing` is the distance as a share of the usual one.
    """
    pitch = line_pitch(lines, transcripts)
    if not pitch:
        return []
    ordered = sorted(lines, key=lambda ln: ln.n)
    out = []
    for above, below in zip(ordered, ordered[1:]):
        ta, tb = transcripts.get(above.n), transcripts.get(below.n)
        if not (ta and tb and ta.text and tb.text) or ta.drawing or tb.drawing:
            continue
        if tb.checkbox != "none" or tb.text.startswith(BULLETS):
            continue
        spacing = (_centre(below) - _centre(above)) / pitch
        lh = max(above.median_height, below.median_height)
        if below.bbox[0] < above.bbox[0] - 0.5 * lh:
            continue
        marked = ta.checkbox != "none" or ta.text.startswith(BULLETS)
        indented = marked and below.bbox[0] > above.bbox[0] + lh
        if spacing <= cfg.continuation_spacing or (indented and spacing <= 1.2):
            out.append((above.n, below.n, spacing, indented))
    return out


def adjacent_drawings(lines: list[Line], transcripts: dict[int, Transcript],
                      cfg: ClassificationConfig) -> list[tuple[int, int]]:
    """Pairs of drawings that touch vertically and overlap horizontally: one drawing cut in two
    (a diagram's base line just outside its outline, say). No model call needed."""
    ordered = sorted(lines, key=lambda ln: ln.n)
    pairs = []
    for above, below in zip(ordered, ordered[1:]):
        ta, tb = transcripts.get(above.n), transcripts.get(below.n)
        if not (ta and tb and ta.drawing and tb.drawing):
            continue
        lh = max(above.median_height, below.median_height)
        gap = below.bbox[1] - above.bbox[3]
        x_overlap = min(above.bbox[2], below.bbox[2]) - max(above.bbox[0], below.bbox[0])
        if gap <= lh and x_overlap > 0:
            pairs.append((above.n, below.n))
    return pairs


def continuations(lines: list[Line], transcripts: dict[int, Transcript], cfg: ClassificationConfig,
                  cache: AICache, judge: LineJudge | None = None) -> list[tuple[int, int]]:
    """Candidate pairs the judge does not reject: `below` continues `above`.

    Geometry is the main evidence here and the judge acts as a veto: on real pages Jev put
    a genuine wrap at ~0.33 and separate neighbouring items at 0.08-0.17, so the default
    threshold is low. Tune `continuation_threshold` on your own pages."""
    pairs = continuation_candidates(lines, transcripts, cfg)
    if not pairs:
        return []
    ordered = _judgeable(transcripts)
    index = {t.n: i for i, t in enumerate(ordered)}
    confirmed: list[tuple[int, int]] = []
    ask: dict[tuple[int, int], tuple[int, int, str]] = {}  # (i, j) -> (above n, below n, cache key)
    questions = []
    for above, below, spacing, indented in pairs:
        key = AICache.key("continuation", PROMPT_VERSION, cfg.model, round(spacing, 2), indented,
                          state_line(transcripts[above]), state_line(transcripts[below]))
        hit = cache.get("continuations", key)
        if hit is not None:
            if hit["p"] >= cfg.continuation_threshold:
                confirmed.append((above, below))
            continue
        q = Continuation(index[above], index[below], spacing, indented)
        ask[(q.above, q.below)] = (above, below, key)
        questions.append(q)
    if questions:
        answers = (judge or judge_for(cfg)).continues(ordered, questions)
        for pos, (above, below, key) in ask.items():
            p = answers.get(pos)
            if p is None:
                continue
            cache.put("continuations", key, {"p": p})
            if p >= cfg.continuation_threshold:
                confirmed.append((above, below))
    return sorted(confirmed)


def merge_continuations(
    page_lines: list[Line], transcripts: dict[int, Transcript], pairs: list[tuple[int, int]]
) -> tuple[list[Line], dict[int, Transcript], dict[int, list[int]]]:
    """Fold each confirmed continuation into the line it continues (chains allowed).
    The merged line keeps the first line's number, so its anchor is the first line's."""
    root: dict[int, int] = {}
    for above, below in pairs:
        root[below] = root.get(above, above)
    by_n = {ln.n: ln for ln in page_lines}
    parts: dict[int, list[int]] = {}
    for ln in sorted(page_lines, key=lambda x: x.n):
        parts.setdefault(root.get(ln.n, ln.n), []).append(ln.n)

    merged_lines, merged_ts = [], {}
    for first, ns in parts.items():
        line = Line(strokes=[s for n in ns for s in by_n[n].strokes], n=first)
        if len(ns) > 1:
            line.rows = [by_n[n].bbox for n in ns]
        line.strokes.sort(key=lambda s: s.x0)
        merged_lines.append(line)
        ts = [transcripts[n] for n in ns if n in transcripts]
        if ts:
            merged_ts[first] = Transcript(
                n=first, checkbox=ts[0].checkbox, text=" ".join(t.text for t in ts if t.text),
                drawing=ts[0].drawing, cached=all(t.cached for t in ts),
            )
    return merged_lines, merged_ts, {first: ns for first, ns in parts.items() if len(ns) > 1}
