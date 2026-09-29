"""Unpack a downloaded notebook and list its pages in order.

Accepts any zip (whatever rmapi names it) or an already unpacked folder.
The .content and .rm files are located by extension, not fixed names.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class NotebookError(Exception):
    pass


@dataclass(frozen=True)
class Page:
    index: int  # 1-based position in the notebook
    id: str
    rm_path: Path | None  # None for a page with nothing drawn on it


@dataclass
class Notebook:
    id: str
    name: str
    version: int | None
    root: Path
    file_type: str = "notebook"  # "pdf" for a template notebook
    pages: list[Page] = field(default_factory=list)

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version, "file_type": self.file_type}


def open_notebook(path: Path, unpack_dir: Path) -> Notebook:
    path = path.expanduser().resolve()
    if path.is_dir():
        root = path
    elif zipfile.is_zipfile(path):
        root = unpack_dir / path.stem
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        with zipfile.ZipFile(path) as z:
            for member in z.namelist():
                target = (root / member).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise NotebookError(f"refusing to unpack {member!r} outside {root}")
            z.extractall(root)
    else:
        raise NotebookError(f"not a zip archive or folder: {path}")

    contents = sorted(root.rglob("*.content"))
    if not contents:
        raise NotebookError(f"no .content file in {path}")
    if len(contents) > 1:
        log.warning("several .content files, using %s", contents[0].name)
    content_path = contents[0]
    doc_id = content_path.stem
    content = json.loads(content_path.read_text())

    rm_files = {p.stem: p for p in root.rglob("*.rm")}
    page_ids = _page_order(content)
    if not page_ids:
        # Unknown layout: fall back to whatever .rm files exist.
        log.warning("no page list in %s; using .rm files in name order", content_path.name)
        page_ids = sorted(rm_files)

    fmt = content.get("formatVersion")
    if fmt is not None and fmt < 2:
        log.warning(".content formatVersion %s: pages may not be v6; rmscene may fail", fmt)

    pages = [Page(index=i, id=pid, rm_path=rm_files.get(pid)) for i, pid in enumerate(page_ids, start=1)]

    name, version = doc_id, None
    meta_files = list(root.rglob("*.metadata"))
    if meta_files:
        meta = json.loads(meta_files[0].read_text())
        name = meta.get("visibleName", name)
        version = meta.get("version")
    sidecar = path.with_suffix(".json") if path.is_file() else None
    if sidecar and sidecar.is_file():  # written by `scan`, carries the cloud version
        ref = json.loads(sidecar.read_text())
        name, version = ref.get("name", name), ref.get("version", version)

    return Notebook(id=doc_id, name=name, version=version, root=root, pages=pages,
                    file_type=content.get("fileType") or "notebook")


def _page_order(content: dict) -> list[str]:
    cpages = (content.get("cPages") or {}).get("pages")
    if cpages:
        live = [p for p in cpages if "deleted" not in p]
        live.sort(key=lambda p: (p.get("idx") or {}).get("value", ""))
        return [p["id"] for p in live]
    return list(content.get("pages") or [])


def select_pages(pages: list[Page], selector: str | list[int]) -> list[Page]:
    if selector == "all":
        return pages
    if selector == "last":
        return pages[-1:]
    wanted = set(selector)
    missing = wanted - {p.index for p in pages}
    if missing:
        log.warning("notebook has %d pages; ignoring page(s) %s", len(pages), sorted(missing))
    return [p for p in pages if p.index in wanted]
