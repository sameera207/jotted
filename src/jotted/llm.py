"""The language model port: one model that reads handwriting and, unless the Jev plugin
is set up, judges the lines it read.

Each provider is an adapter (`adapters/anthropic_llm.py` for Claude) registered in
`PROVIDERS` and `config.LLM_PROVIDERS`. The prompts' policy stays shared: what to
transcribe is in `jotted.recognise`, the criteria for actions and owners in
`jotted.classify`. To add a provider, write a class with the methods below plus the
LABEL and KEY_URL class attributes, then register it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Protocol

from .config import LLMConfig
from .core.model import Judgment


class ModelError(Exception):
    """A model could not answer: no key, a rejected key, or a failed request."""


@dataclass
class Transcript:
    n: int
    checkbox: str  # "empty", "checked" or "none"
    text: str
    drawing: bool = False
    cached: bool = False


@dataclass(frozen=True)
class LineImage:
    n: int  # the line's number on the page, as the model must report it back
    png: bytes


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
    """Judges transcribed lines: the LLM, or the Jev plugin. Every method gets the page's
    written lines in order and the positions to judge; the others are context. Raises
    ModelError. Answers it cannot give are simply absent."""

    model: str

    def verify(self) -> None:
        """Check the key works, without judging anything."""

    def continues(self, lines: list[Transcript], pairs: list[Continuation]) -> dict[tuple[int, int], float]:
        """Per (above, below): the probability that below continues above."""

    def actions(self, document: Document, lines: list[Transcript], targets: list[int],
                context: int) -> dict[int, Judgment]:
        """Per target: the probability it is an action and who owns it (`classify.ACTION`, `classify.OWNER`)."""


class LLM(LineJudge, Protocol):
    """A language model that reads handwriting as well as judging it."""

    def read(self, images: list[LineImage]) -> dict[int, Transcript]:
        """A transcript per line number (`recognise.SYSTEM`); lines the model skipped are absent."""


# provider name -> "module:class", imported only when used, so other providers'
# SDKs need not be installed.
PROVIDERS = {"anthropic": "jotted.adapters.anthropic_llm:AnthropicLLM"}


def llm_class(cfg: LLMConfig) -> type:
    target = PROVIDERS.get(cfg.provider)
    if target is None:
        raise ModelError(f"unknown LLM provider {cfg.provider!r}; known: {sorted(PROVIDERS)}")
    module, cls = target.split(":")
    return getattr(importlib.import_module(module), cls)


def llm_for(cfg: LLMConfig) -> LLM:
    return llm_class(cfg)(cfg)
