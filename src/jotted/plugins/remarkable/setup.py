"""Setting up the reMarkable plugin: rmapi, then the connection to the reMarkable cloud.

Two steps (`jotted.steps`): `remarkable.rmapi`, done by `jotted setup prepare` without
asking, and `remarkable.connect`, done by `jotted connect` with a one-time code. Each
also has an interactive form for the walkthrough (`jotted start`/`jotted setup`).
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from ... import config
from ...config import Config
from ...steps import Step, Walk
from ...ui import UI, SetupError, can_prompt, read_secret
from . import cloud, rmapi_install

CONNECT_URL = "https://my.remarkable.com/device/desktop/connect"
TRIES = 3


def steps() -> list[Step]:
    return [
        Step("remarkable.rmapi", "rmapi, to reach the reMarkable cloud", check_rmapi, command="setup prepare",
             prepare=prepare_rmapi, walk=lambda w: install_rmapi(w.ui, w.cfg)),
        Step("remarkable.connect", "Your reMarkable", check_connect, command="connect --stdin",
             walk=lambda w: connect(w.ui, w.cfg, w.redo)),
    ]


# ---------------------------------------------------------------- rmapi


def _ours(cfg: Config, found: str) -> bool:
    return Path(found).resolve().parent == (cfg.source.parent / "bin").resolve()


def check_rmapi(cfg: Config | None) -> dict:
    found = shutil.which(cfg.rmapi.binary) if cfg else None
    if not found:
        return {"done": False}
    detail = f"rmapi {rmapi_install.VERSION}, checksum verified" if _ours(cfg, found) else f"found at {found}"
    return {"done": True, "detail": detail, "path": found}


def prepare_rmapi(cfg: Config) -> str:
    """Download rmapi for this machine (checksum checked) and point config.toml at it."""
    try:
        binary = rmapi_install.install(cfg.source.parent / "bin")
    except rmapi_install.InstallError as e:
        raise SetupError(str(e), code="not_connected", step="remarkable.rmapi") from e
    config.set_value(cfg.source, "rmapi", "binary", str(binary))
    return f"rmapi {rmapi_install.VERSION} downloaded to {binary}, checksum verified"


def install_rmapi(ui: UI, cfg: Config) -> bool:
    found = shutil.which(cfg.rmapi.binary)
    if found:
        ui.done(f"rmapi: {found}")
        return False
    ui.step("rmapi")
    ui.info("Jotted reaches your reMarkable cloud through rmapi, a free open-source tool "
            "(github.com/ddvk/rmapi). It isn't installed yet.")
    if not ui.confirm(f"Download rmapi {rmapi_install.VERSION} for this computer?"):
        raise SetupError("Jotted needs rmapi. Install it from https://github.com/ddvk/rmapi/releases, "
                         "then run `jotted start` again", step="remarkable.rmapi")
    try:
        binary = rmapi_install.install(cfg.source.parent / "bin")
    except rmapi_install.InstallError as e:
        raise SetupError(str(e), step="remarkable.rmapi") from e
    config.set_value(cfg.source, "rmapi", "binary", str(binary))
    ui.done(f"rmapi installed: {binary}")
    return True


# ---------------------------------------------------------------- the connection


def check_connect(cfg: Config | None) -> dict:
    return {"done": bool(cfg) and cfg.rmapi.token_file.exists()}


def register(cfg: Config, code: str) -> int:
    """Connect with a one-time code, keeping any previous connection if it fails.
    Returns how many documents the library has."""
    token = cfg.rmapi.token_file
    backup = token.with_suffix(".previous")
    if token.exists():
        token.replace(backup)  # rmapi only registers when there is no token
    try:
        cloud.register(cfg, code)
        docs, _ = cloud.library(cfg)
    except cloud.CloudError:
        token.unlink(missing_ok=True)
        if backup.exists():
            backup.replace(token)
        raise
    backup.unlink(missing_ok=True)
    return len(docs)


def connect(ui: UI, cfg: Config, redo: bool) -> None:
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
    had = token.exists()
    ui.info(f"1. Open {CONNECT_URL} and sign in.")
    ui.info("2. Copy the one-time code it shows (8 characters).")
    for _ in range(TRIES):
        code = ui.ask("One-time code").strip()
        if not code:
            continue
        try:
            documents = register(cfg, code)
        except cloud.CloudError as e:
            ui.warn(f"That didn't work: {str(e).splitlines()[0]}. Codes expire after a few minutes; get a new one.")
            continue
        ui.done(f"Connected: {documents} document(s) in your library")
        return
    if had:
        raise SetupError("Couldn't connect your reMarkable; the previous connection is kept",
                         step="remarkable.connect")
    raise SetupError(f"Couldn't connect your reMarkable. Get a new code at {CONNECT_URL} and run `jotted start` again",
                     step="remarkable.connect")


# ---------------------------------------------------------------- `jotted connect`


def cli(sub: argparse._SubParsersAction) -> None:
    for name, kw in (("connect", {"help": "connect your reMarkable with a one-time code"}),
                     ("auth", {})):  # its old name: hidden, kept for one release
        c = sub.add_parser(name, description=f"Get a one-time code at {CONNECT_URL} (sign in, then connect "
                                             "a desktop app).", **kw)
        c.add_argument("--stdin", action="store_true", help="read the one-time code from standard input")
        c.add_argument("--replace", action="store_true", help="replace the current connection")
        c.set_defaults(func=cmd_connect, render=_render_connect, deprecated="connect" if name == "auth" else None)


def cmd_connect(cfg: Config, args: argparse.Namespace) -> dict:
    """`jotted connect`: connect to the reMarkable cloud with a one-time code."""
    if not shutil.which(cfg.rmapi.binary):
        raise SetupError("rmapi isn't installed yet; run `jotted setup prepare` first", step="remarkable.rmapi")
    if cfg.rmapi.token_file.exists() and not args.replace:
        if not can_prompt(args) or args.stdin:
            raise SetupError("Your reMarkable is already connected; pass --replace to connect it again",
                             code="conflict")
        if input("Your reMarkable is already connected. Connect it again? [y/N] ").strip().lower() not in ("y", "yes"):
            return {"connected": True, "replaced": False}
    if not args.stdin and can_prompt(args):
        print(f"Get a one-time code at {CONNECT_URL} (sign in, then connect a desktop app).")
    code = read_secret(args, "One-time code")
    if not code:
        raise SetupError("No one-time code given", code="invalid")
    try:
        documents = register(cfg, code)
    except cloud.CloudError as e:
        raise SetupError(f"That code didn't work: {str(e).splitlines()[0]}. Codes expire after a few minutes; "
                         f"get a new one at {CONNECT_URL}", code="invalid") from e
    return {"connected": True, "replaced": True, "documents": documents}


def _render_connect(data: dict, console) -> None:
    if data.get("replaced") is False:
        console.print("Keeping the current connection.")
    else:
        console.print(f"[green]Connected:[/green] {data['documents']} document(s) in your library.")
