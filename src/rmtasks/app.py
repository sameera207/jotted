"""Composition root: builds the adapters and runs the background scheduler.

Everything that talks to the cloud goes through `sync.LOCK`: two rmapi processes at
once block each other.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from . import cloud, sync
from .adapters.jev_judge import JevActionJudge
from .adapters.remarkable_library import RemarkableLibrary
from .adapters.sqlite_repo import SqliteRepository
from .adapters.todo_document import TodoDocument
from .aicache import AICache
from .classify import ClassificationError
from .config import Config
from .core import service
from .notebook import NotebookError
from .recognise import RecognitionError
from .store import Store, to_utc

log = logging.getLogger("rmtasks")

SYNC_ERRORS = (cloud.CloudError, sync.SyncError, NotebookError, RecognitionError, ClassificationError)


@dataclass
class App:
    cfg: Config
    store: Store  # the Tasks notebook
    repo: SqliteRepository  # collected actions, settings, the To-do document
    source: RemarkableLibrary
    judge: JevActionJudge

    @classmethod
    def build(cls, cfg: Config, store: Store | None = None) -> "App":
        cache = AICache(cfg.paths.cache_dir / "ai")
        store = store or Store(cfg.server.db)
        return cls(cfg=cfg, store=store, repo=SqliteRepository(cfg.server.db),
                   source=RemarkableLibrary(cfg, cache), judge=JevActionJudge(cfg.classification, cache))

    def todo_document(self) -> TodoDocument:
        s = self.repo.settings()
        return TodoDocument(self.cfg, s.todo_name, s.todo_folder, self.source.cache)

    def own_doc_ids(self) -> set[str]:
        """Documents we write ourselves (the Tasks notebook, the To-do list): never collected."""
        ids = set()
        nb = self.store.notebook_by_name(self.cfg.notebook.name)
        if nb:
            ids.add(nb["id"])
        meta = self.repo.todo_meta()
        if meta.get("doc_id"):
            ids.add(meta["doc_id"])
        return ids

    def collect(self, progress=lambda _: None):
        return service.collect(self.source, self.judge, self.repo, exclude=self.own_doc_ids(), progress=progress)

    def sync_todo(self, force: bool = False) -> dict:
        if not self.repo.settings().todo_enabled:
            return {"enabled": False}
        doc = self.todo_document()
        result = service.sync_todo(self.repo, doc, force=force)
        found = cloud.find_document(self.cfg, doc.name, doc.folder)
        if found:
            with self.repo.db() as db:  # remember its id so the collector never reads it
                db.execute("INSERT INTO todo_meta (key, value) VALUES ('doc_id', ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (found.id,))
        return result

    def tasks_pull_if_changed(self) -> bool:
        """Pull the Tasks notebook when its cloud copy is newer than the stored one."""
        doc = cloud.find_document(self.cfg, self.cfg.notebook.name, self.cfg.notebook.folder)
        if doc is None:
            return False
        known = self.store.notebook(doc.id)
        if known and known["paper_modified"] and to_utc(doc.modified) <= known["paper_modified"]:
            return False
        sync.pull(self.cfg, self.store)
        return True

    def tasks_push_if_pending(self) -> bool:
        nb = self.store.notebook_by_name(self.cfg.notebook.name)
        if not nb or nb["file_type"] != "pdf":
            return False
        if not self.store.state(nb["id"])["pending_push"]:
            return False
        sync.push(self.cfg, self.store)
        return True


@dataclass
class Status:
    running: bool = False
    step: str = ""
    last_run_at: float | None = None
    last_summary: dict = field(default_factory=dict)
    last_error: str | None = None


class Scheduler:
    """One background loop for everything that reads from the cloud:
    the Tasks notebook, the watched folders, and ticks on the To-do document.
    `push_soon()` runs the writing side a few seconds after a web edit."""

    def __init__(self, app: App, push_delay_s: float):
        self.app = app
        self.status = Status()
        self.push_status = Status()
        self._push_delay = push_delay_s
        self._cond = threading.Condition()
        self._push_due: float | None = None
        self._poll_now = False

    def start(self) -> "Scheduler":
        threading.Thread(target=self._poll_loop, name="poll", daemon=True).start()
        threading.Thread(target=self._push_loop, name="push", daemon=True).start()
        return self

    # ------------------------------------------------------------ reading

    def poll_now(self) -> None:
        with self._cond:
            self._poll_now = True
            self._cond.notify_all()

    def poll_once(self) -> dict:
        summary: dict = {}
        with sync.LOCK:
            self.status.running = True
            try:
                self.status.step = "Checking the Tasks notebook"
                try:
                    summary["tasks_pulled"] = self.app.tasks_pull_if_changed()
                except cloud.CloudError as e:
                    summary["tasks_error"] = str(e)
                self.status.step = "Checking watched folders"
                collected = self.app.collect(progress=lambda m: setattr(self.status, "step", m))
                summary["collect"] = collected.as_dict()
                self.status.step = "Updating the To-do document"
                summary["todo"] = self.app.sync_todo()
            finally:
                self.status.running = False
                self.status.step = ""
        if summary.get("todo", {}).get("ticked"):
            self.push_soon()  # ticks on Task-notebook items need their strike-through printed
        return summary

    def _poll_loop(self) -> None:
        while True:
            try:
                self.status.last_summary = self.poll_once()
                errors = self.status.last_summary.get("collect", {}).get("errors") or []
                self.status.last_error = "; ".join(errors) or self.status.last_summary.get("tasks_error")
            except SYNC_ERRORS as e:
                log.error("background poll failed: %s", e)
                self.status.last_error = str(e)
            except Exception as e:  # keep the loop alive
                log.exception("background poll crashed")
                self.status.last_error = f"Unexpected error: {e}"
            self.status.last_run_at = time.time()
            interval = max(15, self.app.repo.settings().poll_interval_s)
            with self._cond:
                self._cond.wait_for(lambda: self._poll_now, timeout=interval)
                self._poll_now = False

    # ------------------------------------------------------------ writing

    def push_soon(self) -> None:
        with self._cond:
            self._push_due = time.monotonic() + self._push_delay
            self._cond.notify_all()

    def push_once(self) -> dict:
        with sync.LOCK:
            self.push_status.running = True
            try:
                return {"tasks_pushed": self.app.tasks_push_if_pending(), "todo": self.app.sync_todo()}
            finally:
                self.push_status.running = False

    def _push_loop(self) -> None:
        while True:
            with self._cond:
                while self._push_due is None or time.monotonic() < self._push_due:
                    self._cond.wait(None if self._push_due is None else max(0.05, self._push_due - time.monotonic()))
                self._push_due = None
            try:
                self.push_status.last_summary = self.push_once()
                self.push_status.last_error = None
            except SYNC_ERRORS as e:
                log.error("automatic push failed: %s", e)
                self.push_status.last_error = str(e)
            except Exception as e:
                log.exception("automatic push crashed")
                self.push_status.last_error = f"Unexpected error: {e}"
            self.push_status.last_run_at = time.time()

    def describe(self) -> dict:
        with self._cond:
            due = self._push_due
        return {
            "poll": {"running": self.status.running, "step": self.status.step,
                     "last_run_at": self.status.last_run_at, "last_error": self.status.last_error,
                     "last_summary": self.status.last_summary},
            "push": {"running": self.push_status.running, "scheduled": due is not None,
                     "in_s": max(0, round(due - time.monotonic())) if due is not None else None,
                     "last_error": self.push_status.last_error},
        }


__all__ = ["App", "Scheduler", "SYNC_ERRORS"]
