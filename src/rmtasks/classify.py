"""Decide whether each transcribed line is a to-do, with Jev (TypeSafe System One).

One request per page. The state holds the page's lines; each uncached line gets
its own Choice question over that shared state. Policy (thresholds, what a
checkbox means) stays in code; Jev only supplies the judgment.
"""

from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass

from typesafe_sdk import (
    Choice,
    Noul,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeError,
)

from .aicache import AICache
from .config import ClassificationConfig
from .lines import Line
from .recognise import Transcript

log = logging.getLogger(__name__)

PROMPT_VERSION = 3

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


BULLETS = ("-", "–", "—", "•", "*", "·", ">")


def _state_line(t: Transcript) -> dict:
    line = {"n": t.n, "checkbox": t.checkbox, "text": t.text}
    if t.drawing:
        line["drawing"] = True
    return line


def _judgeable(transcripts: dict[int, Transcript]) -> list[Transcript]:
    """Lines Jev sees: written text only. Drawings and empty lines are left out of the
    state; with them in, Jev read them as odd blank lines and neighbouring judgments
    dropped (P(todo) 0.92 -> 0.45 for the same line)."""
    return [transcripts[n] for n in sorted(transcripts) if transcripts[n].text and not transcripts[n].drawing]


def _page_state(ordered: list[Transcript]) -> dict:
    return {"notebook": NOTEBOOK, "lines": [_state_line(t) for t in ordered]}


def _ask(questions: dict, state: dict, cfg: ClassificationConfig):
    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        raise ClassificationError(f"{cfg.api_key_env} is not set; export it or set classification.enabled = false")
    try:
        with TypeSafeClient(api_key=api_key, model=cfg.model, timeout=float(cfg.timeout_s)) as client:
            resp = client.system_one(state=state, questions=questions)
    except TypeSafeAuthenticationError as e:
        raise ClassificationError(f"TypeSafe rejected the key in {cfg.api_key_env}") from e
    except TypeSafeError as e:
        raise ClassificationError(f"TypeSafe request failed: {e}") from e
    log.debug("asked %d question(s) of %s (request %s)", len(questions), resp.model, resp.request_id)
    return resp


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
                  cache: AICache) -> list[tuple[int, int]]:
    """Candidate pairs Jev does not reject: `below` continues `above`.

    Geometry is the main evidence here and Jev acts as a veto: on real pages Jev put a
    genuine wrap at ~0.33 and separate neighbouring items at 0.08-0.17, so the default
    threshold is low. Tune `continuation_threshold` on your own pages."""
    pairs = continuation_candidates(lines, transcripts, cfg)
    if not pairs:
        return []
    ordered = _judgeable(transcripts)
    index = {t.n: i for i, t in enumerate(ordered)}
    confirmed, questions, keys = [], {}, {}
    for above, below, spacing, indented in pairs:
        key = AICache.key("continuation", PROMPT_VERSION, cfg.model, round(spacing, 2), indented,
                          _state_line(transcripts[above]), _state_line(transcripts[below]))
        hit = cache.get("continuations", key)
        if hit is not None:
            if hit["p"] >= cfg.continuation_threshold:
                confirmed.append((above, below))
            continue
        qid = f"cont_{above}_{below}"
        keys[qid] = (above, below, key)
        i, j = index[above], index[below]
        questions[qid] = Noul(
            instructions={
                "layout": f"`lines[{j}]` is written directly under `lines[{i}]`, with no bullet or checkbox of its "
                          f"own"
                          + (", indented under the text of the list item above it" if indented else "")
                          + f". The spacing between them is {spacing:.0%} of the usual spacing between lines "
                          f"on this page.",
                "question": f"Does `lines[{j}]` continue `lines[{i}]`, the same item wrapped onto a second line, "
                            f"so they should be read as one item?",
            },
            criteria={
                "true": f"Read together, `lines[{i}]` and `lines[{j}]` form one item, like \"email the landlord "
                        f"about\" followed by \"the broken heater\".",
                "false": f"`lines[{j}]` makes sense as its own separate item, note or heading, like \"buy milk\" "
                         f"followed by \"call mum\".",
            },
        )
    if questions:
        resp = _ask(questions, _page_state(ordered), cfg)
        for qid, (above, below, key) in keys.items():
            ans = resp.nouls.get(qid)
            if ans is None:
                continue
            cache.put("continuations", key, {"p": ans.noul})
            if ans.noul >= cfg.continuation_threshold:
                confirmed.append((above, below))
    return sorted(confirmed)


def classify(transcripts: dict[int, Transcript], cfg: ClassificationConfig, cache: AICache) -> dict[int, Judgment]:
    ordered = _judgeable(transcripts)
    state = _page_state(ordered)

    out: dict[int, Judgment] = {}
    questions: dict[str, Choice] = {}
    keys: dict[str, tuple[int, str]] = {}
    for i, t in enumerate(ordered):
        lo, hi = max(0, i - cfg.context_lines), min(len(ordered) - 1, i + cfg.context_lines)
        context = [_state_line(x) for x in ordered[lo : hi + 1]]
        key = AICache.key("judgment", PROMPT_VERSION, cfg.model, cfg.context_lines, _state_line(t), context)
        hit = cache.get("judgments", key)
        if hit is not None:
            out[t.n] = Judgment(n=t.n, cached=True, **hit)
            continue
        qid = f"line_{t.n}"
        keys[qid] = (t.n, key)
        neighbours = (
            f" Lines `lines[{lo}]` to `lines[{hi}]` are its neighbours; use them only as context."
            if cfg.context_lines and hi > lo else ""
        )
        questions[qid] = Choice(
            instructions={
                "line": f"`lines[{i}]`",
                "question": f"On this to-do notebook page, what kind of line is `lines[{i}]`? "
                            f"Judge its `text`; `checkbox` says whether it was written with a checkbox."
                            + neighbours,
            },
            criteria=CRITERIA,
        )
    if not questions:
        return out

    resp = _ask(questions, state, cfg)
    for qid, (n, key) in keys.items():
        ans = resp.choices.get(qid)
        if ans is None:
            log.warning("no judgment returned for line %d", n)
            continue
        j = Judgment(n=n, choice=ans.choice, probabilities=dict(ans.probabilities), confidence=ans.confidence)
        cache.put("judgments", key, {"choice": j.choice, "probabilities": j.probabilities, "confidence": j.confidence})
        out[n] = j
    return out
