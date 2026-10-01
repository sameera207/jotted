"""Jev (TypeSafe System One) as the LineJudge.

One request per page and question type. The state holds the page's written lines;
each line or pair to judge gets its own question over that shared state: a Choice
for line kinds and owners, a Noul for continuations and "is it an action".
Criteria texts come from `jotted.classify`; the question wording is Jev's.
"""

from __future__ import annotations

import logging
import os

from typesafe_sdk import Choice, Noul, TypeSafeAuthenticationError, TypeSafeClient, TypeSafeError

from ..classify import (ACTION, CRITERIA, NOTEBOOK, OWNER, ClassificationError, Continuation, Document, KindAnswer,
                        state_line)
from ..config import ClassificationConfig
from ..core.model import Judgment as ActionJudgment
from ..recognise import Transcript

log = logging.getLogger(__name__)


def _neighbours(lines: list, i: int, context: int, phrase: str, always: bool = False) -> str:
    lo, hi = max(0, i - context), min(len(lines) - 1, i + context)
    return f" `lines[{lo}]` to `lines[{hi}]` {phrase}" if always or (context and hi > lo) else ""


class TypeSafeJudge:
    LABEL = "TypeSafe"
    KEY_URL = "https://typesafe.ai"

    def __init__(self, cfg: ClassificationConfig):
        self.cfg = cfg
        self.model = cfg.model
        self._key = os.environ.get(cfg.api_key_env)
        if not self._key:
            raise ClassificationError(f"{cfg.api_key_env} is not set; export it or set classification.enabled = false")

    def verify(self) -> None:
        try:
            with TypeSafeClient(api_key=self._key, timeout=float(self.cfg.timeout_s)) as client:
                client.models.list()
        except TypeSafeAuthenticationError as e:
            raise ClassificationError("TypeSafe rejected this key") from e
        except TypeSafeError as e:
            raise ClassificationError(f"could not check the key with TypeSafe: {e}") from e

    def _ask(self, questions: dict, state: dict):
        try:
            with TypeSafeClient(api_key=self._key, model=self.cfg.model, timeout=float(self.cfg.timeout_s)) as client:
                resp = client.system_one(state=state, questions=questions)
        except TypeSafeAuthenticationError as e:
            raise ClassificationError(f"TypeSafe rejected the key in {self.cfg.api_key_env}") from e
        except TypeSafeError as e:
            raise ClassificationError(f"TypeSafe request failed: {e}") from e
        log.debug("asked %d question(s) of %s (request %s)", len(questions), resp.model, resp.request_id)
        return resp

    @staticmethod
    def _page_state(lines: list[Transcript]) -> dict:
        return {"notebook": NOTEBOOK, "lines": [state_line(t) for t in lines]}

    def kinds(self, lines: list[Transcript], targets: list[int], context: int) -> dict[int, KindAnswer]:
        questions = {}
        for i in targets:
            neighbours = _neighbours(lines, i, context, "are its neighbours; use them only as context.")
            questions[f"line_{lines[i].n}"] = Choice(
                instructions={
                    "line": f"`lines[{i}]`",
                    "question": f"On this to-do notebook page, what kind of line is `lines[{i}]`? "
                                f"Judge its `text`; `checkbox` says whether it was written with a checkbox."
                                + (" Lines" + neighbours if neighbours else ""),
                },
                criteria=CRITERIA,
            )
        resp = self._ask(questions, self._page_state(lines))
        out = {}
        for i in targets:
            ans = resp.choices.get(f"line_{lines[i].n}")
            if ans is not None:
                out[i] = KindAnswer(ans.choice, dict(ans.probabilities), ans.confidence)
        return out

    def continues(self, lines: list[Transcript], pairs: list[Continuation]) -> dict[tuple[int, int], float]:
        questions, qids = {}, {}
        for q in pairs:
            i, j = q.above, q.below
            qid = f"cont_{lines[i].n}_{lines[j].n}"
            qids[(i, j)] = qid
            questions[qid] = Noul(
                instructions={
                    "layout": f"`lines[{j}]` is written directly under `lines[{i}]`, with no bullet or checkbox of "
                              f"its own"
                              + (", indented under the text of the list item above it" if q.indented else "")
                              + f". The spacing between them is {q.spacing:.0%} of the usual spacing between lines "
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
        resp = self._ask(questions, self._page_state(lines))
        return {pos: ans.noul for pos, qid in qids.items() if (ans := resp.nouls.get(qid)) is not None}

    def actions(self, document: Document, lines: list[Transcript], targets: list[int],
                context: int) -> dict[int, ActionJudgment]:
        state = {
            "document": {"name": document.name, "folder": document.folder, "page": document.page},
            "lines": [{"n": t.n, "text": t.text, **({"checkbox": t.checkbox} if t.checkbox != "none" else {})}
                      for t in lines],
        }
        questions = {}
        for i in targets:
            neighbours = _neighbours(lines, i, context, "are nearby lines; use them only as context.", always=True)
            questions[f"act_{i}"] = Noul(
                instructions={
                    "source": "Handwritten notes from `document`, transcribed line by line.",
                    "question": f"Is `lines[{i}]` an action item?" + neighbours,
                },
                criteria=ACTION,
            )
            questions[f"own_{i}"] = Choice(
                instructions={
                    "assume": f"Treat `lines[{i}]` as an action item.",
                    "question": f"Who is to do `lines[{i}]`?" + neighbours,
                },
                criteria=OWNER,
            )
        resp = self._ask(questions, state)
        out = {}
        for i in targets:
            act, own = resp.nouls.get(f"act_{i}"), resp.choices.get(f"own_{i}")
            if act is not None and own is not None:
                out[i] = ActionJudgment(p_action=float(act.noul), owner=own.choice, owner_probs=dict(own.probabilities))
        return out
