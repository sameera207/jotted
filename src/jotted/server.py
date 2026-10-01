"""Local web app (a driving adapter): the to-do list and settings, over `api.Jotted`.

Each route parses its input, calls one operation and returns its result as JSON; no
product logic lives here. Runs on this machine only (127.0.0.1 by default). Reading
from and writing to the source run in the background (`app.Scheduler`).

Other front ends (a desktop app) can use the same JSON API. Two guards keep other web
pages out:
- the Host header must name this machine, so a site can't reach the API through DNS
  rebinding;
- every request that changes something carries the install's token in `X-Jotted-Token`.
  The page gets it embedded when it loads; other local apps read it from
  `<secrets_dir>/server-token` (readable by this user only).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from importlib import resources
from pathlib import Path
from typing import Callable

from flask import Flask, Response, jsonify, request

from .api import ApiError, Jotted
from .app import App, Scheduler
from .config import Config

log = logging.getLogger("jotted")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
TOKEN_FILE = "server-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def server_token(cfg: Config) -> str:
    """This install's token for changing things through the API, made on first use."""
    path = Path(cfg.paths.secrets_dir) / TOKEN_FILE
    if path.is_file() and path.read_text().strip():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def _host_name(header: str) -> str:
    if header.startswith("["):  # [::1]:8765
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


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

    token = server_token(cfg)
    flask.config["token"] = token
    allowed_hosts = LOCAL_HOSTS | {cfg.server.host}
    open_to_network = cfg.server.host not in LOCAL_HOSTS  # chosen on purpose; `jotted serve` warns

    @flask.before_request
    def guard():
        if not open_to_network and _host_name(request.host or "") not in allowed_hosts:
            return jsonify(error="This app only answers on this computer's own address"), 403
        if request.method not in SAFE_METHODS and request.path.startswith("/api/"):
            sent = request.headers.get("X-Jotted-Token", "")
            if not hmac.compare_digest(sent, token):
                return jsonify(error="Missing or wrong X-Jotted-Token"), 403
        return None

    @flask.errorhandler(ApiError)
    def api_error(e: ApiError):
        return jsonify(error=str(e)), e.status

    core = app_ or App.build(cfg)
    scheduler = Scheduler(core, cfg.server.auto_push_delay_s)
    if background:
        scheduler.start()
    flask.config["scheduler"] = scheduler
    jotted = Jotted(core, scheduler=scheduler, on_change=scheduler.push_soon if cfg.server.auto_push else None)
    flask.config["jotted"] = jotted

    def route(method: str, rule: str, op: str) -> Callable:
        """Register a route that calls the operation `op` (checked against api.OPERATIONS)."""
        def register(fn: Callable) -> Callable:
            fn.operation = op
            return flask.route(rule, methods=[method], endpoint=fn.__name__)(fn)
        return register

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def filters() -> dict:
        return {k: request.args.get(k) or None for k in ("status", "owner", "folder")}

    @flask.get("/")
    def index():
        html = resources.files("jotted").joinpath("web/index.html").read_text()
        return Response(html.replace("{{JOTTED_TOKEN}}", token), mimetype="text/html",
                        headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ the to-do list

    @route("GET", "/api/todo", "items.list")
    def get_todo():
        return jsonify(jotted.overview(**filters()))

    @route("POST", "/api/items", "items.add")
    def add_item():
        item = jotted.add_item(body().get("text"))
        return jsonify(item=item, **jotted.overview(**filters())), 201

    @route("PATCH", "/api/items/<int:item_id>", "items.edit")
    def edit_item(item_id: int):
        b = body()
        jotted.edit_item(item_id, text=b.get("text"), status=b.get("status"), dismissed=b.get("dismissed"))
        return jsonify(jotted.overview(**filters()))

    @route("POST", "/api/poll", "check")
    def post_poll():
        return jsonify(jotted.check()), 202

    @route("GET", "/api/status", "status")
    def get_status():
        return jsonify(jotted.status())

    # ------------------------------------------------------------ settings and the library

    @route("GET", "/api/settings", "settings.get")
    def get_settings():
        return jsonify(jotted.settings())

    @route("PUT", "/api/settings", "settings.update")
    def put_settings():
        return jsonify(jotted.update_settings(body()))

    @route("GET", "/api/library", "library")
    def get_library():
        return jsonify(jotted.library())

    @route("GET", "/api/library/pending", "pending")
    def get_pending():
        return jsonify(documents=[d["path"] for d in jotted.pending()])

    # ------------------------------------------------------------ AI: the LLM and the Jev plugin

    @route("GET", "/api/ai", "ai.get")
    def get_ai():
        return jsonify(jotted.ai())

    @route("PUT", "/api/ai/<which>/key", "ai.set_key")
    def put_ai_key(which: str):
        return jsonify(jotted.set_key(which, body().get("key")))

    @route("DELETE", "/api/ai/<which>/key", "ai.remove_key")
    def delete_ai_key(which: str):
        return jsonify(jotted.remove_key(which))

    # ------------------------------------------------------------ where an item came from

    @route("GET", "/api/sources/<doc_id>/<int:page>/preview.svg", "page.image")
    def source_page(doc_id: str, page: int):
        return _svg(jotted.page_image(doc_id, page, request.args.get("anchor") or None))

    @route("GET", "/api/sources/<doc_id>/line/<anchor>.svg", "line.image")
    def source_line(doc_id: str, anchor: str):
        return _svg(jotted.line_image(doc_id, anchor))

    return flask


def _svg(text: str) -> Response:
    return Response(text, mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})
