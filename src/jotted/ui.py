"""How setup talks to the person: a console today, a desktop app's windows tomorrow.
Plugins' setup steps use the same interface."""

from __future__ import annotations

import getpass
import sys
from typing import Any, Protocol

from .contract import UsageError


class SetupError(Exception):
    """Setup can't go on; the message says what the person should do. `code` is its
    contract error code (`jotted.contract`), `step` the setup step that is missing."""

    def __init__(self, message: str, code: str = "not_set_up", step: str | None = None):
        super().__init__(message)
        self.code, self.step = code, step


class UI(Protocol):
    def step(self, title: str) -> None: ...
    def done(self, text: str) -> None: ...
    def info(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def ask(self, prompt: str) -> str: ...
    def secret(self, prompt: str) -> str: ...
    def confirm(self, prompt: str, default: bool = True) -> bool: ...


def can_prompt(args: Any) -> bool:
    """Prompts only when stdin is a terminal and --json is off: a wrapper never hangs on one."""
    return not getattr(args, "json", False) and sys.stdin.isatty()


def read_secret(args: Any, prompt: str) -> str:
    """A key or one-time code for a command: from stdin with --stdin, else asked (hidden)."""
    if getattr(args, "stdin", False):
        return sys.stdin.readline().strip()
    if not can_prompt(args):
        raise UsageError(f"{prompt} needed: pass it on standard input with --stdin")
    return getpass.getpass(f"{prompt} (it stays hidden): ").strip()
