"""The core's ActionJudge, answered by the configured LineJudge (classification.provider).

Is a line an action, and whose? One request per page: the state holds the page's
written lines plus the document's name and folder. Answers are cached by the line's
text and its neighbours, so a line is judged once, whichever provider answers.
"""

from __future__ import annotations

from ..aicache import AICache
from ..classify import ACTION_PROMPT_VERSION, Document, LineJudge, judge_for
from ..config import ClassificationConfig
from ..core.model import DocInfo, Judgment, PageInfo, SourceLine
from ..recognise import Transcript

CONTEXT = 2  # neighbouring lines on each side


class ModelActionJudge:
    def __init__(self, cfg: ClassificationConfig, cache: AICache, judge: LineJudge | None = None):
        self.cfg, self.cache = cfg, cache
        self._judge = judge  # made on first use, so cached pages need no API key

    def judge(self, doc: DocInfo, page: PageInfo, lines: list[SourceLine],
              new: list[SourceLine]) -> dict[str, Judgment]:
        written = [ln for ln in lines if ln.text and not ln.drawing]
        index = {ln.anchor: i for i, ln in enumerate(written)}
        out: dict[str, Judgment] = {}
        ask: dict[int, tuple[str, str]] = {}  # position -> (anchor, cache key)
        for ln in new:
            i = index.get(ln.anchor)
            if i is None:
                continue
            context = [w.text for w in written[max(0, i - CONTEXT): i + CONTEXT + 1]]
            key = AICache.key("action", ACTION_PROMPT_VERSION, self.cfg.model, doc.name, doc.folder, ln.text, context)
            hit = self.cache.get("actions", key)
            if hit is not None:
                out[ln.anchor] = Judgment(**hit)
            else:
                ask[i] = (ln.anchor, key)
        if not ask:
            return out
        self._judge = self._judge or judge_for(self.cfg)
        page_lines = [Transcript(n=i + 1, checkbox=ln.checkbox, text=ln.text) for i, ln in enumerate(written)]
        answers = self._judge.actions(Document(doc.name, doc.folder, page.index), page_lines, list(ask), CONTEXT)
        for i, (anchor, key) in ask.items():
            j = answers.get(i)
            if j is None:
                continue
            self.cache.put("actions", key, {"p_action": j.p_action, "owner": j.owner, "owner_probs": j.owner_probs})
            out[anchor] = j
        return out
