"""Local web app (a driving adapter): the common to-do list, the Tasks notebook, and settings.

Runs on this machine only (127.0.0.1 by default) and has no login. Reading from and
writing to the tablet run in the background (`app.Scheduler`); one at a time.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, fields
from importlib import resources
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, session

from . import auth, sync, template
from .app import SYNC_ERRORS, App, Scheduler
from .config import Config
from .core import service
from .core.model import Settings
from .store import Store

log = logging.getLogger("rmtasks")


def create_app(cfg: Config, store: Store | None = None, background: bool = True, app_: App | None = None) -> Flask:
    flask = Flask(__name__)
    auth_cfg = auth.setup(flask)
    core = app_ or App.build(cfg, store)
    store = core.store
    scheduler = Scheduler(core, cfg.server.auto_push_delay_s)
    if background:
        scheduler.start()
    flask.config["scheduler"] = scheduler

    # ------------------------------------------------------------ the Tasks notebook

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
        base["background"] = scheduler.describe()
        return base

    def changed():
        """After a web edit: write to the tablet a few seconds later (debounced)."""
        if cfg.server.auto_push:
            scheduler.push_soon()

    def run_sync(fn):
        if not sync.LOCK.acquire(blocking=False):
            return jsonify(error="A sync is already running; try again in a moment"), 409
        try:
            return fn()
        except SYNC_ERRORS as e:
            log.error("sync failed: %s", e)
            return jsonify(error=str(e), state=state()), 502
        finally:
            sync.LOCK.release()

    @flask.get("/")
    def index():
        html = resources.files("rmtasks").joinpath("web/index.html").read_text()
        return Response(html, mimetype="text/html")

    @flask.get("/api/state")
    def get_state():
        return jsonify(state())

    @flask.post("/api/pull")
    def post_pull():
        def go():
            result = sync.pull(cfg, store)
            return jsonify(summary=result.summary.as_dict(), state=state())
        return run_sync(go)

    @flask.post("/api/push")
    def post_push():
        def go():
            result = sync.push(cfg, store)
            return jsonify(strikes=result.strikes, moved=result.moved, footer=result.footer,
                           summary=result.pull.summary.as_dict(), state=state())
        return run_sync(go)

    @flask.post("/api/tasks")
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

    @flask.patch("/api/tasks/<int:task_id>")
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

    @flask.delete("/api/tasks/<int:task_id>")
    def delete_task(task_id: int):
        try:
            store.delete_web_task(task_id)
        except KeyError:
            abort(404)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        changed()
        return jsonify(state=state())

    @flask.get("/api/pages/<int:page>/preview.svg")
    def page_preview(page: int):
        """The Tasks notebook page as the tablet shows it after the next push."""
        nb = store.notebook_by_name(cfg.notebook.name)
        if not nb:
            abort(404)
        pages, _ = store.page_states(nb["id"], pin=False)
        page_id = _page_id_from_cache(cfg, nb["id"], page)
        strokes_ = core.source.page_strokes(nb["id"], page_id) if page_id else []
        svg = template.render_svg(strokes_, pages.get(page, template.PageState()), cfg.template,
                                  templated=nb["file_type"] == "pdf")
        return _svg(svg)

    @flask.get("/api/pages/<int:page>.svg")
    def page_svg(page: int):
        """The debug overlay from the latest pull: what was read."""
        nid = notebook_id()
        pulls = [s for s in (store.recent_syncs(nid, 20) if nid else []) if s["run_id"]]
        if not pulls:
            abort(404)
        svg = Path(cfg.paths.output_dir) / pulls[0]["run_id"] / f"page-{page:02d}.svg"
        if not svg.is_file():
            abort(404)
        return _svg(svg.read_text())

    # ------------------------------------------------------------ the common to-do list

    def todo_state() -> dict:
        args = request.args
        items = core.repo.items(status=args.get("status") or None, owner=args.get("owner") or None,
                                folder=args.get("folder") or None)
        return {"items": items, "settings": asdict(core.repo.settings()), "background": scheduler.describe(),
                "auth": auth_cfg.enabled, "user": session.get("email"),
                "todo": core.repo.todo_meta(), "sources": core.repo.source_docs()}

    @flask.get("/api/todo")
    def get_todo():
        return jsonify(todo_state())

    @flask.patch("/api/items/<kind>/<int:item_id>")
    def edit_item(kind: str, item_id: int):
        body = request.get_json(silent=True) or {}
        try:
            if kind == "action":
                core.repo.edit_action(item_id, text=body.get("text"), status=body.get("status"),
                                      dismissed=body.get("dismissed"))
            elif kind == "task":
                store.edit_task(item_id, text=body.get("text"), status=body.get("status"))
            else:
                abort(404)
        except (KeyError, StopIteration):
            abort(404)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        changed()
        return jsonify(todo_state())

    @flask.post("/api/poll")
    def post_poll():
        """Check the tablet now (Tasks notebook, watched folders, ticks) in the background."""
        scheduler.poll_now()
        return jsonify(background=scheduler.describe()), 202

    @flask.get("/api/settings")
    def get_settings():
        return jsonify(asdict(core.repo.settings()))

    @flask.put("/api/settings")
    def put_settings():
        body = request.get_json(silent=True) or {}
        current = asdict(core.repo.settings())
        names = {f.name: f for f in fields(Settings)}
        for k, v in body.items():
            if k not in names:
                return jsonify(error=f"unknown setting {k!r}"), 400
            current[k] = v
        try:
            s = Settings(**current)
            s.action_threshold = float(s.action_threshold)
            s.poll_interval_s = int(s.poll_interval_s)
            if not 0 <= s.action_threshold <= 1:
                raise ValueError("action_threshold must be between 0 and 1")
            if s.poll_interval_s < 15:
                raise ValueError("poll_interval_s must be at least 15 seconds")
            s.watch = sorted({"/" + w.strip("/") if w.strip("/") else "/" for w in s.watch})
            if not s.todo_name.strip():
                raise ValueError("todo_name is empty")
        except (TypeError, ValueError) as e:
            return jsonify(error=str(e)), 400
        core.repo.save_settings(s)
        scheduler.poll_now()  # pick up new folders without waiting for the next round
        return jsonify(asdict(s))

    @flask.get("/api/library")
    def get_library():
        """Folders and documents for the settings picker, with what each watch would cover."""
        if not sync.LOCK.acquire(timeout=60):
            return jsonify(error="The tablet is busy syncing; try again in a moment"), 409
        try:
            docs = core.source.list_documents()
            folders = core.source.folders()
        except SYNC_ERRORS as e:
            return jsonify(error=str(e)), 502
        finally:
            sync.LOCK.release()
        own = core.own_doc_ids()
        counts = {f: sum(1 for d in docs if (d.folder + "/").startswith(f.rstrip("/") + "/")) for f in folders}
        return jsonify(
            folders=[{"path": f, "documents": counts[f]} for f in folders],
            documents=[{"path": d.path, "folder": d.folder, "id": d.id, "own": d.id in own} for d in docs],
        )

    @flask.get("/api/library/pending")
    def get_pending():
        """What the next collection would read (documents only; nothing is downloaded)."""
        if not sync.LOCK.acquire(timeout=60):
            return jsonify(error="The tablet is busy syncing; try again in a moment"), 409
        try:
            docs = service.pending(core.source, core.repo, exclude=core.own_doc_ids())
        except SYNC_ERRORS as e:
            return jsonify(error=str(e)), 502
        finally:
            sync.LOCK.release()
        return jsonify(documents=[d.path for d, _ in docs])

    @flask.get("/api/sources/<doc_id>/<int:page>/preview.svg")
    def source_page(doc_id: str, page: int):
        """A source page with the action's line highlighted (?anchor=…)."""
        page_id = core.repo.page_id(doc_id, page)
        if not page_id:
            abort(404)
        line = core.repo.source_line(doc_id, request.args.get("anchor", "")) if request.args.get("anchor") else None
        svg = template.render_svg(core.source.page_strokes(doc_id, page_id), template.PageState(), cfg.template,
                                  templated=False, highlight=line["rows"] if line else None)
        return _svg(svg)

    @flask.get("/api/sources/<doc_id>/line/<anchor>.svg")
    def source_line(doc_id: str, anchor: str):
        """An image of one handwritten line."""
        line = core.repo.source_line(doc_id, anchor)
        if not line:
            abort(404)
        x0, y0, x1, y1 = line["bbox"]
        svg = template.render_svg(core.source.page_strokes(doc_id, line["page_id"]), template.PageState(),
                                  cfg.template, templated=False, crop=(x0 - 16, y0 - 12, x1 + 16, y1 + 12))
        return _svg(svg)

    return flask


def _svg(text: str) -> Response:
    return Response(text, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


def _page_id_from_cache(cfg: Config, doc_id: str, index: int) -> str | None:
    import json
    import zipfile

    from .notebook import page_order

    path = Path(cfg.paths.cache_dir) / f"{doc_id}.rmdoc"
    if not path.is_file():
        return None
    with zipfile.ZipFile(path) as z:
        content = next((n for n in z.namelist() if n.endswith(".content")), None)
        order = page_order(json.loads(z.read(content))) if content else []
    return order[index - 1] if 0 < index <= len(order) else None
