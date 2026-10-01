"""Local web app: see tasks from the tablet, change them, and sync both ways.

Runs on this machine only (127.0.0.1 by default) and has no login. Pull and push
take a while (download, Claude, Jev, upload); one runs at a time.
"""

from __future__ import annotations

import json
import logging
import threading
import zipfile
import time
from importlib import resources
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request

from . import cloud, strokes, sync, template
from .classify import ClassificationError
from .config import Config
from .notebook import NotebookError, page_order
from .recognise import RecognitionError
from .store import Store, to_utc

log = logging.getLogger("rmtasks")

SYNC_ERRORS = (cloud.CloudError, sync.SyncError, NotebookError, RecognitionError, ClassificationError)


class AutoPusher:
    """Push a few seconds after the last change, on a background thread.

    Each change resets the countdown, so a burst of edits becomes one push. A change
    made while a push runs schedules another one afterwards.
    """

    def __init__(self, delay_s: float, push):
        self.delay_s = delay_s
        self._push = push
        self._cond = threading.Condition()
        self._due: float | None = None
        self.running = False
        self.last_error: str | None = None
        self.last_done_at: float | None = None
        threading.Thread(target=self._loop, name="auto-push", daemon=True).start()

    def schedule(self) -> None:
        with self._cond:
            self._due = time.monotonic() + self.delay_s
            self._cond.notify()

    def status(self) -> dict:
        with self._cond:
            due = self._due
        return {
            "scheduled": due is not None,
            "in_s": max(0, round(due - time.monotonic())) if due is not None else None,
            "running": self.running,
            "last_error": self.last_error,
        }

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._due is None or time.monotonic() < self._due:
                    self._cond.wait(None if self._due is None else max(0.05, self._due - time.monotonic()))
                self._due = None
                self.running = True
            try:
                with sync.LOCK:
                    self._push()
                self.last_error = None
            except SYNC_ERRORS as e:
                log.error("automatic push failed: %s", e)
                self.last_error = str(e)
            except Exception as e:  # keep the worker alive whatever happens
                log.exception("automatic push crashed")
                self.last_error = f"Unexpected error: {e}"
            finally:
                self.running = False
                self.last_done_at = time.time()


class AutoPuller:
    """Check the cloud every `interval_s` and pull when the notebook changed since the last pull.

    The check is cheap (a library listing); the pull (download, reading, Jev) only runs when
    the tablet has synced something new. Skips a round when another sync holds the lock.
    """

    def __init__(self, interval_s: float, cfg: Config, store: Store):
        self.interval_s = interval_s
        self.cfg, self.store = cfg, store
        self.running = False
        self.last_check: float | None = None
        self.last_error: str | None = None
        threading.Thread(target=self._loop, name="auto-pull", daemon=True).start()

    def status(self) -> dict:
        return {"running": self.running, "last_check": self.last_check, "last_error": self.last_error}

    def check_once(self) -> bool:
        """Pull if the cloud copy is newer than the stored one. True when a pull ran.

        The check holds the sync lock too: two rmapi processes at once block each other.
        """
        if not sync.LOCK.acquire(blocking=False):
            return False  # another sync is running; check next round
        try:
            doc = cloud.find_notebook(self.cfg)
            known = self.store.notebook(doc.id)
            if known and known["paper_modified"] and to_utc(doc.modified) <= known["paper_modified"]:
                return False
            self.running = True
            sync.pull(self.cfg, self.store)
            return True
        finally:
            self.running = False
            sync.LOCK.release()

    def _loop(self) -> None:
        while True:
            try:
                if self.check_once():
                    log.info("pulled new handwriting from the tablet")
                self.last_error = None
            except SYNC_ERRORS as e:
                log.error("automatic pull failed: %s", e)
                self.last_error = str(e)
            except Exception as e:  # keep the worker alive whatever happens
                log.exception("automatic pull crashed")
                self.last_error = f"Unexpected error: {e}"
            self.last_check = time.time()
            time.sleep(self.interval_s)


def create_app(cfg: Config, store: Store | None = None, background: bool = True) -> Flask:
    app = Flask(__name__)
    store = store or Store(cfg.server.db)
    auto = AutoPusher(cfg.server.auto_push_delay_s, lambda: sync.push(cfg, store)) if cfg.server.auto_push else None
    puller = (AutoPuller(cfg.server.auto_pull_interval_s, cfg, store)
              if background and cfg.server.auto_pull_interval_s > 0 else None)

    def notebook_id() -> str | None:
        nb = store.notebook_by_name(cfg.notebook.name)
        return nb["id"] if nb else None

    def state() -> dict:
        nid = notebook_id()
        base = {"notebook_name": cfg.notebook.name, "notebook": None, "pages": [], "pending_push": False,
                "current_page": 1, "syncs": []}
        if nid:
            base.update(store.state(nid))
            base["syncs"] = store.recent_syncs(nid, 5)
        base["writable"] = bool(base["notebook"]) and base["notebook"]["file_type"] == "pdf"
        base["auto"] = auto.status() if auto else None
        base["auto_pull"] = puller.status() if puller else None
        return base

    def changed():
        """After a web edit: schedule a push when this notebook can be written to."""
        nb = store.notebook_by_name(cfg.notebook.name)
        if auto and nb and nb["file_type"] == "pdf":
            auto.schedule()

    def run_sync(fn):
        if not sync.LOCK.acquire(blocking=False):
            return jsonify(error="A sync is already running"), 409
        try:
            return fn()
        except SYNC_ERRORS as e:
            log.error("sync failed: %s", e)
            return jsonify(error=str(e), state=state()), 502
        finally:
            sync.LOCK.release()

    @app.get("/")
    def index():
        html = resources.files("rmtasks").joinpath("web/index.html").read_text()
        return Response(html, mimetype="text/html")

    @app.get("/api/state")
    def get_state():
        return jsonify(state())

    @app.post("/api/pull")
    def post_pull():
        def go():
            result = sync.pull(cfg, store)
            return jsonify(summary=result.summary.as_dict(), state=state())
        return run_sync(go)

    @app.post("/api/push")
    def post_push():
        def go():
            result = sync.push(cfg, store)
            return jsonify(strikes=result.strikes, moved=result.moved, footer=result.footer,
                           summary=result.pull.summary.as_dict(), state=state())
        return run_sync(go)

    @app.post("/api/tasks")
    def add_task():
        nid = notebook_id()
        if not nid:
            return jsonify(error="Pull from the tablet first, so the notebook is known"), 400
        text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
        if not text:
            return jsonify(error="Task text is empty"), 400
        task_id = store.add_web_task(nid, text)
        changed()
        return jsonify(task=store.task(task_id), state=state()), 201

    @app.patch("/api/tasks/<int:task_id>")
    def edit_task(task_id: int):
        body = request.get_json(silent=True) or {}
        try:
            task = store.edit_task(task_id, text=body.get("text"), status=body.get("status"))
        except KeyError:
            abort(404)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        changed()
        return jsonify(task=task, state=state())

    @app.delete("/api/tasks/<int:task_id>")
    def delete_task(task_id: int):
        try:
            store.delete_web_task(task_id)
        except KeyError:
            abort(404)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        changed()
        return jsonify(state=state())

    page_cache: dict[tuple, list] = {}

    def page_strokes(nid: str, index: int) -> list:
        """Strokes of one page, read straight from the cached .rmdoc (no unpacking)."""
        path = Path(cfg.paths.cache_dir) / f"{nid}.rmdoc"
        if not path.is_file():
            return []
        key = (str(path), path.stat().st_mtime_ns, index)
        if key not in page_cache:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                content = next((n for n in names if n.endswith(".content")), None)
                order = page_order(json.loads(z.read(content))) if content else []
                page_id = order[index - 1] if 0 < index <= len(order) else None
                member = next((n for n in names if page_id and n.endswith(f"{page_id}.rm")), None)
                result = []
                if member:
                    with z.open(member) as f:
                        result = strokes.load_strokes_from(f, member, cfg.strokes)
            page_cache.clear()  # keep one download's worth
            page_cache[key] = result
        return page_cache[key]

    @app.get("/api/pages/<int:page>/preview.svg")
    def page_preview(page: int):
        """What the tablet shows after the next push: template, ink and everything we print."""
        nb = store.notebook_by_name(cfg.notebook.name)
        if not nb:
            abort(404)
        pages, _ = store.page_states(nb["id"], pin=False)
        svg = template.render_svg(page_strokes(nb["id"], page), pages.get(page, template.PageState()),
                                  cfg.template, templated=nb["file_type"] == "pdf")
        return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/api/pages/<int:page>.svg")
    def page_svg(page: int):
        """The overlay from the latest pull, for checking what was read."""
        nid = notebook_id()
        pulls = [s for s in (store.recent_syncs(nid, 20) if nid else []) if s["run_id"]]
        if not pulls:
            abort(404)
        svg = Path(cfg.paths.output_dir) / pulls[0]["run_id"] / f"page-{page:02d}.svg"
        if not svg.is_file():
            abort(404)
        return Response(svg.read_text(), mimetype="image/svg+xml")

    return app
