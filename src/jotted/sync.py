"""Pull (tablet -> store) and push (store -> tablet), shared by the CLI and the web server."""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.console import Console

from . import analysis, cloud, report, template
from .config import Config
from .store import PullSummary, Store

log = logging.getLogger("jotted")

# One sync at a time: the web server may get a second click while a pull is running.
LOCK = threading.Lock()


class SyncError(Exception):
    pass


@dataclass
class PullResult:
    doc: cloud.DocRef
    run: report.Run
    summary: PullSummary
    rmdoc: Path


@dataclass
class PushResult:
    pull: PullResult
    pdf: Path
    strikes: int
    footer: int
    moved: int
    uploaded: bool
    backup: Path | None = None
    notes: list[str] = field(default_factory=list)


def pull(cfg: Config, store: Store, console: Console | None = None) -> PullResult:
    """Download the notebook, analyse it, and fold it into the store."""
    doc = cloud.find_notebook(cfg)
    path = cloud.download(cfg, doc)
    run = analysis.analyse_notebook(path, cfg)
    report.write(run, cfg, console or Console(quiet=True))
    summary = store.apply_run(run, doc.modified)
    return PullResult(doc=doc, run=run, summary=summary, rmdoc=path)


def push(cfg: Config, store: Store, *, dry_run: bool = False, console: Console | None = None) -> PushResult:
    """Pull first (fresh ink positions, latest paper changes), then print the store into the PDF.

    Only template notebooks are written, and only their PDF: the handwriting is never touched.
    """
    result = pull(cfg, store, console)
    if result.run.notebook.get("file_type") != "pdf":
        raise SyncError(f"{result.doc.name!r} is not a template notebook; only PDFs made by "
                        "`jotted template upload` are written to")
    backups = cfg.paths.cache_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"{result.doc.id}-{datetime.now():%Y%m%d-%H%M%S}.rmdoc"
    shutil.copy2(result.rmdoc, backup)

    pages, snapshot_at = store.page_states(result.doc.id)
    pdf = result.run.out_dir / f"{cfg.notebook.name}.pdf"  # rmapi names the document after the file
    template.build(pdf, cfg.template, pages, page_count=result.run.page_count)
    out = PushResult(pull=result, pdf=pdf, backup=backup,
                     strikes=sum(len(p.strikes) for p in pages.values()),
                     footer=sum(len(p.footer) for p in pages.values()),
                     moved=sum(len(p.moved) for p in pages.values()), uploaded=False)
    if not dry_run:
        cloud.upload_pdf(cfg, pdf, content_only=True)
        out.uploaded = True
        store.record_push(result.doc.id, result.run.run_id,
                          {"strikes": out.strikes, "moved": out.moved, "footer": out.footer}, snapshot_at)
    return out
