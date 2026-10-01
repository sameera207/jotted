"""Setting up the reMarkable plugin: rmapi, then the connection to the reMarkable cloud.
Run by `jotted start`/`jotted setup` through the plugin, and `jotted auth` on its own."""

from __future__ import annotations

import argparse
import getpass
import shutil

from ... import config
from ...config import Config
from ...ui import UI, SetupError
from . import cloud, rmapi_install

CONNECT_URL = "https://my.remarkable.com/device/desktop/connect"
TRIES = 3


def run(ui: UI, cfg: Config, redo: bool) -> bool:
    """True if config.toml changed (rmapi was installed)."""
    changed = install_rmapi(ui, cfg)
    if changed:
        cfg = config.load(cfg.source)
    connect(ui, cfg, redo)
    return changed


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
                         "then run `jotted start` again")
    try:
        binary = rmapi_install.install(cfg.source.parent / "bin")
    except rmapi_install.InstallError as e:
        raise SetupError(str(e)) from e
    config.set_value(cfg.source, "rmapi", "binary", str(binary))
    ui.done(f"rmapi installed: {binary}")
    return True


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


def cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    """`jotted auth`: connect to the reMarkable cloud with a one-time code."""
    token = cfg.rmapi.token_file
    if token.exists():
        answer = input(f"A token already exists at {token}. Replace it? [y/N] ").strip().lower()
        if answer != "y":
            print("Keeping the existing token.")
            return 0
        token.unlink()
    print(f"Get a one-time code at {CONNECT_URL} (sign in, then connect a desktop app).")
    code = getpass.getpass("One-time code: ")
    cloud.register(cfg, code)
    print(f"Registered. Token saved to {token} (keep it secret).")
    return 0
