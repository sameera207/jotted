"""rmtasks command line: auth, config check, scan, analyse, diff."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import checkbox, classify, cloud, lines, notebook, recognise, report, strokes, template
from .aicache import AICache
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


# ---------------------------------------------------------------- analysis


def geometric_kind(cb: checkbox.Checkbox | None, cfg: Config) -> str:
    if cb is None or cb.confidence < cfg.checkbox.min_confidence:
        return "note"
    return "empty_checkbox" if cfg.checkbox.require_text and not cb.text else "task"


def decide(r: report.LineResult, cfg: Config) -> str:
    """Kind policy: code owns the rules, the models supply readings and judgments."""
    t, j = r.transcript, r.judgment
    if t is None:  # recognition off or failed: geometry only
        return r.geometric_kind
    if t.drawing:
        return "drawing"
    has_box = t.checkbox != "none"
    if has_box and not t.text:
        return "empty_checkbox"
    if j is not None:
        if j.p_todo >= cfg.classification.todo_threshold or (has_box and cfg.classification.checkbox_is_task):
            return "done" if t.checkbox == "checked" else "task"
        return "heading" if j.choice == "heading" else "note"
    if has_box:  # classification off: the checkbox mark decides
        return "done" if t.checkbox == "checked" else "task"
    return "note"


def merge_continuations(
    page_lines: list[lines.Line], transcripts: dict[int, recognise.Transcript], pairs: list[tuple[int, int]]
) -> tuple[list[lines.Line], dict[int, recognise.Transcript], dict[int, list[int]]]:
    """Fold each confirmed continuation into the line it continues (chains allowed).
    The merged line keeps the first line's number, so its anchor is the first line's."""
    root: dict[int, int] = {}
    for above, below in pairs:
        root[below] = root.get(above, above)
    by_n = {ln.n: ln for ln in page_lines}
    parts: dict[int, list[int]] = {}
    for ln in sorted(page_lines, key=lambda x: x.n):
        parts.setdefault(root.get(ln.n, ln.n), []).append(ln.n)

    merged_lines, merged_ts = [], {}
    for first, ns in parts.items():
        line = lines.Line(strokes=[s for n in ns for s in by_n[n].strokes], n=first)
        line.strokes.sort(key=lambda s: s.x0)
        merged_lines.append(line)
        ts = [transcripts[n] for n in ns if n in transcripts]
        if ts:
            merged_ts[first] = recognise.Transcript(
                n=first, checkbox=ts[0].checkbox, text=" ".join(t.text for t in ts if t.text),
                drawing=ts[0].drawing, cached=all(t.cached for t in ts),
            )
    return merged_lines, merged_ts, {first: ns for first, ns in parts.items() if len(ns) > 1}


def analyse_page(page: notebook.Page, cfg: Config, cache: AICache, zoned: bool = False) -> report.PageResult:
    """zoned: a template page. Header lines give the date, footer lines (printed by us) are
    skipped, and only body lines can be tasks."""
    if page.rm_path is None:
        return report.PageResult(index=page.index, id=page.id, strokes=[])
    page_strokes = strokes.load_strokes(page.rm_path, cfg.strokes)
    page_lines, unassigned = lines.cluster(page_strokes, cfg.lines)
    result = report.PageResult(index=page.index, id=page.id, strokes=page_strokes, unassigned=unassigned)

    zone_of: dict[int, str] = {}
    if zoned:
        z = template.zones(cfg.template)
        zone_of = {ln.n: z.of((ln.bbox[1] + ln.bbox[3]) / 2) for ln in page_lines}
    header = [ln for ln in page_lines if zone_of.get(ln.n) == "header"]
    footer = [ln for ln in page_lines if zone_of.get(ln.n) == "footer"]
    page_lines = [ln for ln in page_lines if zone_of.get(ln.n, "body") == "body"]
    body_lines = list(page_lines)
    header_ts: dict[int, recognise.Transcript] = {}

    transcripts: dict[int, recognise.Transcript] = {}
    judgments: dict[int, classify.Judgment] = {}
    merged: dict[int, list[int]] = {}
    if cfg.recognition.enabled and page_lines:
        try:
            with console.status(f"Reading page {page.index}…"):
                transcripts = recognise.transcribe(page_lines + header, cfg.recognition, cache)
                header_ts = {ln.n: transcripts.pop(ln.n) for ln in header if ln.n in transcripts}
                result.date = " ".join(t.text for t in header_ts.values() if t.text) or None
                if cfg.classification.enabled:
                    pairs = sorted(
                        classify.adjacent_drawings(page_lines, transcripts, cfg.classification)
                        + classify.continuations(page_lines, transcripts, cfg.classification, cache)
                    )
                    page_lines, transcripts, merged = merge_continuations(page_lines, transcripts, pairs)
                    judgments = classify.classify(transcripts, cfg.classification, cache)
        except (recognise.RecognitionError, classify.ClassificationError) as e:
            log.error("page %d: %s; falling back to geometric detection", page.index, e)
            transcripts, judgments, merged = {}, {}, {}
            page_lines = body_lines

    for line in sorted(page_lines, key=lambda ln: ln.n):
        cb = checkbox.candidate(line, cfg.checkbox)
        r = report.LineResult(
            line=line, kind="note", checkbox=cb, geometric_kind=geometric_kind(cb, cfg),
            transcript=transcripts.get(line.n), judgment=judgments.get(line.n), merged=merged.get(line.n, [line.n]),
        )
        r.kind = decide(r, cfg)
        result.lines.append(r)
    for line in header + footer:
        zone = zone_of[line.n]
        result.lines.append(report.LineResult(line=line, kind=zone, checkbox=None, zone=zone, merged=[line.n],
                                              transcript=header_ts.get(line.n)))
    result.lines.sort(key=lambda r: r.line.n)
    return result


def analyse_notebook(path: Path, cfg: Config) -> report.Run:
    nb = notebook.open_notebook(path, cfg.paths.cache_dir / "unpacked")
    selected = notebook.select_pages(nb.pages, cfg.notebook.pages)
    log.info("%s: %d page(s), analysing %d", nb.name, len(nb.pages), len(selected))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = cfg.paths.output_dir / run_id
    suffix = 1
    while out_dir.exists():  # two runs in the same second
        suffix += 1
        out_dir = cfg.paths.output_dir / f"{run_id}-{suffix}"
    run = report.Run(run_id=out_dir.name, out_dir=out_dir, notebook=nb.meta(), source=str(path))
    cache = AICache(cfg.paths.cache_dir / "ai")
    zoned = nb.file_type == "pdf"
    for page in selected:
        run.pages.append(analyse_page(page, cfg, cache, zoned=zoned))
    run.page_count = len(nb.pages)
    return run


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
    with console.status("Finding notebook…"):
        doc = cloud.find_notebook(cfg)
    console.print(f"Found [bold]{doc.name}[/bold] ({doc.id}, version {doc.version})")
    with console.status("Downloading…"):
        path = cloud.download(cfg, doc)
    console.print(f"Cached at {path}")
    run = analyse_notebook(path, cfg)
    report.write(run, cfg, console)
    return 0


def cmd_analyse(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        console.print(f"[red]No such file or folder:[/red] {path}")
        return 2
    run = analyse_notebook(path, cfg)
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


def load_webstate(path: Path) -> dict:
    """Stand-in for the task database:
    {"calibration": bool, "done": [anchor, ...], "edit": {anchor: new text}, "add": {"<page>": [text, ...]}}"""
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def page_states(run: report.Run, state: dict) -> tuple[dict[int, template.PageState], list[str]]:
    """What to print on each page, from the latest scan plus the web state.
    Returns the page states and the anchors that were not found on any page."""
    done, edits = set(state.get("done", [])), dict(state.get("edit", {}))
    pages: dict[int, template.PageState] = {}
    seen: set[str] = set()
    for page in run.pages:
        ps = template.PageState(footer=list(state.get("add", {}).get(str(page.index), [])))
        for r in page.lines:
            anchor = r.line.anchor_id
            if r.zone != "body" or (anchor not in done and anchor not in edits):
                continue
            seen.add(anchor)
            x0, y0, x1, y1 = r.line.bbox
            if r.box != "none" and r.checkbox is not None:
                x0 = r.checkbox.bbox[2]  # start after the checkbox
            ps.strikes.append((x0, x1, (y0 + y1) / 2))
            if anchor in edits:
                ps.footer.append(edits[anchor])
        pages[page.index] = ps
    if state.get("calibration"):
        pages.setdefault(1, template.PageState()).calibration = True
    return pages, sorted((done | set(edits)) - seen)


def cmd_push(cfg: Config, args: argparse.Namespace) -> int:
    with console.status("Finding notebook…"):
        doc = cloud.find_notebook(cfg)
    with console.status("Downloading…"):
        path = cloud.download(cfg, doc)
    backups = cfg.paths.cache_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"{doc.id}-{datetime.now():%Y%m%d-%H%M%S}.rmdoc"
    shutil.copy2(path, backup)

    run = analyse_notebook(path, cfg)
    if run.notebook.get("file_type") != "pdf":
        console.print(f"[red]{doc.name!r} is not a template notebook[/red]; push only writes to PDFs made by "
                      "`rmtasks template upload`. Your handwriting notebooks are never written to.")
        return 1
    report.write(run, cfg, console)

    state = load_webstate(cfg.template.webstate)
    pages, missing = page_states(run, state)
    for anchor in missing:
        log.warning("anchor %s from %s is not on any analysed page; skipped", anchor, cfg.template.webstate.name)
    pdf = _pdf_path(cfg, run.out_dir)
    template.build(pdf, cfg.template, pages, page_count=run.page_count)
    strikes = sum(len(p.strikes) for p in pages.values())
    footer = sum(len(p.footer) for p in pages.values())
    console.print(f"PDF: {pdf}  ({run.page_count} pages, {strikes} strike(s), {footer} footer task(s))")
    if args.dry_run:
        console.print("[yellow]Dry run[/yellow]: nothing uploaded.")
        return 0
    with console.status("Replacing the PDF (handwriting kept)…"):
        cloud.upload_pdf(cfg, pdf, content_only=True)
    console.print(f"[green]Pushed[/green]. Backup of the previous version: {backup}")
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
    except (cloud.CloudError, notebook.NotebookError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
