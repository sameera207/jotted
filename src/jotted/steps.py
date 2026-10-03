"""Setup as steps a wrapper can check and run one at a time.

Each step can say whether it is done (`check`), names the `jotted` command that completes
it, and may be done without asking anything (`prepare`, for `jotted setup prepare`). The
interactive walkthrough (`jotted setup`, `jotted start`) runs the same steps in order
with prompts (`walk`; see `jotted.onboarding`).

Steps: a home for settings and data, the source plugin's own (prefixed with its name:
`remarkable.rmapi`, `remarkable.connect`), the LLM's key, the optional Jev plugin, and a
watched folder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from . import config, keys
from .config import Config, ConfigMissing

if TYPE_CHECKING:
    from .ui import UI


@dataclass
class Walk:
    """What a step's interactive form gets."""
    ui: "UI"
    cfg: Config
    redo: bool  # `jotted setup`: ask again about what is already done
    fresh: bool  # config.toml was made just now


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    check: Callable[[Config | None], dict]  # {"done": bool, "detail"?: str, ...}; None: no config yet
    command: str | None = None  # the `jotted` command that completes it
    optional: bool = False
    prepare: Callable[[Config], str | None] | None = None  # done by `setup prepare`; returns a detail
    walk: Callable[[Walk], None] | None = None  # the walkthrough's form, asking the person


def _plugin_class(cfg: Config | None) -> Any:
    from . import plugins

    return plugins.plugin_class(cfg.plugins.source if cfg else config.PluginsConfig().source)


def _effective(cfg: Config) -> Config:
    from .app import effective

    return effective(cfg)


# ---------------------------------------------------------------- the core's steps


def _app_folder(cfg: Config | None) -> dict:
    return {"done": cfg is not None, "detail": str(cfg.source.parent) if cfg else None}


def _llm(cfg: Config | None) -> dict:
    if cfg is None:
        return {"done": False, "provider": config.LLMConfig().provider}
    cfg = _effective(cfg)
    key = keys.describe(cfg, cfg.llm.api_key_env)
    return {"done": key["set"], "provider": cfg.llm.provider, "model": cfg.llm.model,
            "detail": f"key {key['hint']} ({key['source']})" if key["set"] else None}


def _jev(cfg: Config | None) -> dict:
    if cfg is None:
        return {"done": False}
    key = keys.describe(cfg, cfg.jev.api_key_env)
    return {"done": key["set"], "detail": f"key {key['hint']} ({key['source']})" if key["set"] else None}


def _folders(cfg: Config | None) -> dict:
    if cfg is None or not cfg.server.db.is_file():
        return {"done": False}
    from .adapters.sqlite_repo import SqliteRepository

    watch = SqliteRepository(cfg.server.db).settings().watch
    return {"done": bool(watch), "detail": ", ".join(watch) or None}


def _walk_llm(w: Walk) -> None:
    from . import onboarding

    onboarding.llm_key(w.ui, _effective(w.cfg), w.redo)


def _walk_jev(w: Walk) -> None:
    from . import onboarding

    onboarding.jev(w.ui, w.cfg, ask=w.fresh or w.redo)


def steps(cfg: Config | None) -> list[Step]:
    """Every step in order: the core's first step, the source plugin's, then the rest."""
    return [
        Step("app_folder", "A home for settings and data", _app_folder, command="setup prepare"),
        *_plugin_class(cfg).setup_steps(),
        Step("llm", "The language model's key", _llm, command="ai key llm --stdin", walk=_walk_llm),
        Step("jev", "The Jev plugin", _jev, command="ai key jev --stdin", optional=True, walk=_walk_jev),
        Step("folders", "A watched folder", _folders, command="watch add PATH"),
    ]


def load_config() -> Config | None:
    """The config, or None before the first step."""
    try:
        cfg = config.load()
    except ConfigMissing:
        return None
    keys.load_into_env(cfg)
    return cfg


def status(cfg: Config | None) -> dict:
    """{complete, steps}: complete once every step that isn't optional is done."""
    out = []
    for s in steps(cfg):
        found = s.check(cfg)
        row = {"id": s.id, "title": s.title, "done": bool(found.pop("done")),
               **{k: v for k, v in found.items() if v is not None}}
        if s.optional:
            row["optional"] = True
        if not row["done"] and s.command:
            row["command"] = s.command
        out.append(row)
    return {"complete": all(r["done"] for r in out if not r.get("optional")), "steps": out}


def prepare() -> dict:
    """Do every step that needs no answer from the person: make the app folder (with
    config.toml), then the plugin's (for reMarkable: download rmapi, checksum checked)."""
    path = config.resolve_path()
    done: list[dict] = []
    if not path.is_file():
        config.create(path)
        done.append({"id": "app_folder", "detail": str(path.parent)})
    cfg = config.load(path)
    for s in steps(cfg):
        if s.prepare and not s.check(cfg)["done"]:
            done.append({"id": s.id, "detail": s.prepare(cfg)})
            cfg = config.load(path)  # a step may have written config.toml
    return {"prepared": done, **status(load_config())}


def first_missing(cfg: Config, ids: list[str] | None = None) -> Step | None:
    """The first step that isn't done (among `ids`, if given), skipping optional ones."""
    for s in steps(cfg):
        if s.optional or (ids is not None and s.id not in ids):
            continue
        if not s.check(cfg)["done"]:
            return s
    return None

