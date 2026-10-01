"""rmtasks command line: auth, config check, scan, analyse, diff."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from . import analysis, cloud, notebook, report, sync, template
from .store import Store
from .config import Config, ConfigError, load, resolve_path

log = logging.getLogger("rmtasks")
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
    console.print(f"rmapi token:  {'present' if cfg.rmapi.token_file.exists() else '[yellow]missing (run rmtasks auth)[/yellow]'}")
    for section in (cfg.recognition, cfg.classification):
        if section.enabled:
            ok = bool(os.environ.get(section.api_key_env))
            console.print(f"{section.api_key_env}: {'set' if ok else '[yellow]not set[/yellow]'}")
    return 0


def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    with console.status("Pulling from the cloud…"):
        result = sync.pull(cfg, _store(cfg), console=console)
    s = result.summary
    console.print(f"Cached at {result.rmdoc}")
    console.print(f"Store: {s.new} new, {s.text_from_paper} text and {s.status_from_paper} status change(s) "
                  f"from paper, {s.missing} missing")
    return 0


def cmd_analyse(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        console.print(f"[red]No such file or folder:[/red] {path}")
        return 2
    run = analysis.analyse_notebook(path, cfg)
    report.write(run, cfg, console)
    return 0


def _load_report(p: str) -> dict:
    path = Path(p)
    if path.is_dir():
        path = path / "report.json"
    if not path.is_file():
        raise FileNotFoundError(f"no report.json at {path}")
    return json.loads(path.read_text())


def cmd_diff(cfg: Config, args: argparse.Namespace) -> int:
    """Every task in run A should still be a task in run B with the same anchor stroke."""
    a, b = _load_report(args.run_a), _load_report(args.run_b)
    tracked = report.KINDS_WITH_BOX
    b_pages = {p["id"]: p for p in b["pages"]}
    kept = changed = missing = 0
    rows = []
    for page in a["pages"]:
        other = b_pages.get(page["id"])
        b_lines = (other or {}).get("lines", [])
        by_anchor = {ln["anchor_id"]: ln for ln in b_lines}
        for ln in page["lines"]:
            if ln["kind"] not in tracked:
                continue
            where = f"p{page['index']} #{ln['n']}"
            label = ln.get("text") or ""
            match = by_anchor.get(ln["anchor_id"])
            if match is not None:
                note = [] if match["n"] == ln["n"] else [f"now #{match['n']}"]
                if match["kind"] != ln["kind"]:
                    note.append(f"{ln['kind']} → {match['kind']}")
                if match["kind"] in tracked:
                    kept += 1
                    rows.append(("kept", where, ln["anchor_id"], label, ", ".join(note)))
                else:
                    changed += 1
                    rows.append(("NOT A TASK", where, ln["anchor_id"], label, ", ".join(note)))
                continue
            # The anchor stroke may have been regrouped into another line, or erased.
            holder = next((o for o in b_lines if ln["anchor_id"] in o["stroke_ids"]), None)
            if holder is not None:
                changed += 1
                rows.append(("REGROUPED", where, ln["anchor_id"], label,
                             f"stroke now in #{holder['n']} (anchor {holder['anchor_id']})"))
            else:
                missing += 1
                rows.append(("MISSING", where, ln["anchor_id"], label,
                             "page gone" if other is None else "anchor stroke not in B"))

    a_anchors = {ln["anchor_id"] for p in a["pages"] for ln in p["lines"] if ln["kind"] in tracked}
    new = [
        f"p{p['index']} #{ln['n']} {ln.get('text') or ''}".strip()
        for p in b["pages"]
        for ln in p["lines"]
        if ln["kind"] in tracked and ln["anchor_id"] not in a_anchors
    ]

    console.print(f"A: {a['run']}   B: {b['run']}")
    table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("status", "task (in A)", "anchor", "text", ""):
        table.add_column(col)
    colours = {"kept": "green", "NOT A TASK": "red", "REGROUPED": "red", "MISSING": "yellow"}
    for status, where, anchor, label, note in rows:
        table.add_row(f"[{colours[status]}]{status}[/]", where, anchor, label, note)
    console.print(table)
    if new:
        console.print(f"\nNew in B ({len(new)}): " + "; ".join(new))
    console.print(f"\nkept {kept}   changed {changed}   missing {missing}   new {len(new)}")
    if changed or missing:
        console.print("[red]Anchor stability FAILED[/red]: some pre-existing tasks lost their anchor")
        return 1
    console.print("[green]Anchor stability OK[/green]: every pre-existing task kept its anchor")
    return 0


# ---------------------------------------------------------------- write path (PDF template)


def _pdf_path(cfg: Config, directory: Path) -> Path:
    # rmapi names the document after the file, so the file carries the notebook's name.
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{cfg.notebook.name}.pdf"


def cmd_template_build(cfg: Config, args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else _pdf_path(cfg, cfg.paths.output_dir / "template")
    pages = {1: template.PageState(calibration=True)} if args.calibration else {}
    template.build(out, cfg.template, pages)
    console.print(f"Template written to {out} ({cfg.template.pages} pages)")
    return 0


def cmd_template_upload(cfg: Config, args: argparse.Namespace) -> int:
    """Create a new template notebook in the cloud. Refuses if the name is taken."""
    try:
        doc = cloud.find_notebook(cfg)
    except cloud.CloudError as e:
        if "not found" not in str(e).lower():
            raise
    else:
        console.print(f"[red]A document named {doc.name!r} already exists[/red] ({doc.id}); pick a new notebook.name")
        return 1
    pdf = _pdf_path(cfg, cfg.paths.output_dir / "template")
    template.build(pdf, cfg.template, {1: template.PageState(calibration=True)} if args.calibration else {})
    with console.status(f"Uploading {pdf.name}…"):
        cloud.upload_pdf(cfg, pdf, content_only=False)
    console.print(f"[green]Created[/green] {cfg.notebook.name!r} in {cfg.notebook.folder} ({cfg.template.pages} pages). "
                  "Let the tablet sync, then open it.")
    return 0


def _store(cfg: Config) -> Store:
    return Store(cfg.server.db)


def cmd_push(cfg: Config, args: argparse.Namespace) -> int:
    with console.status("Pulling, then printing into the PDF…"):
        try:
            result = sync.push(cfg, _store(cfg), dry_run=args.dry_run, console=console)
        except sync.SyncError as e:
            console.print(f"[red]{e}[/red]")
            return 1
    console.print(f"PDF: {result.pdf}  ({result.pull.run.page_count} pages, {result.strikes} strike(s), "
                  f"{result.footer} footer task(s))")
    if not result.uploaded:
        console.print("[yellow]Dry run[/yellow]: nothing uploaded.")
        return 0
    console.print(f"[green]Pushed[/green]. Backup of the previous version: {result.backup}")
    return 0


def _app(cfg: Config):
    from .app import App

    return App.build(cfg, _store(cfg))


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
    app.repo.save_settings(s)
    console.print("Watching: " + (", ".join(s.watch) or "[dim]nothing[/dim]"))
    return 0


def cmd_collect(cfg: Config, args: argparse.Namespace) -> int:
    from .core import service

    app = _app(cfg)
    if not app.repo.settings().watch:
        console.print("Nothing is watched. Add a folder with `rmtasks watch add /Meeting notes` or in the web app.")
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
        if i["kind"] == "action":
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
    from .server import create_app

    host = args.host or cfg.server.host
    port = args.port or int(os.environ.get("PORT") or cfg.server.port)
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not os.environ.get("RMTASKS_PASSWORD"):
        console.print(f"[red]Refusing to listen on {host}[/red] without a password: anyone who can reach it could "
                      "read your notes and use your reMarkable token. Set RMTASKS_PASSWORD.")
        return 2
    console.print(f"rmtasks for [bold]{cfg.notebook.name}[/bold] at http://{host}:{port}  (store: {cfg.server.db})"
                  + ("  · login required" if os.environ.get("RMTASKS_PASSWORD") else ""))
    app = create_app(cfg)
    if args.dev:
        app.run(host=host, port=port, debug=False, threaded=True)
    else:
        from waitress import serve

        # One process, many threads: the background scheduler must exist exactly once.
        serve(app, host=host, port=port, threads=8, ident="rmtasks")
    return 0


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rmtasks", description="Find [ ] tasks in a handwritten reMarkable notebook.")
    p.add_argument("--notebook", metavar="NAME", help="use this notebook instead of notebook.name (e.g. Tasks-sandbox)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="register rmapi with a one-time code").set_defaults(func=cmd_auth)
    cfg_p = sub.add_parser("config", help="configuration commands")
    cfg_sub = cfg_p.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("check", help="validate config.toml and print resolved values").set_defaults(func=cmd_config_check)
    sub.add_parser("scan", help="download the notebook from the cloud and analyse it").set_defaults(func=cmd_scan)
    an = sub.add_parser("analyse", aliases=["analyze"], help="analyse a cached .rmdoc or unpacked folder")
    an.add_argument("path")
    an.set_defaults(func=cmd_analyse)
    tp = sub.add_parser("template", help="the PDF template notebook (write path)")
    tp_sub = tp.add_subparsers(dest="template_command", required=True)
    tb = tp_sub.add_parser("build", help="write the blank template PDF locally")
    tb.add_argument("--out", help="output path (default: out/template/<notebook.name>.pdf)")
    tb.add_argument("--calibration", action="store_true", help="add trace-over marks on page 1")
    tb.set_defaults(func=cmd_template_build)
    tu = tp_sub.add_parser("upload", help="create a new template notebook named notebook.name in the cloud")
    tu.add_argument("--calibration", action="store_true", help="add trace-over marks on page 1")
    tu.set_defaults(func=cmd_template_upload)
    pu = sub.add_parser("push", help="print web changes into the template notebook's PDF")
    pu.add_argument("--dry-run", action="store_true", help="build the PDF locally; do not upload")
    pu.set_defaults(func=cmd_push)
    sub.add_parser("library", help="list the library's folders and which are watched").set_defaults(func=cmd_library)
    wa = sub.add_parser("watch", help="watch or stop watching a folder (also in the web app)")
    wa.add_argument("action", choices=["add", "remove"])
    wa.add_argument("path", help="folder or document path, e.g. '/Meeting notes'")
    wa.set_defaults(func=cmd_watch)
    co = sub.add_parser("collect", help="read what changed in watched folders and update the to-do list")
    co.add_argument("--dry-run", action="store_true", help="only show which documents and pages changed")
    co.set_defaults(func=cmd_collect)
    td = sub.add_parser("todo", help="read ticks from, and republish, the To-do document")
    td.add_argument("--force", action="store_true", help="republish even if nothing changed")
    td.set_defaults(func=cmd_todo)
    sv = sub.add_parser("serve", help="run the local web app")
    sv.add_argument("--host", help="default: server.host")
    sv.add_argument("--port", type=int, help="default: $PORT, else server.port")
    sv.add_argument("--dev", action="store_true", help="use Flask's development server")
    sv.set_defaults(func=cmd_serve)
    df = sub.add_parser("diff", help="compare checkbox stroke IDs between two runs")
    df.add_argument("run_a")
    df.add_argument("run_b")
    df.set_defaults(func=cmd_diff)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load()
    except ConfigError as e:
        console.print(f"[red]Config error:[/red] {e}")
        console.print(f"[dim](config path: {resolve_path()}; set RMTASKS_CONFIG to use another)[/dim]")
        return 2
    if args.notebook:
        import dataclasses

        cfg = dataclasses.replace(cfg, notebook=dataclasses.replace(cfg.notebook, name=args.notebook))
    _setup_logging(cfg.logging.level)
    try:
        return args.func(cfg, args)
    except (cloud.CloudError, notebook.NotebookError, sync.SyncError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
