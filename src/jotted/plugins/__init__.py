"""Source plugins: where notes come from, and where the to-do list can be printed back.

A plugin connects one kind of device or service (the reMarkable cloud today). It owns
everything specific to it: talking to the service, its file formats, its config
sections, its setup steps and its CLI commands. Jotted's core only sees the ports it
returns (`core.ports.DocumentSource`, `core.ports.TodoPublisher`).

Plugins for handwriting turn their pages into `jotted.ink` strokes and hand them to
`Host.ink`, which clusters them into lines, has the LLM read them and returns
`SourceLine`s; a plugin for typed notes builds `SourceLine`s itself.

Register a plugin in another package with an entry point in the "jotted.sources" group:

    [project.entry-points."jotted.sources"]
    supernote = "jotted_supernote:SupernotePlugin"

and choose it with `[plugins] source = "supernote"` in config.toml.
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from ..core.ports import DocumentSource, TodoPublisher

if TYPE_CHECKING:
    from ..config import Config
    from ..ink.reader import InkReader
    from ..ui import UI

GROUP = "jotted.sources"
BUILTIN = {"remarkable": "jotted.plugins.remarkable:RemarkablePlugin"}

BBox = tuple[float, float, float, float]


@dataclass
class Host:
    """What Jotted gives a plugin."""
    ink: "InkReader"  # strokes -> lines, read by the LLM (cached); for handwriting plugins


class SourcePlugin(Protocol):
    NAME: ClassVar[str]  # "remarkable": stored with every item it finds
    LABEL: ClassVar[str]  # "reMarkable"
    MARK: ClassVar[str]  # one or two characters for the web app's source mark: "rM"
    DEVICE: ClassVar[str]  # how the app refers to it in a sentence: "your reMarkable"
    SECTIONS: ClassVar[dict[str, type]]  # config sections it owns: name -> frozen dataclass

    @classmethod
    def validate(cls, cfg: "Config") -> None:
        """Check its config sections; raise config.ConfigError."""

    def __init__(self, cfg: "Config", host: Host): ...

    def source(self) -> DocumentSource:
        """Where the documents come from."""

    def publisher(self, name: str, folder: str) -> TodoPublisher | None:
        """The To-do document called `name` in `folder`; None if the device can't show one."""

    def render_page(self, doc_id: str, page_id: str, highlight: list[BBox] | None = None,
                    crop: BBox | None = None) -> str | None:
        """A page as SVG for the web app (`highlight`/`crop` in the plugin's own units, as its
        SourceLines gave them); None if it can't be drawn."""

    def setup(self, ui: "UI", redo: bool) -> bool:
        """Setup steps (install tools, connect an account). True if it changed config.toml."""

    @classmethod
    def cli(cls, sub: argparse._SubParsersAction) -> None:
        """Add its own subcommands; each sets `func(cfg, args) -> int`. A classmethod: the
        command line is built before any config is read."""

    def describe(self) -> dict[str, Any]:
        """For the web app and `jotted status`: {connected: bool, detail: str}."""


class PluginError(Exception):
    pass


def available() -> dict[str, str]:
    """Installed source plugins: name -> "module:attribute"."""
    found = dict(BUILTIN)
    for ep in entry_points(group=GROUP):
        found.setdefault(ep.name, ep.value)
    return found


def plugin_class(name: str) -> type[SourcePlugin]:
    target = available().get(name)
    if target is None:
        raise PluginError(f"no source plugin {name!r}; installed: {sorted(available())}")
    module, attr = target.split(":")
    return getattr(importlib.import_module(module), attr)


def sections() -> dict[str, tuple[type, type]]:
    """Config sections owned by installed plugins: name -> (plugin class, section type)."""
    out: dict[str, tuple[type, type]] = {}
    for name in available():
        cls = plugin_class(name)
        for section, section_cls in cls.SECTIONS.items():
            out[section] = (cls, section_cls)
    return out
