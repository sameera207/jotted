"""The Jotted widget for MCP Apps hosts (Claude Desktop): `ui://jotted/list`.

Plain HTML, CSS and JavaScript, written by hand (specs/Claude-widget-spec.md). The host
loads the widget into a sandboxed iframe, so it is served as one document, with the CSS
and scripts inlined: bridge.js (the only code that talks to the host), then list.js.
dev/ holds a fake host for working on it in a browser; it is never served.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

URI = "ui://jotted/list"
MIME = "text/html;profile=mcp-app"
HERE = Path(__file__).parent
SCRIPTS = ("bridge.js", "list.js")  # in this order: list.js uses Bridge


def _build(folder: Path) -> str:
    page = (folder / "list.html").read_text(encoding="utf-8")
    css = (folder / "list.css").read_text(encoding="utf-8")
    js = "\n".join((folder / name).read_text(encoding="utf-8") for name in SCRIPTS)
    for name, text in (("list.css", css), ("scripts", js)):
        if "</style" in text.lower() or "</script" in text.lower():
            raise ValueError(f"{name} would close its own tag when inlined")
    return page.replace("/* LIST_CSS */", css).replace("/* SCRIPTS */", js)


@lru_cache(maxsize=1)
def _packaged() -> str:
    return _build(HERE)


def load_html(ui_dir: str | Path | None = None) -> str:
    """The widget as one HTML document. From `ui_dir` it is read afresh on every call, so
    edits show without reinstalling (`jotted mcp --ui-dir`); otherwise once."""
    return _build(Path(ui_dir)) if ui_dir else _packaged()
