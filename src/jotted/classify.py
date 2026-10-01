"""Judging lines: what kind each one is, whether it continues the line above, and
whether it is an action (and whose).

The model is behind a port, `LineJudge`, with an adapter per provider
(`adapters/typesafe_judge.py` for Jev). Policy stays here and is shared: which lines
are judged and with what context, the criteria, thresholds, geometry, and a cache per
question. To add a provider: write an adapter with `kinds()`, `continues()` and
`actions()`, then register it in `PROVIDERS` and `config.CLASSIFICATION_PROVIDERS`.
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
PROMPT_VERSION = 3  # line kinds and continuations
ACTION_PROMPT_VERSION = 1  # actions and owners

NOTEBOOK = (
    "A handwritten reMarkable notebook the writer uses as a to-do list. It holds tasks, "
    "some with a checkbox and some without, mixed with notes, dates and headings. "
    "Lines were transcribed from handwriting, so expect abbreviations and small reading errors."
)

CRITERIA = {
    "todo": "An action the writer intends to do or follow up: call, email, fix, buy, prepare, book, "
            "review, decide, or a meeting or conversation to have. A short noun phrase counts when, "
            "on a to-do list, it names something to get done.",
    "note": "Information rather than an action: a fact, an observation, a thought, a quote, "
            "or a record of what already happened.",
    "heading": "A date, a day name, a title or a section label that organises the lines below it.",
}

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


@dataclass
class Judgment:
    n: int
    choice: str
    probabilities: dict[str, float]
    confidence: float
    cached: bool = False

    @property
    def p_todo(self) -> float:
        return self.probabilities.get("todo", 0.0)


# ---------------------------------------------------------------- the port


@dataclass(frozen=True)
class KindAnswer:
    choice: str  # a key of CRITERIA
    probabilities: dict[str, float]
    confidence: float


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

    def kinds(self, lines: list[Transcript], targets: list[int], context: int) -> dict[int, KindAnswer]:
        """Per target: todo, note or heading (CRITERIA). `context` neighbours each side matter."""

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


def classify(transcripts: dict[int, Transcript], cfg: ClassificationConfig, cache: AICache,
             judge: LineJudge | None = None) -> dict[int, Judgment]:
    ordered = _judgeable(transcripts)
    out: dict[int, Judgment] = {}
    keys: dict[int, str] = {}  # position -> cache key, for lines to ask about
    for i, t in enumerate(ordered):
        lo, hi = max(0, i - cfg.context_lines), min(len(ordered) - 1, i + cfg.context_lines)
        context = [state_line(x) for x in ordered[lo : hi + 1]]
        key = AICache.key("judgment", PROMPT_VERSION, cfg.model, cfg.context_lines, state_line(t), context)
        hit = cache.get("judgments", key)
        if hit is not None:
            out[t.n] = Judgment(n=t.n, cached=True, **hit)
        else:
            keys[i] = key
    if not keys:
        return out

    answers = (judge or judge_for(cfg)).kinds(ordered, list(keys), cfg.context_lines)
    for i, key in keys.items():
        n, ans = ordered[i].n, answers.get(i)
        if ans is None:
            log.warning("no judgment returned for line %d", n)
            continue
        j = Judgment(n=n, choice=ans.choice, probabilities=dict(ans.probabilities), confidence=ans.confidence)
        cache.put("judgments", key, {"choice": j.choice, "probabilities": j.probabilities, "confidence": j.confidence})
        out[n] = j
    return out
