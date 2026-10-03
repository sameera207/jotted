"""Guided setup: from a fresh install to the web app, one step at a time.

`jotted start` runs it before serving; `jotted setup` runs it again on purpose (to
change a key, say). Every step checks first and is skipped when already done, so a
second start asks nothing. Input and output go through `UI`, so a Mac app can drive
the same steps with its own windows.

The steps are `jotted.steps`' (a wrapper can check and run them one at a time); this is
their interactive form: a home for settings and data, the source plugin's own steps (for
reMarkable: rmapi and the cloud connection), the LLM's API key, and the optional Jev
plugin (offered on first setup and when run again on purpose).
"""

from __future__ import annotations

import getpass
import os

from rich.console import Console
from rich.markup import escape

from . import config, keys, llm, steps
from .config import Config
from .ui import UI, SetupError

__all__ = ["ConsoleUI", "SetupError", "UI", "run"]

TRIES = 3


class ConsoleUI:
    def __init__(self, console: Console | None = None):
        self.c = console or Console()

    def step(self, title: str) -> None:
        self.c.print(f"\n[bold]{escape(title)}[/bold]")

    def done(self, text: str) -> None:
        self.c.print(f"[green]✓[/green] {escape(text)}")

    def info(self, text: str) -> None:
        self.c.print(f"  {escape(text)}")

    def warn(self, text: str) -> None:
        self.c.print(f"[yellow]![/yellow] {escape(text)}")

    def ask(self, prompt: str) -> str:
        return input(f"  {prompt}: ")

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"  {prompt}: ")

    def confirm(self, prompt: str, default: bool = True) -> bool:
        answer = input(f"  {prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer in ("y", "yes")


def run(ui: UI, redo: bool = False) -> Config:
    """Bring this machine to a working setup and return its config: every step of
    `jotted.steps` that has an interactive form, in order. `redo` asks again about the
    reMarkable connection and the keys, keeping what's there by default."""
    path = config.resolve_path()
    fresh = not path.is_file()
    if fresh:
        ui.step("Setting up Jotted")
        config.create(path)
        ui.done(f"Settings, data and keys will live in {path.parent}")
    cfg = config.load(path)
    keys.load_into_env(cfg)
    for step in steps.steps(cfg):
        if step.walk:
            step.walk(steps.Walk(ui, cfg, redo, fresh))
            cfg = config.load(path)  # a step may have written config.toml
    return cfg


# ---------------------------------------------------------------- API keys


def llm_key(ui: UI, cfg: Config, redo: bool) -> None:
    cls = llm.llm_class(cfg.llm)
    current = os.environ.get(cfg.llm.api_key_env)
    if current and not redo:
        ui.done(f"{cls.LABEL} key")
        return
    ui.step(f"{cls.LABEL} API key")
    ui.info(f"Jotted reads your handwriting with {cls.MODEL_FAMILY}: images of new lines are sent to {cls.LABEL}, "
            "and their text to judge which lines are actions.")
    ui.info(f"Create a key at {cls.KEY_URL}")
    if not _ask_key(ui, cfg, cfg.llm.api_key_env, lambda: cls(cfg.llm), cls, current):
        raise SetupError(f"Jotted needs a {cls.LABEL} key to read your notes. Create one at {cls.KEY_URL}, "
                         "then run `jotted start` again", step="llm")


def jev(ui: UI, cfg: Config, ask: bool) -> None:
    """The Jev plugin: optional, so only offered on first setup or `jotted setup`."""
    from .adapters.typesafe_judge import TypeSafeJudge as cls

    name = cfg.jev.api_key_env
    current = os.environ.get(name)
    if not ask:
        if current:
            ui.done(f"{cls.NAME} plugin ({cls.LABEL} key)")
        return
    ui.step(f"{cls.NAME} plugin (optional)")
    ui.info(f"{cls.NAME}, from {cls.LABEL}, can judge which lines are actions and whose they are, instead of "
            f"the LLM. Their text is then sent to {cls.LABEL}. You can add or remove it in Settings any time.")
    if not ui.confirm(f"Use {cls.NAME}?", default=bool(current)):
        if current and keys.describe(cfg, name)["source"] == "saved":
            keys.remove(cfg, name)
            ui.done(f"{cls.NAME} removed")
        else:
            ui.done(f"Not using {cls.NAME}")
        return
    ui.info(f"Create a key at {cls.KEY_URL}")
    if not _ask_key(ui, cfg, name, lambda: cls(cfg.jev), cls, current):
        ui.warn(f"Carrying on without {cls.NAME}")


def _ask_key(ui: UI, cfg: Config, name: str, make, cls: type, current: str | None) -> bool:
    """Ask for a key until one checks out (or is kept). False if none was given."""
    for _ in range(TRIES):
        hint = " (Enter keeps the current one)" if current else ""
        value = ui.secret(f"Paste your {cls.LABEL} key, it stays hidden{hint}").strip()
        if not value:
            if current:
                ui.done(f"Keeping the current {cls.LABEL} key")
                return True
            continue
        os.environ[name] = value
        try:
            make().verify()
        except llm.ModelError as e:
            _restore(name, current)
            if "rejected" in str(e):
                ui.warn(f"{e}. Check it was copied in full.")
                continue
            ui.warn(str(e))
            if not ui.confirm("Save it anyway?", default=False):
                continue
        keys.save(cfg, name, value)
        ui.done(f"{cls.LABEL} key saved (only your user can read it)")
        return True
    if current:
        ui.warn(f"Keeping the current {cls.LABEL} key")
        return True
    return False


def _restore(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
