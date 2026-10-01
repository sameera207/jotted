"""Claude as the HandwritingReader: one Messages request per page, structured output.

The prompt, schema and line images come from `jotted.recognise`; this adapter only
talks to the Anthropic API and turns its answer into transcripts.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import anthropic

from ..config import RecognitionConfig
from ..recognise import SCHEMA, SYSTEM, LineImage, RecognitionError, Transcript

log = logging.getLogger(__name__)


class AnthropicReader:
    LABEL = "Anthropic"
    KEY_URL = "https://console.anthropic.com/settings/keys"

    def __init__(self, cfg: RecognitionConfig):
        self.cfg = cfg
        self.model = cfg.model
        api_key = os.environ.get(cfg.api_key_env)
        if not api_key:
            raise RecognitionError(f"{cfg.api_key_env} is not set; export it or set recognition.enabled = false")
        self.client = anthropic.Anthropic(api_key=api_key, timeout=float(cfg.timeout_s))

    def verify(self) -> None:
        try:
            self.client.models.list(limit=1)
        except anthropic.AuthenticationError as e:
            raise RecognitionError("Anthropic rejected this key") from e
        except anthropic.APIError as e:
            raise RecognitionError(f"could not check the key with Anthropic: {e}") from e

    def read(self, images: list[LineImage]) -> dict[int, Transcript]:
        content: list[dict] = [{"type": "text", "text": f"Transcribe these {len(images)} handwritten lines."}]
        for img in images:
            content.append({"type": "text", "text": f"Line {img.n}:"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.standard_b64encode(img.png).decode()},
            })
        try:
            resp = self.client.beta.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                output_config={"effort": self.cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.AuthenticationError as e:
            raise RecognitionError(f"Anthropic rejected the key in {self.cfg.api_key_env}") from e
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
            items = json.loads(text)["lines"]
            got = {item["n"]: Transcript(n=item["n"], checkbox=item["checkbox"], text=item["text"].strip(),
                                         drawing=item["drawing"]) for item in items}
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
            raise RecognitionError(f"unexpected transcription output: {text[:200]!r}") from e
        log.debug("transcribed %d line(s): %d in / %d out tokens (request %s)",
                  len(images), resp.usage.input_tokens, resp.usage.output_tokens, resp._request_id)
        return got
