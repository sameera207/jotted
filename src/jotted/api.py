"""Jotted's application layer: every operation the product offers, in one place.

The CLI, the local web server and any other front end (a desktop app, say) are thin
callers of `Jotted`: they parse input, call one method, and show the result. Nothing
here knows about HTTP or the terminal, and nothing outside knows how an operation is
done. Results are plain dicts and lists, ready for JSON: that is the contract front
ends rely on.

Each public operation is registered in `OPERATIONS` with `@operation`; a test checks
that every one is reachable from the CLI, and every web route maps to one.

Errors are `ApiError`s with a message for the person and an HTTP-like status.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, fields, replace
from typing import Any, Callable

from . import classify, keys, llm
from .app import SYNC_ERRORS, App, Scheduler
from .config import Config
from .core import service
from .core.model import DocInfo, Settings
from .locking import Busy

log = logging.getLogger("jotted")

OPERATIONS: dict[str, str] = {}  # operation name -> method name

SOURCE_WAIT_S = 60  # how long a request waits for the source to be free


class ApiError(Exception):
    status = 400


class Invalid(ApiError):
    status = 400


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


class Unavailable(ApiError):
    """The source or a model failed, or couldn't be reached."""
    status = 502


def operation(name: str) -> Callable:
    def register(fn: Callable) -> Callable:
        OPERATIONS[name] = fn.__name__
        fn.operation = name
        return fn
    return register


def _settings_from(changes: dict, current: Settings) -> Settings:
    """Validated settings: `current` with `changes` applied."""
    names = {f.name for f in fields(Settings)}
    values = asdict(current)
    for k, v in changes.items():
        if k not in names:
            raise Invalid(f"unknown setting {k!r}")
        values[k] = v
    try:
        s = Settings(**values)
        s.action_threshold = float(s.action_threshold)
        s.poll_interval_s = int(s.poll_interval_s)
    except (TypeError, ValueError) as e:
        raise Invalid(str(e)) from e
    if not 0 <= s.action_threshold <= 1:
        raise Invalid("action_threshold must be between 0 and 1")
    if s.poll_interval_s < 15:
        raise Invalid("poll_interval_s must be at least 15 seconds")
    if not isinstance(s.watch, list) or not all(isinstance(w, str) for w in s.watch):
        raise Invalid("watch must be a list of folder or document paths")
    s.watch = sorted({_path(w) for w in s.watch})
    if not isinstance(s.from_now, list) or not all(isinstance(d, str) for d in s.from_now):
        raise Invalid("from_now must be a list of document IDs")
    s.from_now = sorted(set(s.from_now))
    for flag in ("include_others", "todo_enabled"):
        if not isinstance(getattr(s, flag), bool):
            raise Invalid(f"{flag} must be true or false")
    if not isinstance(s.todo_name, str) or not s.todo_name.strip():
        raise Invalid("todo_name is empty")
    s.todo_name = s.todo_name.strip()
    s.todo_folder = _path(s.todo_folder) if isinstance(s.todo_folder, str) else "/"
    return s


def _path(p: str) -> str:
    return "/" + p.strip("/") if p.strip("/") else "/"


class Jotted:
    """The application. `on_change` runs after an edit that should reach the To-do document
    (the web server schedules an update; the CLI leaves it to `jotted todo` or the server)."""

    def __init__(self, app: App, scheduler: Scheduler | None = None, on_change: Callable[[], None] | None = None):
        self.app, self.cfg, self.repo = app, app.cfg, app.repo
        self.scheduler = scheduler
        self._on_change = on_change or (lambda: None)

    @classmethod
    def open(cls, cfg: Config, **kw: Any) -> "Jotted":
        return cls(App.build(cfg), **kw)

    def _source_job(self, what: str, fn: Callable[[], Any], wait: float | None = SOURCE_WAIT_S) -> Any:
        """Run `fn` holding the source lock; turn source and model failures into ApiErrors."""
        try:
            with self.app.lock.held(wait, what):
                return fn()
        except Busy as e:
            raise Conflict(str(e)) from e
        except SYNC_ERRORS as e:
            raise Unavailable(str(e)) from e

    # ------------------------------------------------------------ the to-do list

    @operation("items.list")
    def items(self, status: str | None = None, owner: str | None = None, folder: str | None = None) -> list[dict]:
        if status not in (None, "", "open", "done"):
            raise Invalid("status must be open or done")
        if owner not in (None, "", "mine", "others"):
            raise Invalid("owner must be mine or others")
        return self.repo.items(status=status or None, owner=owner or None, folder=folder or None)

    @operation("items.add")
    def add_item(self, text: str) -> dict:
        try:
            item_id = self.repo.add_item(text if isinstance(text, str) else "")
        except ValueError as e:
            raise Invalid(str(e)) from e
        self._on_change()
        return self.repo.item(item_id)

    @operation("items.edit")
    def edit_item(self, item_id: int, *, text: str | None = None, status: str | None = None,
                  dismissed: bool | None = None) -> dict:
        """Change an item's text or status, or dismiss it (not an action; or delete one added here).
        Returns the item, or {} once dismissed."""
        try:
            item = self.repo.edit_action(item_id, text=text, status=status, dismissed=dismissed)
        except KeyError:
            raise NotFound(f"no item {item_id}") from None
        except ValueError as e:
            raise Invalid(str(e)) from e
        self._on_change()
        return item

    def overview(self, status: str | None = None, owner: str | None = None, folder: str | None = None) -> dict:
        """Everything the to-do page shows at once: `items` plus what explains them."""
        return {"items": self.items(status, owner, folder), "settings": self.settings(),
                "background": self.scheduler.describe() if self.scheduler else None,
                "todo": self.repo.todo_meta(), "sources": self.repo.source_docs(), "source": self.source()}

    # ------------------------------------------------------------ settings

    @operation("settings.get")
    def settings(self) -> dict:
        return asdict(self.repo.settings())

    @operation("settings.update")
    def update_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise Invalid("settings must be an object")
        s = _settings_from(changes, self.repo.settings())
        self.repo.save_settings(s)
        if self.scheduler:
            self.scheduler.poll_now()  # pick up new folders without waiting for the next round
        return asdict(s)

    @operation("watch.add")
    def watch(self, path: str) -> dict:
        s = self.repo.settings()
        return self.update_settings({"watch": s.watch + [_path(path)]})

    @operation("watch.remove")
    def unwatch(self, path: str) -> dict:
        s = self.repo.settings()
        return self.update_settings({"watch": [w for w in s.watch if w != _path(path)]})

    @operation("watch.from_now")
    def from_now(self, path: str, on: bool = True) -> dict:
        """Skip (on) or read again (off) what is already written in the documents at `path`
        (a document, or every document in a folder now). Returns the settings, the documents
        affected, and those already read in full, where there is nothing left to skip."""
        target = _path(path)
        docs = [d for d in self._documents() if target == "/" or d.path == target or d.path.startswith(target + "/")]
        if not docs:
            raise NotFound(f"no documents at {target}")
        ids = {d.id for d in docs}
        s = self.repo.settings()
        late: list[str] = []
        if on:
            read = {d["id"] for d in self.repo.source_docs() if d["marker"]}
            late = [d.path for d in docs if d.id in read and d.id not in self.repo.baselined_docs()]
            settings = self.update_settings({"from_now": sorted(set(s.from_now) | ids)})
        else:
            settings = self.update_settings({"from_now": sorted(set(s.from_now) - ids)})
        return {"settings": settings, "documents": [d.path for d in docs], "already_read": late}

    # ------------------------------------------------------------ the source's library

    def _documents(self) -> list[DocInfo]:
        return self._source_job("The library", self.app.source.list_documents)

    @operation("library")
    def library(self) -> dict:
        """Folders and documents for the watch picker, with what each watch would cover."""
        docs, folders = self._source_job(
            "The library", lambda: (self.app.source.list_documents(), self.app.source.folders()))
        own = self.app.own_doc_ids()
        counts = {f: sum(1 for d in docs if (d.folder + "/").startswith(f.rstrip("/") + "/")) for f in folders}
        read = {d["id"] for d in self.repo.source_docs() if d["marker"]}  # fully collected at least once
        baseline = self.repo.baseline_pages()
        watched = self.repo.settings()
        return {
            "folders": [{"path": f, "documents": counts[f],
                         "watched": watched.watches(DocInfo("", "", "x", f, ""))} for f in folders],
            "documents": [{"path": d.path, "folder": d.folder, "id": d.id, "own": d.id in own,
                           "read": d.id in read, "baseline_pages": baseline.get(d.id, 0),
                           "watched": watched.watches(d)} for d in docs],
        }

    @operation("pending")
    def pending(self, fetch: bool = False) -> list[dict]:
        """What the next collection would read: changed documents and, with `fetch`, their
        changed pages (downloads only; nothing is read or judged)."""
        todo = self._source_job("The library", lambda: service.pending(
            self.app.source, self.repo, exclude=self.app.own_doc_ids(), fetch=fetch))
        return [{"path": d.path, "id": d.id, "pages": [p.index for p in pages]} for d, pages in todo]

    # ------------------------------------------------------------ work

    @operation("collect")
    def collect(self, progress: Callable[[str], None] = lambda _: None, wait: float | None = None) -> dict:
        """Read what changed in the watched documents now."""
        if not self.repo.settings().watch:
            raise Invalid("Nothing is watched yet. Watch a folder first (`jotted watch add PATH`, or Settings).")
        return self._source_job("The source", lambda: self.app.collect(progress=progress).as_dict(), wait)

    @operation("todo.sync")
    def sync_todo(self, force: bool = False, wait: float | None = None) -> dict:
        """Read ticks and new items from the To-do document, then republish it if needed."""
        if not self.repo.settings().todo_enabled:
            raise Invalid("The To-do document is off. Turn it on with `jotted settings set todo_enabled true` "
                          "or in Settings.")
        return self._source_job("The source", lambda: self.app.sync_todo(force=force), wait)

    @operation("check")
    def check(self) -> dict:
        """Check now: in the background when a scheduler runs (the web server), else right here."""
        if self.scheduler:
            self.scheduler.poll_now()
            return {"background": self.scheduler.describe()}
        out: dict = {}
        if self.repo.settings().watch:
            out["collect"] = self.collect(wait=None)
        if self.repo.settings().todo_enabled:
            out["todo"] = self.sync_todo(wait=None)
        return out

    @operation("status")
    def status(self) -> dict:
        s = self.repo.settings()
        items = self.repo.items()
        docs = self.repo.source_docs()
        meta = self.repo.todo_meta()
        return {
            "source": self.source(),
            "judge": "jev" if classify.jev_enabled(self.cfg) else "llm",
            "watch": s.watch,
            "documents_read": sum(1 for d in docs if d["marker"]),
            "last_collected_at": max((d["collected_at"] for d in docs if d["collected_at"]), default=None),
            "items": {"open": sum(1 for i in items if i["status"] == "open"),
                      "done": sum(1 for i in items if i["status"] == "done")},
            "todo": {"enabled": s.todo_enabled, "name": s.todo_name, "folder": s.todo_folder,
                     "published_at": meta.get("published_at")},
            "background": self.scheduler.describe() if self.scheduler else None,
        }

    @operation("source")
    def source(self) -> dict:
        p = self.app.plugin
        return {"name": p.NAME, "label": p.LABEL, "mark": p.MARK, "device": p.DEVICE, **p.describe()}

    # ------------------------------------------------------------ where an item came from

    @operation("page.image")
    def page_image(self, doc_id: str, page: int, anchor: str | None = None) -> str:
        """A source page as SVG, with the line `anchor` highlighted."""
        page_id = self.repo.page_id(doc_id, page)
        if not page_id:
            raise NotFound(f"no page {page} of {doc_id}")
        line = self.repo.source_line(doc_id, anchor) if anchor else None
        svg = self.app.plugin.render_page(doc_id, page_id, highlight=line["rows"] if line else None)
        if svg is None:
            raise NotFound("this source can't draw its pages")
        return svg

    @operation("line.image")
    def line_image(self, doc_id: str, anchor: str) -> str:
        """One handwritten line as SVG."""
        line = self.repo.source_line(doc_id, anchor)
        if not line:
            raise NotFound(f"no line {anchor} in {doc_id}")
        x0, y0, x1, y1 = line["bbox"]
        svg = self.app.plugin.render_page(doc_id, line["page_id"], crop=(x0 - 16, y0 - 12, x1 + 16, y1 + 12))
        if svg is None:
            raise NotFound("this source can't draw its pages")
        return svg

    # ------------------------------------------------------------ AI: the LLM and the Jev plugin

    @staticmethod
    def _jev_class() -> type:
        from .adapters.typesafe_judge import TypeSafeJudge

        return TypeSafeJudge

    def _ai_parts(self, which: str):
        if which == "llm":
            cls = llm.llm_class(self.cfg.llm)
            return self.cfg.llm.api_key_env, cls, lambda: cls(self.cfg.llm)
        if which == "jev":
            cls = self._jev_class()
            return self.cfg.jev.api_key_env, cls, lambda: cls(self.cfg.jev)
        raise NotFound(f"no AI part {which!r}; use llm or jev")

    @operation("ai.get")
    def ai(self) -> dict:
        cfg = self.cfg
        cls, jev = llm.llm_class(cfg.llm), self._jev_class()
        return {
            "llm": {"provider": cfg.llm.provider, "label": cls.LABEL, "family": cls.MODEL_FAMILY,
                    "model": cfg.llm.model, "key_url": cls.KEY_URL, "key": keys.describe(cfg, cfg.llm.api_key_env),
                    "providers": [{"id": p, "label": llm.llm_class(replace(cfg.llm, provider=p)).LABEL}
                                  for p in sorted(llm.PROVIDERS)]},
            "jev": {"name": jev.NAME, "by": jev.LABEL, "model": cfg.jev.model, "key_url": jev.KEY_URL,
                    "key": keys.describe(cfg, cfg.jev.api_key_env), "enabled": classify.jev_enabled(cfg)},
            "judge": "jev" if classify.jev_enabled(cfg) else "llm",
        }

    @operation("ai.set_key")
    def set_key(self, which: str, value: str) -> dict:
        """Check a key with its provider, then save it. Turning Jev on is adding its key."""
        name, cls, make = self._ai_parts(which)
        value = value.strip() if isinstance(value, str) else ""
        if not value:
            raise Invalid("Paste a key first")
        previous = os.environ.get(name)
        os.environ[name] = value
        try:
            make().verify()
        except llm.ModelError as e:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
            if "rejected" in str(e):
                raise Invalid(f"{e}. Check it was copied in full.") from e
            raise Unavailable(f"{e}. The key wasn't saved.") from e
        keys.save(self.cfg, name, value)
        log.info("%s key saved", cls.LABEL)
        return self.ai()

    @operation("ai.remove_key")
    def remove_key(self, which: str) -> dict:
        """Turn the Jev plugin off. The LLM's key can be replaced but not removed: nothing works without it."""
        name, cls, _ = self._ai_parts(which)
        if which == "llm":
            raise Invalid(f"Jotted needs a {cls.LABEL} key to read handwriting; replace it instead")
        if keys.describe(self.cfg, name)["source"] == "environment":
            raise Conflict(f"{name} is exported in the shell that started Jotted. Remove it there, "
                           "then start Jotted again.")
        keys.remove(self.cfg, name)
        return self.ai()
