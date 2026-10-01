"""The reMarkable plugin: notes from the reMarkable cloud, and the To-do document on the tablet.

Everything specific to reMarkable lives here: rmapi (`cloud`, `rmapi_install`), .rm pages
(`rmfile`), the library (`library`), the printed To-do document (`todo_document`), page
drawing (`page`), setup and `jotted auth` (`setup`), and the [rmapi], [strokes] and
[template] config sections (`settings`).
"""

from __future__ import annotations

import argparse
from typing import Any

from ...config import Config
from ...ui import UI
from .. import BBox, Host
from . import page, settings, setup
from .library import RemarkableLibrary
from .todo_document import TodoDocument


class RemarkablePlugin:
    NAME = "remarkable"
    LABEL = "reMarkable"
    MARK = "rM"
    DEVICE = "your reMarkable"
    SECTIONS = settings.SECTIONS

    @classmethod
    def validate(cls, cfg: Config) -> None:
        settings.validate(cfg)

    def __init__(self, cfg: Config, host: Host):
        self.cfg, self.host = cfg, host
        self._library: RemarkableLibrary | None = None

    def source(self) -> RemarkableLibrary:
        if self._library is None:  # one, so its listing cache is shared
            self._library = RemarkableLibrary(self.cfg, self.host.ink)
        return self._library

    def publisher(self, name: str, folder: str) -> TodoDocument:
        return TodoDocument(self.cfg, name, folder, self.host.ink)

    def render_page(self, doc_id: str, page_id: str, highlight: list[BBox] | None = None,
                    crop: BBox | None = None) -> str | None:
        strokes = self.source().page_strokes(doc_id, page_id)
        return page.render_svg(strokes, self.cfg.template.scale, highlight=highlight, crop=crop)

    def setup(self, ui: UI, redo: bool) -> bool:
        return setup.run(ui, self.cfg, redo)

    @classmethod
    def cli(cls, sub: argparse._SubParsersAction) -> None:
        sub.add_parser("auth", help="connect to the reMarkable cloud with a one-time code").set_defaults(
            func=setup.cmd_auth)

    def describe(self) -> dict[str, Any]:
        token = self.cfg.rmapi.token_file
        return {"connected": token.exists(),
                "detail": "connected through rmapi" if token.exists() else "not connected: run `jotted auth`"}
