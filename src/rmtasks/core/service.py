"""Core services: collect actions from sources, and keep the To-do document in step.

Reading is incremental at three levels:
1. documents whose change marker is unchanged are skipped (no download);
2. pages whose content hash is unchanged are skipped (no parsing);
3. on a changed page, only lines whose key changed are judged.
"""

from __future__ import annotations

import logging

from .model import CollectSummary, DocInfo
from .ports import ActionJudge, DocumentSource, Progress, Repository, TodoPublisher

log = logging.getLogger("rmtasks.core")


def _quiet(_: str) -> None:
    pass


def collect(source: DocumentSource, judge: ActionJudge, repo: Repository,
            exclude: set[str] | None = None, progress: Progress = _quiet) -> CollectSummary:
    """Read what changed in the watched documents and update the action items."""
    settings = repo.settings()
    summary = CollectSummary()
    exclude = exclude or set()
    if not settings.watch:
        return summary
    docs = [d for d in source.list_documents() if settings.watches(d) and d.id not in exclude]
    summary.docs_seen = len(docs)
    for doc in docs:
        if repo.doc_marker(source.name, doc.id) == doc.modified:
            continue
        summary.docs_changed += 1
        progress(f"Reading {doc.path}")
        try:
            _collect_doc(source, judge, repo, doc, settings.action_threshold, summary)
        except Exception as e:  # one bad document must not stop the rest
            log.exception("collecting %s failed", doc.path)
            summary.errors.append(f"{doc.path}: {e}")
    return summary


def _collect_doc(source: DocumentSource, judge: ActionJudge, repo: Repository, doc: DocInfo,
                 threshold: float, summary: CollectSummary) -> None:
    pages = source.pages(doc)
    for page in pages:
        if repo.page_hash(doc.id, page.id) == page.content_hash:
            summary.pages_skipped += 1
            continue
        lines = source.read_page(doc, page)
        known = repo.line_keys(doc.id, page.id)
        new = [ln for ln in lines if known.get(ln.anchor) != ln.key and ln.text and not ln.drawing]
        judgments = judge.judge(doc, page, lines, new) if new else {}
        added, updated, missing = repo.save_page(doc, page, lines, judgments, threshold)
        summary.pages_read += 1
        summary.lines_judged += len(new)
        summary.actions_new += added
        summary.actions_updated += updated
        summary.actions_missing += missing
    # Only now: a failure part-way leaves the old marker, so the next run retries this document.
    repo.save_doc(doc, len(pages))


def sync_todo(repo: Repository, publisher: TodoPublisher, force: bool = False) -> dict:
    """Read ticks from the To-do document, then republish it if anything changed.

    Ticks are read before publishing, so a tick made on paper is never lost to a
    republish. Slots are permanent: an item keeps the row it was first printed on.
    """
    ticked = 0
    read = publisher.read_ticks()
    if read is not None:
        slots, marker = read
        ticked = repo.apply_ticks(slots, marker)
    settings = repo.settings()
    entries = repo.assign_slots(repo.todo_entries(settings.tablet_include_others))
    overflow = [e for e in entries if e.slot is not None and e.slot >= publisher.capacity()]
    if overflow:
        log.warning("%d item(s) do not fit on the To-do document", len(overflow))
    printable = [e for e in entries if e.slot is not None and e.slot < publisher.capacity()]
    published = False
    if force or read is None or repo.todo_needs_publish(printable):
        publisher.publish(printable)
        repo.mark_todo_published(printable)
        published = True
    return {"ticked": ticked, "published": published, "items": len(printable), "overflow": len(overflow)}


def pending(source: DocumentSource, repo: Repository, exclude: set[str] | None = None,
            fetch: bool = False) -> list[tuple[DocInfo, list]]:
    """What a collect would read: changed documents and, with `fetch`, their changed pages.

    Nothing is transcribed, judged or stored. Without `fetch` nothing is downloaded either.
    """
    settings = repo.settings()
    exclude = exclude or set()
    out = []
    for doc in source.list_documents():
        if not settings.watches(doc) or doc.id in exclude or repo.doc_marker(source.name, doc.id) == doc.modified:
            continue
        pages = [p for p in source.pages(doc) if repo.page_hash(doc.id, p.id) != p.content_hash] if fetch else []
        out.append((doc, pages))
    return out
