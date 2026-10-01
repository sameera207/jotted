"""reMarkable library as a DocumentSource: rmapi for listing and download, rmscene for
pages, line clustering and Claude for reading. Pages are read straight from the
downloaded .rmdoc (a zip), so nothing is unpacked."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import zipfile
from pathlib import Path

from .. import analysis, classify, cloud, lines as lines_mod, recognise, strokes
from ..aicache import AICache
from ..config import Config
from ..core.model import DocInfo, PageInfo, SourceLine
from ..notebook import page_order

log = logging.getLogger("jotted.remarkable")

EMPTY = "empty"  # hash of a page with nothing drawn on it


class RemarkableLibrary:
    name = "remarkable"

    def __init__(self, cfg: Config, cache: AICache | None = None, listing_ttl_s: float = 20):
        self.cfg = cfg
        self.cache = cache or AICache(cfg.paths.cache_dir / "ai")
        self._listing: tuple[float, list, list] | None = None
        self._ttl = listing_ttl_s

    # ------------------------------------------------------------ listing

    def _library(self):
        if self._listing is None or time.monotonic() - self._listing[0] > self._ttl:
            docs, folders = cloud.library(self.cfg)
            self._listing = (time.monotonic(), docs, folders)
        return self._listing[1], self._listing[2]

    def list_documents(self) -> list[DocInfo]:
        docs, _ = self._library()
        return [DocInfo(source=self.name, id=e.doc.id, name=e.doc.name, folder=e.folder, modified=e.doc.modified)
                for e in docs]

    def folders(self) -> list[str]:
        return self._library()[1]

    # ------------------------------------------------------------ pages

    def _rmdoc(self, doc: DocInfo) -> Path:
        path = self.cfg.paths.cache_dir / f"{doc.id}.rmdoc"
        sidecar = path.with_suffix(".json")
        if path.is_file() and sidecar.is_file() and json.loads(sidecar.read_text()).get("modified") == doc.modified:
            return path  # already have this version
        ref = cloud.DocRef(id=doc.id, name=doc.name, version=0, modified=doc.modified, parent="")
        return cloud.download(self.cfg, ref)

    def pages(self, doc: DocInfo) -> list[PageInfo]:
        path = self._rmdoc(doc)
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            content = next((n for n in names if n.endswith(".content")), None)
            order = page_order(json.loads(z.read(content))) if content else []
            members = {n.rsplit("/", 1)[-1][:-3]: n for n in names if n.endswith(".rm")}
            out = []
            for i, pid in enumerate(order, start=1):
                member = members.get(pid)
                digest = hashlib.sha256(z.read(member)).hexdigest() if member else EMPTY
                out.append(PageInfo(doc_id=doc.id, id=pid, index=i, content_hash=digest))
        return out

    def page_strokes(self, doc_id: str, page_id: str) -> list:
        path = self.cfg.paths.cache_dir / f"{doc_id}.rmdoc"
        if not path.is_file():
            return []
        with zipfile.ZipFile(path) as z:
            member = next((n for n in z.namelist() if n.endswith(f"{page_id}.rm")), None)
            if member is None:
                return []
            with z.open(member) as f:
                return strokes.load_strokes_from(f, member, self.cfg.strokes)

    def stroke_ids(self, doc: DocInfo, page: PageInfo) -> set[str]:
        if page.content_hash == EMPTY:
            return set()
        return {s.id for s in self.page_strokes(doc.id, page.id)}

    def read_page(self, doc: DocInfo, page: PageInfo) -> list[SourceLine]:
        if page.content_hash == EMPTY:
            return []
        page_strokes = self.page_strokes(doc.id, page.id)
        page_lines, _ = lines_mod.cluster(page_strokes, self.cfg.lines)
        if not page_lines:
            return []
        transcripts = recognise.transcribe(page_lines, self.cfg.recognition, self.cache)
        merged: dict[int, list[int]] = {}
        if self.cfg.classification.enabled:
            pairs = sorted(classify.adjacent_drawings(page_lines, transcripts, self.cfg.classification)
                           + classify.continuations(page_lines, transcripts, self.cfg.classification, self.cache))
            page_lines, transcripts, merged = analysis.merge_continuations(page_lines, transcripts, pairs)
        out = []
        for ln in sorted(page_lines, key=lambda x: x.n):
            t = transcripts.get(ln.n)
            ids = sorted(s.id for s in ln.strokes)
            out.append(SourceLine(
                anchor=ln.anchor_id,
                key=hashlib.sha256(",".join(ids).encode()).hexdigest()[:24],
                text=t.text if t else "",
                bbox=ln.bbox,
                rows=list(ln.row_boxes),
                drawing=bool(t and t.drawing),
                checkbox=t.checkbox if t else "none",
                strokes=tuple(ids),
            ))
        return out
