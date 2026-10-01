"""Jev (TypeSafe System One) as the ActionJudge: is a line an action, and whose?

One request per page. The state holds the page's written lines plus the document's
name and folder. Each new line gets two questions over that shared state: a Noul
(is it an action) and a Choice (who owns it). Answers are cached by the line's text
and its neighbours, so a line is judged once.
"""

from __future__ import annotations

from typesafe_sdk import Choice, Noul

from ..aicache import AICache
from ..classify import ask
from ..config import ClassificationConfig
from ..core.model import DocInfo, Judgment, PageInfo, SourceLine

PROMPT_VERSION = 1
CONTEXT = 2  # neighbouring lines on each side

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


class JevActionJudge:
    def __init__(self, cfg: ClassificationConfig, cache: AICache):
        self.cfg, self.cache = cfg, cache

    def judge(self, doc: DocInfo, page: PageInfo, lines: list[SourceLine],
              new: list[SourceLine]) -> dict[str, Judgment]:
        written = [ln for ln in lines if ln.text and not ln.drawing]
        index = {ln.anchor: i for i, ln in enumerate(written)}
        state = {
            "document": {"name": doc.name, "folder": doc.folder, "page": page.index},
            "lines": [{"n": i + 1, "text": ln.text, **({"checkbox": ln.checkbox} if ln.checkbox != "none" else {})}
                      for i, ln in enumerate(written)],
        }
        out: dict[str, Judgment] = {}
        questions: dict = {}
        keys: dict[str, tuple[str, str, str]] = {}
        for ln in new:
            i = index.get(ln.anchor)
            if i is None:
                continue
            context = [w.text for w in written[max(0, i - CONTEXT): i + CONTEXT + 1]]
            key = AICache.key("action", PROMPT_VERSION, self.cfg.model, doc.name, doc.folder, ln.text, context)
            hit = self.cache.get("actions", key)
            if hit is not None:
                out[ln.anchor] = Judgment(**hit)
                continue
            neighbours = f" `lines[{max(0, i - CONTEXT)}]` to `lines[{min(len(written) - 1, i + CONTEXT)}]` are " \
                         f"nearby lines; use them only as context."
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
            keys[ln.anchor] = (f"act_{i}", f"own_{i}", key)
        if questions:
            resp = ask(questions, state, self.cfg)
            for anchor, (q_act, q_own, key) in keys.items():
                act, own = resp.nouls.get(q_act), resp.choices.get(q_own)
                if act is None or own is None:
                    continue
                j = Judgment(p_action=float(act.noul), owner=own.choice, owner_probs=dict(own.probabilities))
                self.cache.put("actions", key, {"p_action": j.p_action, "owner": j.owner, "owner_probs": j.owner_probs})
                out[anchor] = j
        return out
