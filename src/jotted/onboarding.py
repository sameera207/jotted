"""Guided setup: from a fresh install to the web app, one step at a time.

`jotted start` runs it before serving; `jotted setup` runs it again on purpose (to
change a key, say). Every step checks first and is skipped when already done, so a
second start asks nothing. Input and output go through `UI`, so a Mac app can drive
the same steps with its own windows.

Steps: a home for settings and data, rmapi, the reMarkable connection, the LLM's API
key, and the optional Jev plugin (offered on first setup and when run again on purpose).
"""

from __future__ import annotations

import getpass
import os
import shutil
from typing import Protocol

from rich.console import Console
from rich.markup import escape

from . import cloud, config, keys, llm, rmapi_install
from .config import Config

CONNECT_URL = "https://my.remarkable.com/device/desktop/connect"
TRIES = 3


class SetupError(Exception):
    """Setup can't go on; the message says what the person should do."""


class UI(Protocol):
    def step(self, title: str) -> None: ...
    def done(self, text: str) -> None: ...
    def info(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def ask(self, prompt: str) -> str: ...
    def secret(self, prompt: str) -> str: ...
    def confirm(self, prompt: str, default: bool = True) -> bool: ...


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
    """Bring this machine to a working setup and return its config. `redo` asks again
    about the reMarkable connection and the keys, keeping what's there by default."""
    path = config.resolve_path()
    fresh = not path.is_file()
    if fresh:
        ui.step("Setting up Jotted")
        config.create(path)
        ui.done(f"Settings, data and keys will live in {path.parent}")
    cfg = config.load(path)
    keys.load_into_env(cfg)
    cfg = _rmapi(ui, cfg)
    _connect(ui, cfg, redo)
    _llm_key(ui, cfg, redo)
    _jev(ui, cfg, ask=fresh or redo)
    return cfg


# ---------------------------------------------------------------- rmapi


def _rmapi(ui: UI, cfg: Config) -> Config:
    found = shutil.which(cfg.rmapi.binary)
    if found:
        ui.done(f"rmapi: {found}")
        return cfg
    ui.step("rmapi")
    ui.info("Jotted reaches your reMarkable cloud through rmapi, a free open-source tool "
            "(github.com/ddvk/rmapi). It isn't installed yet.")
    if not ui.confirm(f"Download rmapi {rmapi_install.VERSION} for this computer?"):
        raise SetupError("Jotted needs rmapi. Install it from https://github.com/ddvk/rmapi/releases, "
                         "then run `jotted start` again")
    try:
        binary = rmapi_install.install(cfg.source.parent / "bin")
    except rmapi_install.InstallError as e:
        raise SetupError(str(e)) from e
    config.set_value(cfg.source, "rmapi", "binary", str(binary))
    ui.done(f"rmapi installed: {binary}")
    return config.load(cfg.source)


# ---------------------------------------------------------------- reMarkable


def _connect(ui: UI, cfg: Config, redo: bool) -> None:
    token = cfg.rmapi.token_file
    if token.exists():
        if not redo:
            ui.done("reMarkable connected")
            return
        ui.step("Your reMarkable")
        if not ui.confirm("Connect your reMarkable again?", default=False):
            ui.done("Keeping the current connection")
            return
    else:
        ui.step("Connect your reMarkable")
    ui.info(f"1. Open {CONNECT_URL} and sign in.")
    ui.info("2. Copy the one-time code it shows (8 characters).")
    backup = token.with_suffix(".previous")
    if token.exists():
        token.replace(backup)  # rmapi only registers when there is no token
    for _ in range(TRIES):
        code = ui.ask("One-time code").strip()
        if not code:
            continue
        try:
            cloud.register(cfg, code)
            docs, _ = cloud.library(cfg)
        except cloud.CloudError as e:
            token.unlink(missing_ok=True)
            ui.warn(f"That didn't work: {str(e).splitlines()[0]}. Codes expire after a few minutes; get a new one.")
            continue
        backup.unlink(missing_ok=True)
        ui.done(f"Connected: {len(docs)} document(s) in your library")
        return
    if backup.exists():
        backup.replace(token)
        raise SetupError("Couldn't connect your reMarkable; the previous connection is kept")
    raise SetupError(f"Couldn't connect your reMarkable. Get a new code at {CONNECT_URL} and run `jotted start` again")


# ---------------------------------------------------------------- API keys


def _llm_key(ui: UI, cfg: Config, redo: bool) -> None:
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
                         "then run `jotted start` again")


def _jev(ui: UI, cfg: Config, ask: bool) -> None:
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
