"""Claude as the LLM: it reads handwriting, and judges lines when Jev isn't set up.

One Messages request per page and question, with structured output. What to
transcribe comes from `jotted.recognise`, the criteria for actions and owners from
`jotted.classify`; this adapter only words the judging questions for Claude, talks to
the Anthropic API and turns its answers into the port's types.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import anthropic

from ..classify import ACTION, OWNER
from ..config import LLMConfig
from ..core.model import Judgment
from ..llm import Continuation, Document, LineImage, ModelError, Transcript
from ..recognise import SCHEMA, SYSTEM

log = logging.getLogger(__name__)

NOTES = "Lines were transcribed from handwriting, so expect abbreviations and small reading errors."

ACTIONS_SYSTEM = f"""You judge lines from someone's handwritten notes. For each line you are asked about,
decide whether it is an action item and, if it is, who is to do it. The other lines are context only.

action:
- true: {ACTION["true"]}
- false: {ACTION["false"]}

owner:
- me: {OWNER["me"]}
- someone_else: {OWNER["someone_else"]}
- unclear: {OWNER["unclear"]}

{NOTES} Answer for every line you are asked about, once each."""

ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "action": {"type": "boolean"},
                    "owner": {"type": "string", "enum": list(OWNER)},
                },
                "required": ["i", "action", "owner"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}

CONTINUES_SYSTEM = f"""You judge the layout of someone's handwritten notes. Each pair you are given is a line
written directly under another, with no bullet or checkbox of its own. Decide whether the lower line
continues the upper one: the same item wrapped onto a second line, so the two should be read as one item.

- true: read together they form one item, like "email the landlord about" followed by "the broken heater".
- false: the lower line makes sense as its own item, note or heading, like "buy milk" followed by "call mum".

{NOTES} Answer for every pair, once each."""

CONTINUES_SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "above": {"type": "integer"},
                    "below": {"type": "integer"},
                    "continues": {"type": "boolean"},
                },
                "required": ["above", "below", "continues"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}


def _line(i: int, t: Transcript) -> dict:
    return {"i": i, "text": t.text, **({"checkbox": t.checkbox} if t.checkbox != "none" else {})}


class AnthropicLLM:
    LABEL = "Anthropic"
    MODEL_FAMILY = "Claude"
    KEY_URL = "https://console.anthropic.com/settings/keys"
    KEY_ENV = "ANTHROPIC_API_KEY"

    @staticmethod
    def reads_images(model: str) -> bool:
        """Every Claude model reads images."""
        return model.startswith("claude-")

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.model = cfg.model
        api_key = os.environ.get(cfg.api_key_env)
        if not api_key:
            raise ModelError(f"{cfg.api_key_env} is not set; run `jotted setup` or add the key in Settings")
        self.client = anthropic.Anthropic(api_key=api_key, timeout=float(cfg.timeout_s))

    def verify(self) -> None:
        try:
            self.client.models.list(limit=1)
        except anthropic.AuthenticationError as e:
            raise ModelError("Anthropic rejected this key") from e
        except anthropic.APIError as e:
            raise ModelError(f"could not check the key with Anthropic: {e}") from e

    def _ask(self, system: str, content: list[dict], schema: dict, what: str) -> dict:
        try:
            resp = self.client.beta.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.AuthenticationError as e:
            raise ModelError(f"Anthropic rejected the key in {self.cfg.api_key_env}") from e
        except anthropic.APIStatusError as e:
            raise ModelError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ModelError(f"could not reach the Anthropic API: {e}") from e

        if resp.stop_reason == "refusal":
            raise ModelError(f"{what} declined ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise ModelError(f"{what} was cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ModelError(f"unexpected {what} output: {text[:200]!r}") from e
        log.debug("%s: %d in / %d out tokens (request %s)", what, resp.usage.input_tokens,
                  resp.usage.output_tokens, resp._request_id)
        return data

    # ------------------------------------------------------------ reading

    def read(self, images: list[LineImage]) -> dict[int, Transcript]:
        content: list[dict] = [{"type": "text", "text": f"Transcribe these {len(images)} handwritten lines."}]
        for img in images:
            content.append({"type": "text", "text": f"Line {img.n}:"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.standard_b64encode(img.png).decode()},
            })
        data = self._ask(SYSTEM, content, SCHEMA, "transcription")
        try:
            return {item["n"]: Transcript(n=item["n"], checkbox=item["checkbox"], text=item["text"].strip(),
                                          drawing=item["drawing"]) for item in data["lines"]}
        except (KeyError, TypeError, AttributeError) as e:
            raise ModelError(f"unexpected transcription output: {str(data)[:200]!r}") from e

    # ------------------------------------------------------------ judging

    def actions(self, document: Document, lines: list[Transcript], targets: list[int],
                context: int) -> dict[int, Judgment]:
        """Claude sees the whole page; `context` only matters to judges with a narrower window."""
        question = {
            "document": {"name": document.name, "folder": document.folder, "page": document.page},
            "lines": [_line(i, t) for i, t in enumerate(lines)],
            "judge": targets,
        }
        data = self._ask(ACTIONS_SYSTEM, [{"type": "text", "text": json.dumps(question, ensure_ascii=False)}],
                         ACTIONS_SCHEMA, "action judging")
        wanted = set(targets)
        out = {}
        for item in data.get("lines", []):
            if item.get("i") in wanted:
                out[item["i"]] = Judgment(p_action=1.0 if item["action"] else 0.0, owner=item["owner"])
        return out

    def continues(self, lines: list[Transcript], pairs: list[Continuation]) -> dict[tuple[int, int], float]:
        question = {
            "lines": [_line(i, t) for i, t in enumerate(lines)],
            "pairs": [{"above": q.above, "below": q.below,
                       "spacing": f"{q.spacing:.0%} of the usual spacing between lines on this page",
                       **({"indented": "indented under the text of the list item above it"} if q.indented else {})}
                      for q in pairs],
        }
        data = self._ask(CONTINUES_SYSTEM, [{"type": "text", "text": json.dumps(question, ensure_ascii=False)}],
                         CONTINUES_SCHEMA, "continuation judging")
        wanted = {(q.above, q.below) for q in pairs}
        return {(p["above"], p["below"]): 1.0 if p["continues"] else 0.0
                for p in data.get("pairs", []) if (p.get("above"), p.get("below")) in wanted}
