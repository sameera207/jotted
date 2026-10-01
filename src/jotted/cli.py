"""Jotted command line: start, setup, collect, the To-do document, and debugging commands."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from . import cloud, keys, selfupdate
from .config import Config, ConfigError, load, resolve_path

log = logging.getLogger("jotted")
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        handlers=[RichHandler(console=Console(stderr=True), show_time=False, show_path=False)],
        force=True,
    )
    if level.upper() != "DEBUG":  # per-request lines from the HTTP clients are noise at INFO
        for name in ("httpx", "httpx2", "anthropic", "typesafe_sdk"):
            logging.getLogger(name).setLevel(logging.WARNING)
        # rmscene repeats "data not read" for every page from newer firmware; known and harmless.
        logging.getLogger("rmscene").setLevel(logging.ERROR)


# ---------------------------------------------------------------- commands


def cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    token = cfg.rmapi.token_file
    if token.exists():
        answer = input(f"A token already exists at {token}. Replace it? [y/N] ").strip().lower()
        if answer != "y":
            console.print("Keeping the existing token.")
            return 0
        token.unlink()
    console.print(
        "Get a one-time code at [bold]https://my.remarkable.com/device/desktop/connect[/bold] "
        "(sign in, then connect a desktop app)."
    )
    code = getpass.getpass("One-time code: ")
    cloud.register(cfg, code)
    console.print(f"[green]Registered.[/green] Token saved to {token} (keep it secret).")
    return 0


def cmd_config_check(cfg: Config, args: argparse.Namespace) -> int:
    console.print(f"[green]OK[/green] {cfg.source}")
    for section, values in cfg.as_dict().items():
        console.print(f"\n[bold]\\[{section}][/bold]")
        width = max(len(k) for k in values)
        for k, v in values.items():
            console.print(f"  {k:<{width}} = {json.dumps(v)}", highlight=False)
    rmapi = shutil.which(cfg.rmapi.binary)
    console.print()
    console.print(f"rmapi binary: {rmapi or '[red]not found[/red]'}")
    console.print(f"rmapi token:  {'present' if cfg.rmapi.token_file.exists() else '[yellow]missing (run jotted auth)[/yellow]'}")
    llm_key = bool(os.environ.get(cfg.llm.api_key_env))
    console.print(f"LLM ({cfg.llm.provider}, {cfg.llm.model}): {cfg.llm.api_key_env} "
                  f"{'set' if llm_key else '[yellow]not set (run jotted setup)[/yellow]'}")
    jev = bool(os.environ.get(cfg.jev.api_key_env))
    console.print(f"Jev plugin: {'on' if jev else 'off'} ({cfg.jev.api_key_env} {'set' if jev else 'not set'})")
    return 0


def _app(cfg: Config):
    from .app import App

    return App.build(cfg)


def cmd_library(cfg: Config, args: argparse.Namespace) -> int:
    from .core.model import DocInfo

    app = _app(cfg)
    with console.status("Listing the library…"):
        docs = app.source.list_documents()
        folders = app.source.folders()
    watched = app.repo.settings()
    table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("folder", "documents", "watched"):
        table.add_column(col)
    for f in ["/"] + folders:
        n = sum(1 for d in docs if d.folder == f)
        table.add_row(f, str(n), "yes" if watched.watches(DocInfo("remarkable", "", "x", f, "")) else "")
    console.print(table)
    return 0


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    app = _app(cfg)
    s = app.repo.settings()
    if args.action == "add":
        s.watch = sorted(set(s.watch) | {"/" + args.path.strip("/")})
    elif args.action == "remove":
        s.watch = [w for w in s.watch if w.strip("/") != args.path.strip("/")]
    else:  # from-now / read-all: the document at PATH, or every document in that folder now
        target = "/" + args.path.strip("/") if args.path.strip("/") else "/"
        with console.status("Listing the library…"):
            docs = [d for d in app.source.list_documents()
                    if target == "/" or d.path == target or d.path.startswith(target + "/")]
        if not docs:
            console.print(f"No documents at {target}")
            return 1
        ids = {d.id for d in docs}
        read = {d["id"] for d in app.repo.source_docs() if d["marker"]}
        if args.action == "from-now":
            late = [d.path for d in docs if d.id in read and d.id not in app.repo.baselined_docs()]
            s.from_now = sorted(set(s.from_now) | ids)
            if late:
                console.print(f"[dim]Already read in full, so nothing is skipped: {', '.join(late)}[/dim]")
        else:
            s.from_now = sorted(set(s.from_now) - ids)
        console.print(f"{'New writing only' if args.action == 'from-now' else 'Everything is read'} in "
                      f"{len(docs)} document(s) at {target}")
    app.repo.save_settings(s)
    console.print("Watching: " + (", ".join(s.watch) or "[dim]nothing[/dim]"))
    return 0


def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    from .core import service

    app = _app(cfg)
    if not app.repo.settings().watch:
        console.print("Nothing is watched. Add a folder with `jotted watch add /Meeting notes` or in the web app.")
        return 1
    if args.dry_run:
        with console.status("Checking what changed (downloads only, nothing is read)…"):
            todo = service.pending(app.source, app.repo, exclude=app.own_doc_ids(), fetch=True)
        if not todo:
            console.print("Nothing changed since the last collection.")
        for doc, pages in todo:
            console.print(f"[bold]{doc.path}[/bold]: {len(pages)} changed page(s) "
                          + (", ".join(str(p.index) for p in pages) if pages else ""))
        return 0
    with console.status("Collecting…") as status:
        summary = app.collect(progress=lambda m: status.update(m))
    console.print(summary.as_dict())
    for i in app.repo.items(status="open"):
        if i["origin"] != "web":
            src = i["source"]
            console.print(f"  [{'cyan' if i['owner'] == 'someone_else' else 'green'}]{i['owner']:<12}[/] "
                          f"{escape(i['text'])}  [dim]{escape(src['folder'])} › {escape(src['name'])} · p{src['page']}[/dim]")
    return 0 if not summary.errors else 1


def cmd_todo(cfg: Config, args: argparse.Namespace) -> int:
    app = _app(cfg)
    s = app.repo.settings()
    if not s.todo_enabled:
        console.print("The To-do document is off. Turn it on in the web app's settings.")
        return 1
    with console.status("Reading ticks and publishing the To-do document…"):
        result = app.sync_todo(force=args.force)
    console.print(result)
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    host, port = args.host or cfg.server.host, args.port or cfg.server.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(f"[yellow]Warning[/yellow]: listening on {host}; the app has no login, so anyone who can "
                      "reach this address can read and change your tasks.")
    return _serve(cfg, host, port, background=not args.no_background, dev=args.dev)


def _running_here(url: str) -> bool:
    """Whether Jotted already answers at `url` (another start, or `jotted serve`)."""
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/api/settings", timeout=2) as resp:  # noqa: S310 - local URL
            return "watch" in json.loads(resp.read())
    except (OSError, ValueError):
        return False


def _serve(cfg: Config, host: str, port: int, *, background: bool = True, dev: bool = False,
           open_path: str | None = None) -> int:
    """Serve the web app; with `open_path`, open the browser there once it is listening."""
    import socket
    import webbrowser

    from .server import create_app

    url = f"http://{host}:{port}"
    with socket.socket() as probe:
        busy = probe.connect_ex((host, port)) == 0
    if busy:
        if _running_here(url):
            console.print(f"Jotted is already running at [bold]{url}[/bold]")
            if os.environ.get(selfupdate.DONE_VAR):
                console.print("[yellow]That window still runs the old version:[/yellow] stop it with Ctrl+C, "
                              "then run [bold]jotted start[/bold] again.")
            if open_path is not None:
                webbrowser.open(url + open_path)
            return 0
        console.print(f"[red]Port {port} is in use[/red] by another program. Try `--port {port + 1}`.")
        return 1
    console.print(f"Jotted at [bold]{url}[/bold]  (store: {cfg.server.db})")
    app = create_app(cfg, background=background)
    if dev:
        app.run(host=host, port=port, debug=False, threaded=True)
        return 0
    from waitress import create_server

    # One process, many threads: the background scheduler must exist exactly once.
    server = create_server(app, host=host, port=port, threads=8, ident="jotted")
    if open_path is not None:
        webbrowser.open(url + open_path)
    console.print("[dim]Leave this window open while you use Jotted. Press Ctrl+C to stop.[/dim]")
    try:
        server.run()
    except KeyboardInterrupt:
        console.print("Stopped.")
    finally:
        server.close()
    return 0


def _onboard(redo: bool) -> Config | None:
    from . import onboarding

    try:
        return onboarding.run(onboarding.ConsoleUI(console), redo=redo)
    except (onboarding.SetupError, ConfigError) as e:
        console.print(f"\n[red]Setup stopped:[/red] {e}")
    except (EOFError, KeyboardInterrupt):
        console.print("\nSetup stopped. Run it again any time; finished steps are kept.")
    return None


def _rerun() -> None:
    """Start this command again in the copy just installed (once: DONE_VAR stops a second update)."""
    os.environ[selfupdate.DONE_VAR] = "1"
    os.execv(sys.argv[0], sys.argv)


def cmd_start(args: argparse.Namespace) -> int:
    """Update from GitHub, set up whatever is missing, then run the web app and open it in the browser."""
    if not args.no_update and selfupdate.check(console):
        _rerun()
    cfg = _onboard(redo=False)
    if cfg is None:
        return 1
    _setup_logging(cfg.logging.level)
    from .adapters.sqlite_repo import SqliteRepository

    first_time = not SqliteRepository(cfg.server.db).settings().watch  # nothing watched yet: start in Settings
    console.print()
    return _serve(cfg, cfg.server.host, args.port or cfg.server.port,
                  open_path=None if args.no_browser else ("/#settings" if first_time else "/"))


def cmd_update(args: argparse.Namespace) -> int:
    """Update to the latest version on GitHub now."""
    if selfupdate.check(console, force=True):
        console.print("Run [bold]jotted start[/bold] to use it (stop a running app first with Ctrl+C).")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Go through every setup step again (change a key, reconnect the tablet)."""
    cfg = _onboard(redo=True)
    if cfg is None:
        return 1
    console.print("\n[green]All set.[/green] Run [bold]jotted start[/bold] to open the app.")
    return 0


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jotted", description="Turn handwritten notes into a to-do list.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="register rmapi with a one-time code").set_defaults(func=cmd_auth)
    cfg_p = sub.add_parser("config", help="configuration commands")
    cfg_sub = cfg_p.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("check", help="validate config.toml and print resolved values").set_defaults(func=cmd_config_check)
    sub.add_parser("library", help="list the library's folders and which are watched").set_defaults(func=cmd_library)
    wa = sub.add_parser("watch", help="watch or stop watching a folder (also in the web app)",
                        description="from-now: skip what is already written in the documents at PATH (a "
                                    "folder means the documents in it now); read-all undoes it.")
    wa.add_argument("action", choices=["add", "remove", "from-now", "read-all"])
    wa.add_argument("path", help="folder or document path, e.g. '/Meeting notes'")
    wa.set_defaults(func=cmd_watch)
    co = sub.add_parser("collect", help="read what changed in watched folders and update the to-do list")
    co.add_argument("--dry-run", action="store_true", help="only show which documents and pages changed")
    co.set_defaults(func=cmd_collect)
    td = sub.add_parser("todo", help="read ticks from, and republish, the To-do document")
    td.add_argument("--force", action="store_true", help="republish even if nothing changed")
    td.set_defaults(func=cmd_todo)
    st = sub.add_parser("start", help="set up anything missing, then open the app (start here)")
    st.add_argument("--port", type=int, help="default: server.port")
    st.add_argument("--no-browser", action="store_true", help="don't open the browser")
    st.add_argument("--no-update", action="store_true", help="don't check GitHub for a newer version")
    st.set_defaults(func=cmd_start, no_config=True)
    sub.add_parser("update", help="update Jotted to the latest version on GitHub").set_defaults(
        func=cmd_update, no_config=True)
    su = sub.add_parser("setup", help="go through setup again: reconnect the tablet, change API keys")
    su.set_defaults(func=cmd_setup, no_config=True)
    sv = sub.add_parser("serve", help="run the local web app")
    sv.add_argument("--host", help="default: server.host")
    sv.add_argument("--port", type=int, help="default: server.port")
    sv.add_argument("--dev", action="store_true", help="use Flask's development server")
    sv.add_argument("--no-background", action="store_true",
                    help="don't check or write to the tablet in the background (debugging)")
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_config", False):  # start and setup make the config when there is none
        return args.func(args)
    try:
        cfg = load()
        keys.load_into_env(cfg)
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        console.print(f"[dim](config path: {resolve_path()}; set JOTTED_CONFIG to use another)[/dim]")
        return 2
    _setup_logging(cfg.logging.level)
    try:
        return args.func(cfg, args)
    except (cloud.CloudError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
