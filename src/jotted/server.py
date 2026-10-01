"""Local web app (a driving adapter): the to-do list and settings.

Runs on this machine only (127.0.0.1 by default) and has no login. Reading from and
writing to the tablet run in the background (`app.Scheduler`); one at a time.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, fields
from importlib import resources

from flask import Flask, Response, abort, jsonify, request

from . import cloud
from .page import render_svg
from .app import SYNC_ERRORS, App, Scheduler
from .config import Config
from .core import service
from .core.model import Settings

log = logging.getLogger("jotted")


def create_app(cfg: Config, background: bool = True, app_: App | None = None) -> Flask:
    flask = Flask(__name__)

    @flask.after_request
    def security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        return resp

    core = app_ or App.build(cfg)
    scheduler = Scheduler(core, cfg.server.auto_push_delay_s)
    if background:
        scheduler.start()
    flask.config["scheduler"] = scheduler

    def changed():
        """After a web edit: update the To-do document a few seconds later (debounced)."""
        if cfg.server.auto_push:
            scheduler.push_soon()

    @flask.get("/")
    def index():
        html = resources.files("jotted").joinpath("web/index.html").read_text()
        return Response(html, mimetype="text/html")

    # ------------------------------------------------------------ the common to-do list

    def todo_state() -> dict:
        args = request.args
        items = core.repo.items(status=args.get("status") or None, owner=args.get("owner") or None,
                                folder=args.get("folder") or None)
        return {"items": items, "settings": asdict(core.repo.settings()), "background": scheduler.describe(),
                "todo": core.repo.todo_meta(), "sources": core.repo.source_docs()}

    @flask.get("/api/todo")
    def get_todo():
        return jsonify(todo_state())

    @flask.post("/api/items")
    def add_item():
        text = (request.get_json(silent=True) or {}).get("text")
        try:
            item_id = core.repo.add_item(text if isinstance(text, str) else "")
        except ValueError as e:
            return jsonify(error=str(e)), 400
        changed()
        return jsonify(item=core.repo.item(item_id), **todo_state()), 201

    @flask.patch("/api/items/<int:item_id>")
    def edit_item(item_id: int):
        body = request.get_json(silent=True) or {}
        try:
            core.repo.edit_action(item_id, text=body.get("text"), status=body.get("status"),
                                  dismissed=body.get("dismissed"))
        except KeyError:
            abort(404)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        changed()
        return jsonify(todo_state())

    @flask.post("/api/poll")
    def post_poll():
        """Check the tablet now (watched folders, the To-do document) in the background."""
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
            if not isinstance(s.from_now, list) or not all(isinstance(d, str) for d in s.from_now):
                raise ValueError("from_now must be a list of document IDs")
            s.from_now = sorted(set(s.from_now))
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
        if not cloud.LOCK.acquire(timeout=60):
            return jsonify(error="The tablet is busy syncing; try again in a moment"), 409
        try:
            docs = core.source.list_documents()
            folders = core.source.folders()
        except SYNC_ERRORS as e:
            return jsonify(error=str(e)), 502
        finally:
            cloud.LOCK.release()
        own = core.own_doc_ids()
        counts = {f: sum(1 for d in docs if (d.folder + "/").startswith(f.rstrip("/") + "/")) for f in folders}
        read = {d["id"] for d in core.repo.source_docs() if d["marker"]}  # fully collected at least once
        baseline = core.repo.baseline_pages()
        return jsonify(
            folders=[{"path": f, "documents": counts[f]} for f in folders],
            documents=[{"path": d.path, "folder": d.folder, "id": d.id, "own": d.id in own,
                        "read": d.id in read, "baseline_pages": baseline.get(d.id, 0)} for d in docs],
        )

    @flask.get("/api/library/pending")
    def get_pending():
        """What the next collection would read (documents only; nothing is downloaded)."""
        if not cloud.LOCK.acquire(timeout=60):
            return jsonify(error="The tablet is busy syncing; try again in a moment"), 409
        try:
            docs = service.pending(core.source, core.repo, exclude=core.own_doc_ids())
        except SYNC_ERRORS as e:
            return jsonify(error=str(e)), 502
        finally:
            cloud.LOCK.release()
        return jsonify(documents=[d.path for d, _ in docs])

    @flask.get("/api/sources/<doc_id>/<int:page>/preview.svg")
    def source_page(doc_id: str, page: int):
        """A source page with the action's line highlighted (?anchor=…)."""
        page_id = core.repo.page_id(doc_id, page)
        if not page_id:
            abort(404)
        line = core.repo.source_line(doc_id, request.args.get("anchor", "")) if request.args.get("anchor") else None
        svg = render_svg(core.source.page_strokes(doc_id, page_id), cfg.template.scale,
                         highlight=line["rows"] if line else None)
        return _svg(svg)

    @flask.get("/api/sources/<doc_id>/line/<anchor>.svg")
    def source_line(doc_id: str, anchor: str):
        """An image of one handwritten line."""
        line = core.repo.source_line(doc_id, anchor)
        if not line:
            abort(404)
        x0, y0, x1, y1 = line["bbox"]
        svg = render_svg(core.source.page_strokes(doc_id, line["page_id"]), cfg.template.scale,
                         crop=(x0 - 16, y0 - 12, x1 + 16, y1 + 12))
        return _svg(svg)

    return flask


def _svg(text: str) -> Response:
    return Response(text, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})

