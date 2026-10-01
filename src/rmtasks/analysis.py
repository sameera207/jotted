"""The analysis pipeline shared by the CLI and the web server:
strokes -> lines -> transcripts -> judgments -> kinds, per page."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console

from . import checkbox, classify, lines, notebook, recognise, report, strokes, template
from .aicache import AICache
from .config import Config

log = logging.getLogger("rmtasks")
console = Console(stderr=True)


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
        if len(ns) > 1:
            line.rows = [by_n[n].bbox for n in ns]
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


