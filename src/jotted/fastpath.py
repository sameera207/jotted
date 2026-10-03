"""The CLI's fast path: hand a command to a running `jotted serve` instead of starting the work here.

Starting Python and importing the model SDK, Flask and reportlab takes about a second, which
is slow for a click in the desktop app. While `jotted serve` runs it writes
`<data_dir>/serve.json` ({pid, port, token, version, host}, readable by this user only); a
command finds it, checks the server is alive and the same version, and sends
`POST /api/op/<operation>` with its arguments. The envelope that comes back is exactly what
the command would have printed itself.

Nothing here imports more than the standard library: that is the point.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .contract import fail, release

FILE = "serve.json"
LOCAL = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}


def path(cfg) -> Path:
    return Path(cfg.server.db).parent / FILE


def write(cfg, host: str, port: int, token: str) -> Path:
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "port": int(port), "token": token, "version": release(), "host": host}, f)
    os.replace(tmp, p)
    return p


def remove(cfg) -> None:
    """Remove serve.json if this process wrote it."""
    found = read(cfg)
    if found and found.get("pid") == os.getpid():
        path(cfg).unlink(missing_ok=True)


def read(cfg) -> dict | None:
    try:
        data = json.loads(path(cfg).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, but someone else's
        return False
    return True


def server(cfg) -> dict | None:
    """The running server to hand commands to: alive, and the same version as this CLI."""
    found = read(cfg)
    if not found or not _alive(found.get("pid")) or found.get("version") != release():
        return None
    if not isinstance(found.get("port"), int) or not isinstance(found.get("token"), str):
        return None
    return found


def send(cfg, op: str, kwargs: dict) -> dict | None:
    """Run `op` on the running server and return its envelope; None when there is no server
    to ask (the caller runs the command itself)."""
    found = server(cfg)
    if found is None:
        return None
    host = LOCAL.get(found.get("host") or "", found.get("host") or "127.0.0.1")
    url = f"http://{'[' + host + ']' if ':' in host else host}:{found['port']}/api/op/{op}"
    req = urllib.request.Request(url, data=json.dumps(kwargs).encode(), method="POST", headers={
        "Content-Type": "application/json", "X-Jotted-Token": found["token"]})
    try:
        with urllib.request.urlopen(req, timeout=None) as resp:  # noqa: S310 - this machine
            envelope = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            envelope = json.loads(e.read())
        except ValueError:
            envelope = None
        if not (isinstance(envelope, dict) and "ok" in envelope):
            return fail("internal", f"The running Jotted answered {e.code} to {op}")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (ConnectionRefusedError, FileNotFoundError)):
            return None  # it stopped without removing serve.json: nothing was sent
        return fail("internal", f"Lost the running Jotted during {op}: {e.reason}")
    except (OSError, ValueError) as e:
        return fail("internal", f"Lost the running Jotted during {op}: {e}")
    return envelope if isinstance(envelope, dict) and "ok" in envelope else fail(
        "internal", f"The running Jotted gave no answer to {op}")
