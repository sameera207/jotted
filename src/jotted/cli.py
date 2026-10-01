"""Jotted command line: the whole product from a terminal, over `api.Jotted`.

Every operation the web app offers is a command here too (a test keeps it so), and every
command takes --json to print the operation's result for scripts and other front ends.
Commands only parse arguments and show results; the work happens in `jotted.api`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from . import keys, plugins, selfupdate
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


# ---------------------------------------------------------------- output

def uses(*ops: str):
    """Mark a command with the api operations it offers (checked by the parity test)."""
    def mark(fn):
        fn.operations = ops
        return fn
    return mark


def _jotted(cfg: Config):
    from .api import Jotted

    return Jotted.open(cfg)


def _emit(args: argparse.Namespace, data, render) -> int:
    """--json prints the operation's result as it is; otherwise `render` shows it to a person."""
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        render()
    return 0


def _label(item: dict) -> str:
    src = item["source"]
    if item["origin"] == "web":
        return "added here"
    if item["written"]:
        return f"{src['name']} · p{src['page']}"
    return f"{(src['folder'] or '/').strip('/') or 'Library'} › {src['name']} · p{src['page']}"


def _items_table(items: list[dict]) -> None:
    if not items:
        console.print("[dim]Nothing here.[/dim]")
        return
    table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("id", "", "owner", "item", "from"):
        table.add_column(col)
    colours = {"me": "green", "someone_else": "cyan", "unclear": "yellow"}
    for i in items:
        table.add_row(str(i["id"]), "✓" if i["status"] == "done" else "·",
                      f"[{colours.get(i['owner'], 'white')}]{i['owner']}[/]", escape(i["text"]),
                      f"[dim]{escape(_label(i))}[/dim]")
    console.print(table)


# ---------------------------------------------------------------- commands


def cmd_config_check(cfg: Config, args: argparse.Namespace) -> int:
    console.print(f"[green]OK[/green] {cfg.source}")
    for section, values in cfg.as_dict().items():
        console.print(f"\n[bold]\\[{section}][/bold]")
        width = max(len(k) for k in values)
        for k, v in values.items():
            console.print(f"  {k:<{width}} = {json.dumps(v)}", highlight=False)
    console.print()
    jotted = _jotted(cfg)
    src = jotted.source()
    console.print(f"Source: {src['label']} ({src['detail']})")
    ai = jotted.ai()
    console.print(f"LLM: {ai['llm']['label']} {ai['llm']['model']}, key "
                  + ("set" if ai["llm"]["key"]["set"] else "[yellow]not set (run jotted setup)[/yellow]"))
    console.print(f"Jev plugin: {'on' if ai['jev']['enabled'] else 'off'}")
    return 0


@uses("items.list", "items.add", "items.edit")
def cmd_items(cfg: Config, args: argparse.Namespace) -> int:
    jotted = _jotted(cfg)
    action = args.items_command or "list"
    if action == "list":
        status = None if args.status == "all" else args.status
        items = jotted.items(status=status, owner=args.owner, folder=args.folder)
        return _emit(args, items, lambda: _items_table(items))
    if action == "add":
        item = jotted.add_item(" ".join(args.text))
        return _emit(args, item, lambda: console.print(
            f"Added #{item['id']}: {escape(item['text'])}. [dim]It reaches the To-do document on the next "
            "check (`jotted todo` now).[/dim]"))
    changes = {"edit": {"text": " ".join(getattr(args, "text", []) or [])}, "done": {"status": "done"},
               "reopen": {"status": "open"}, "dismiss": {"dismissed": True}}[action]
    item = jotted.edit_item(args.id, **changes)
    return _emit(args, item, lambda: console.print(
        f"#{args.id} removed from the list." if action == "dismiss" else
        f"#{item['id']} {'✓ ' if item['status'] == 'done' else ''}{escape(item['text'])}"))


def _value(text: str):
    """A setting's value as typed: JSON when it parses (true, 0.8, ["/A"]), else the text itself."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@uses("settings.get", "settings.update")
def cmd_settings(cfg: Config, args: argparse.Namespace) -> int:
    jotted = _jotted(cfg)
    if args.settings_command == "set":
        settings = jotted.update_settings({args.key: _value(args.value)})
    else:
        settings = jotted.settings()

    def show():
        width = max(len(k) for k in settings)
        for k, v in settings.items():
            console.print(f"{k:<{width}}  {json.dumps(v)}", highlight=False)
    return _emit(args, settings, show)


@uses("watch.add", "watch.remove", "watch.from_now")
def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    jotted = _jotted(cfg)
    if args.action in ("add", "remove"):
        settings = (jotted.watch if args.action == "add" else jotted.unwatch)(args.path)
        return _emit(args, settings, lambda: console.print(
            "Watching: " + (", ".join(settings["watch"]) or "[dim]nothing[/dim]")))
    with console.status("Listing the library…"):
        result = jotted.from_now(args.path, on=args.action == "from-now")

    def show():
        if result["already_read"]:
            console.print(f"[dim]Already read in full, so nothing is skipped: {', '.join(result['already_read'])}[/dim]")
        console.print(f"{'New writing only' if args.action == 'from-now' else 'Everything is read'} in "
                      f"{len(result['documents'])} document(s)")
    return _emit(args, result, show)


@uses("library")
def cmd_library(cfg: Config, args: argparse.Namespace) -> int:
    with console.status("Listing the library…"):
        lib = _jotted(cfg).library()

    def show():
        table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
        for col in ("folder", "documents", "watched"):
            table.add_column(col)
        top = [d for d in lib["documents"] if d["folder"] == "/"]
        table.add_row("/", str(len(top)), "")
        for f in lib["folders"]:
            table.add_row(f["path"], str(f["documents"]), "yes" if f["watched"] else "")
        console.print(table)
    return _emit(args, lib, show)


@uses("collect", "pending")
def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    jotted = _jotted(cfg)
    if args.dry_run:
        with console.status("Checking what changed (downloads only, nothing is read)…"):
            pending = jotted.pending(fetch=True)

        def show():
            if not pending:
                console.print("Nothing changed since the last collection.")
            for d in pending:
                console.print(f"[bold]{escape(d['path'])}[/bold]: {len(d['pages'])} changed page(s) "
                              + ", ".join(str(p) for p in d["pages"]))
        return _emit(args, pending, show)
    with console.status("Collecting…") as status:
        summary = jotted.collect(progress=lambda m: status.update(m))

    def show():
        console.print(f"{summary['docs_changed']} changed document(s), {summary['pages_read']} page(s) read, "
                      f"{summary['lines_judged']} new line(s) judged, {summary['actions_new']} new action(s).")
        for e in summary["errors"]:
            console.print(f"[red]{escape(e)}[/red]")
    _emit(args, summary, show)
    return 0 if not summary["errors"] else 1


@uses("todo.sync")
def cmd_todo(cfg: Config, args: argparse.Namespace) -> int:
    with console.status("Reading ticks and publishing the To-do document…"):
        result = _jotted(cfg).sync_todo(force=args.force)
    return _emit(args, result, lambda: console.print(
        f"{result.get('ticked', 0)} ticked, {result.get('written', 0)} written on paper; "
        + ("published" if result.get("published") else "unchanged")
        + (f"; [yellow]{result['overflow']} item(s) didn't fit[/yellow]" if result.get("overflow") else "")))


@uses("check")
def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    with console.status("Checking…"):
        result = _jotted(cfg).check()
    return _emit(args, result, lambda: console.print(
        "Nothing to check: no folders watched and the To-do document is off." if not result else
        "; ".join(f"{k}: {v}" for k, v in result.items())))


@uses("status", "source")
def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    status = _jotted(cfg).status()

    def show():
        src = status["source"]
        console.print(f"Source:   {src['label']} ({src['detail']})")
        console.print(f"Judge:    {'Jev' if status['judge'] == 'jev' else 'the LLM'}")
        console.print(f"Watching: {', '.join(status['watch']) or '[dim]nothing[/dim]'}")
        console.print(f"Read:     {status['documents_read']} document(s), last at {status['last_collected_at'] or 'never'}")
        console.print(f"Items:    {status['items']['open']} open, {status['items']['done']} done")
        t = status["todo"]
        console.print(f"To-do:    {'on' if t['enabled'] else 'off'}"
                      + (f", “{t['name']}” in {t['folder']}, published {t['published_at'] or 'never'}" if t["enabled"] else ""))
    return _emit(args, status, show)


@uses("ai.get", "ai.set_key", "ai.remove_key")
def cmd_ai(cfg: Config, args: argparse.Namespace) -> int:
    jotted = _jotted(cfg)
    if args.ai_command == "key":
        value = sys.stdin.readline() if args.stdin else getpass.getpass(f"Paste the {args.which} key, it stays hidden: ")
        with console.status("Checking the key…"):
            ai = jotted.set_key(args.which, value)
    elif args.ai_command == "remove":
        ai = jotted.remove_key(args.which)
    else:
        ai = jotted.ai()

    def show():
        m, j = ai["llm"], ai["jev"]
        key = m["key"]
        console.print(f"LLM:  {m['family']} by {m['label']} ({m['model']}), adapter “{m['provider']}”; key "
                      + (f"{key['hint']} ({key['source']})" if key["set"] else "[yellow]not set[/yellow]"))
        console.print(f"Jev:  {'on (' + j['model'] + '), judging actions and owners' if j['enabled'] else 'off'}")
        console.print(f"Judging actions: {'Jev' if ai['judge'] == 'jev' else m['family']}")
    return _emit(args, ai, show)


@uses("page.image", "line.image")
def cmd_image(cfg: Config, args: argparse.Namespace) -> int:
    jotted = _jotted(cfg)
    svg = (jotted.page_image(args.doc_id, args.page, args.anchor) if args.image_command == "page"
           else jotted.line_image(args.doc_id, args.anchor))
    if args.out:
        Path(args.out).write_text(svg)
        return _emit(args, {"path": args.out}, lambda: console.print(f"Written to {args.out}"))
    sys.stdout.write(svg)
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
    p.add_argument("--json", action="store_true", help="print results as JSON (for scripts and other apps)")
    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, func, help: str, **kw) -> argparse.ArgumentParser:
        c = sub.add_parser(name, help=help, **kw)
        c.set_defaults(func=func)
        return c

    # setting up and running
    st = command("start", cmd_start, "set up anything missing, then open the app (start here)")
    st.add_argument("--port", type=int, help="default: server.port")
    st.add_argument("--no-browser", action="store_true", help="don't open the browser")
    st.add_argument("--no-update", action="store_true", help="don't check GitHub for a newer version")
    st.set_defaults(no_config=True)
    command("setup", cmd_setup, "go through setup again: reconnect your device, change API keys").set_defaults(
        no_config=True)
    command("update", cmd_update, "update Jotted to the latest version on GitHub").set_defaults(no_config=True)
    sv = command("serve", cmd_serve, "run the local web app (and check your device in the background)")
    sv.add_argument("--host", help="default: server.host")
    sv.add_argument("--port", type=int, help="default: server.port")
    sv.add_argument("--dev", action="store_true", help="use Flask's development server")
    sv.add_argument("--no-background", action="store_true", help="don't check or write to the device in the background")
    cfg_p = sub.add_parser("config", help="configuration commands")
    cfg_sub = cfg_p.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("check", help="validate config.toml and print resolved values").set_defaults(func=cmd_config_check)

    # the to-do list
    it = command("items", cmd_items, "the to-do list: list, add, edit, tick, dismiss")
    it_sub = it.add_subparsers(dest="items_command")
    for c in (it, it_sub.add_parser("list", help="list items (the default)")):
        c.add_argument("--status", choices=["open", "done", "all"], default="open")
        c.add_argument("--owner", choices=["mine", "others"])
        c.add_argument("--folder", help="only items from documents in this folder")
    it_sub.add_parser("add", help="add an item of yours").add_argument("text", nargs="+")
    ed = it_sub.add_parser("edit", help="change an item's text")
    ed.add_argument("id", type=int)
    ed.add_argument("text", nargs="+")
    for name, help in (("done", "mark an item done"), ("reopen", "mark an item open again"),
                       ("dismiss", "not an action: take it off the list (deletes one added here)")):
        it_sub.add_parser(name, help=help).add_argument("id", type=int)

    # what is read
    se = command("settings", cmd_settings, "show or change settings (also in the web app)")
    se_sub = se.add_subparsers(dest="settings_command")
    ss = se_sub.add_parser("set", help="set one: e.g. todo_enabled true, action_threshold 0.8")
    ss.add_argument("key")
    ss.add_argument("value", help="JSON (true, 0.8, [\"/A\"]) or plain text")
    wa = command("watch", cmd_watch, "watch or stop watching a folder (also in the web app)",
                 description="from-now: skip what is already written in the documents at PATH (a "
                             "folder means the documents in it now); read-all undoes it.")
    wa.add_argument("action", choices=["add", "remove", "from-now", "read-all"])
    wa.add_argument("path", help="folder or document path, e.g. '/Meeting notes'")
    command("library", cmd_library, "list your device's folders and which are watched")

    # doing the work now (`jotted serve` does it in the background)
    co = command("collect", cmd_collect, "read what changed in watched folders and update the to-do list")
    co.add_argument("--dry-run", action="store_true", help="only show which documents and pages changed")
    td = command("todo", cmd_todo, "read ticks from, and republish, the To-do document")
    td.add_argument("--force", action="store_true", help="republish even if nothing changed")
    command("check", cmd_check, "collect and update the To-do document now")
    command("status", cmd_status, "what Jotted reads, judges and publishes, and when it last did")

    # AI
    ai = command("ai", cmd_ai, "the language model and the Jev plugin: status and keys")
    ai_sub = ai.add_subparsers(dest="ai_command")
    k = ai_sub.add_parser("key", help="check and save a key (adding Jev's turns the plugin on)")
    k.add_argument("which", choices=["llm", "jev"])
    k.add_argument("--stdin", action="store_true", help="read the key from standard input instead of asking")
    ai_sub.add_parser("remove", help="turn the Jev plugin off").add_argument("which", choices=["jev"])

    # where an item came from
    im = command("image", cmd_image, "draw a source page or line as SVG")
    im_sub = im.add_subparsers(dest="image_command", required=True)
    pg = im_sub.add_parser("page", help="a page, with a line highlighted")
    pg.add_argument("doc_id")
    pg.add_argument("page", type=int)
    pg.add_argument("--anchor", help="the line to highlight")
    ln = im_sub.add_parser("line", help="one handwritten line")
    ln.add_argument("doc_id")
    ln.add_argument("anchor")
    for c in (pg, ln):
        c.add_argument("-o", "--out", help="write to this file instead of standard output")

    # the source plugins' own commands (reMarkable: auth)
    for name in plugins.available():
        try:
            plugins.plugin_class(name).cli(sub)
        except Exception as e:  # a broken plugin must not take the CLI down
            print(f"warning: source plugin {name!r} failed to load: {e}", file=sys.stderr)
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
    from .api import ApiError
    from .app import SYNC_ERRORS

    try:
        return args.func(cfg, args)
    except (ApiError, *SYNC_ERRORS, FileNotFoundError) as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(e), "status": getattr(e, "status", 500)}))
        else:
            console.print(f"[red]Error:[/red] {escape(str(e))}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
