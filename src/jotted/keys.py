"""API keys saved by `jotted setup`: a JSON file in the secrets folder, readable by this
user only. A key exported in the shell wins over a saved one, so nothing changes for
people who already use environment variables."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Config

FILE = "api-keys.json"


def path(cfg: Config) -> Path:
    return cfg.paths.secrets_dir / FILE


def saved(cfg: Config) -> dict[str, str]:
    p = path(cfg)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def load_into_env(cfg: Config) -> None:
    """Make saved keys visible to the providers, which read them from the environment."""
    for name, value in saved(cfg).items():
        os.environ.setdefault(name, value)


def save(cfg: Config, name: str, value: str) -> None:
    keys = saved(cfg) | {name: value}
    folder = cfg.paths.secrets_dir
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    tmp = folder / (FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(keys, f, indent=2)
    os.replace(tmp, path(cfg))
    os.chmod(path(cfg), 0o600)
    os.environ[name] = value
