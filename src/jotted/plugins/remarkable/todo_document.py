"""The To-do document on the tablet: a generated PDF with one checkbox per item.

Items sit in fixed slots (page, row) so a tick drawn on paper never drifts onto
another item when the list changes. The page count never changes after creation:
`put --content-only` keeps the page list, and the ink with it. So the document is
small, and rebuilt (deleted, then created again) when its rows run out.

Ticks are read with the calibrated mapping from the tablet's page size
(`template.scale`): a stroke whose centre falls on a slot's checkbox area ticks it.
"""

from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.pdfgen.canvas import Canvas



from ...config import Config
from ...core.model import PaperRead, TodoEntry, WrittenItem
from ...ink import lines as lines_mod
from ...ink.reader import InkReader
from . import cloud, page, rmfile
from .notebook import page_order

log = logging.getLogger("jotted.todo")

PAGES = 2
SLOTS_PER_PAGE = 20
TOP = 70.0  # pt from the top of the page to the first row
ROW = 26.0  # pt per row
BOX_X, BOX = 28.0, 11.0
TEXT_X = 48.0
HIT_PAD = 9.0  # pt around the checkbox that still counts as a tick

INK = Color(0.12, 0.12, 0.12)
MUTED = Color(0.5, 0.5, 0.5)


def slot_box(row: int) -> tuple[float, float, float, float]:
    """The checkbox of a row, in pt from the top-left: (x0, y0, x1, y1)."""
    top = TOP + row * ROW
    return BOX_X, top + 4, BOX_X + BOX, top + 4 + BOX


def _fit(c: Canvas, text: str, font: str, size: float, width: float) -> str:
    if c.stringWidth(text, font, size) <= width:
        return text
    while text and c.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def to_pt(x: float, y: float, scale: float) -> tuple[float, float]:
    """Tablet units to pt from the page's top-left."""
    return (x / scale + page.RM_W / 2) / page.UNITS_PER_PT, (y / scale) / page.UNITS_PER_PT


def row_of(y_pt: float) -> int:
    return int((y_pt - TOP) // ROW)


def build_pdf(path: Path, entries: list[TodoEntry], pages: int = PAGES, scale: float = 1.0) -> Path:
    w, h = page.PAGE_W, page.PAGE_H
    by_page: dict[int, list[TodoEntry]] = {}
    for e in entries:
        by_page.setdefault(e.slot // SLOTS_PER_PAGE, []).append(e)
    c = Canvas(str(path), pagesize=(w, h))
    c.setTitle(path.stem)
    open_count = sum(1 for e in entries if not e.done)
    for p in range(pages):
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(BOX_X, h - 42, path.stem)
        c.setFont("Helvetica", 9)
        c.setFillColor(MUTED)
        c.drawRightString(w - 24, h - 42, f"page {p + 1} of {pages}" + (f" · {open_count} open" if p == 0 else ""))
        c.setStrokeColor(MUTED)
        c.setLineWidth(0.6)
        c.line(BOX_X, h - 52, w - 24, h - 52)
        for e in by_page.get(p, []):
            row = e.slot % SLOTS_PER_PAGE
            x0, y0, x1, y1 = slot_box(row)
            c.setStrokeColor(INK)
            c.setLineWidth(0.9)
            c.rect(x0, h - y1, BOX, BOX, stroke=1, fill=0)
            base = h - (TOP + row * ROW) - 12
            if e.handwritten:
                # Written by hand in this row: the ink is the text. Print nothing over it; strike it when done,
                # and show a web edit as a small note under it.
                if e.done and e.ink:
                    (ix0, iy0), (ix1, iy1) = to_pt(e.ink[0], e.ink[1], scale), to_pt(e.ink[2], e.ink[3], scale)
                    c.setStrokeColor(INK)
                    c.setLineWidth(1.1)
                    c.line(ix0 - 2, h - (iy0 + iy1) / 2, ix1 + 2, h - (iy0 + iy1) / 2)
                if e.edited:
                    c.setFillColor(MUTED)
                    c.setFont("Helvetica", 7.5)
                    c.drawString(TEXT_X, base - 10, _fit(c, "edited: " + e.text, "Helvetica", 7.5, w - TEXT_X - 24))
                continue
            text = _fit(c, e.text, "Helvetica", 11.5, w - TEXT_X - 24)
            c.setFillColor(MUTED if e.done else INK)
            c.setFont("Helvetica", 11.5)
            c.drawString(TEXT_X, base, text)
            if e.done:
                c.setStrokeColor(INK)
                c.setLineWidth(1.1)
                c.line(TEXT_X - 2, base + 4, TEXT_X + 2 + c.stringWidth(text, "Helvetica", 11.5), base + 4)
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 7.5)
            c.drawString(TEXT_X, base - 10, _fit(c, e.source_label, "Helvetica", 7.5, w - TEXT_X - 24))
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 7.5)
        c.drawString(BOX_X, 18, "Tick a box to mark it done, or write a new item in an empty row. When the rows run out, a fresh list replaces this one.")
        c.showPage()
    c.save()
    return path


def _box_row(s, scale: float) -> int | None:
    """The row whose checkbox area holds the stroke's centre, if any."""
    x_pt, y_pt = to_pt(s.cx, s.cy, scale)
    for row in range(SLOTS_PER_PAGE):
        x0, y0, x1, y1 = slot_box(row)
        if x0 - HIT_PAD <= x_pt <= x1 + HIT_PAD and y0 - HIT_PAD <= y_pt <= y1 + HIT_PAD:
            return row
    return None


def ticked_rows(page_strokes: list, scale: float) -> set[int]:
    """Rows on one page whose checkbox area holds the centre of a stroke."""
    return {r for s in page_strokes if (r := _box_row(s, scale)) is not None}


def written_rows(page_strokes: list, scale: float, lines_cfg) -> dict[int, list]:
    """Handwriting outside the checkboxes, by row: {row: strokes}.

    Strokes are clustered into lines first (a descender or a tall letter can cross
    into the next row), and each line goes to the row holding its centre.
    """
    text_strokes = [s for s in page_strokes if _box_row(s, scale) is None]
    found, _ = lines_mod.cluster(text_strokes, lines_cfg)
    rows: dict[int, list] = {}
    for line in found:
        x0, y0, x1, y1 = line.bbox
        _, cy = to_pt((x0 + x1) / 2, (y0 + y1) / 2, scale)
        row = row_of(cy)
        if 0 <= row < SLOTS_PER_PAGE:
            rows.setdefault(row, []).extend(line.strokes)
    return rows


class TodoDocument:
    """TodoPublisher for the reMarkable cloud."""

    def __init__(self, cfg: Config, name: str, folder: str, ink: InkReader):
        self.cfg, self.name, self.folder, self.ink = cfg, name, folder, ink

    def capacity(self) -> int:
        return PAGES * SLOTS_PER_PAGE

    def find(self) -> cloud.DocRef | None:
        """The document, if it exists. rmapi addresses documents by name only, so with two of
        the same name nothing can be updated safely: stop and say which to delete."""
        docs = cloud.find_documents(self.cfg, self.name, self.folder)
        if len(docs) > 1:
            when = ", ".join(sorted(d.modified[:16].replace("T", " ") + " UTC" for d in docs))
            raise cloud.CloudError(
                f"There are {len(docs)} documents called {self.name!r} on your reMarkable (last changed {when}). "
                f"Delete the one you don't use (an old one has 20 pages) and the To-do carries on. "
                f"This happens when the tablet has the To-do open while Jotted replaces it.")
        return docs[0] if docs else None

    def document_id(self) -> str | None:
        found = self.find()
        return found.id if found else None

    def publish(self, entries: list[TodoEntry]) -> None:
        existing = self.find()
        with tempfile.TemporaryDirectory() as tmp:
            pdf = build_pdf(Path(tmp) / f"{self.name}.pdf", entries, scale=self.cfg.template.scale)
            cloud.upload_pdf(self.cfg, pdf, content_only=existing is not None, folder=self.folder)
        log.info("published %d item(s) to %s", len(entries), self.name)

    def delete(self) -> None:
        cloud.delete(self.cfg, self.name, self.folder)
        log.info("deleted %s to rebuild it", self.name)

    def read_paper(self, occupied: set[int]) -> PaperRead | None:
        doc = self.find()
        if doc is None:
            return None
        path = self.cfg.paths.cache_dir / f"{doc.id}.rmdoc"
        sidecar = path.with_suffix(".json")
        if not (path.is_file() and sidecar.is_file() and json.loads(sidecar.read_text()).get("modified") == doc.modified):
            path = cloud.download(self.cfg, doc)
        scale = self.cfg.template.scale
        read = PaperRead(doc_id=doc.id, marker=doc.modified)
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            content = next((n for n in names if n.endswith(".content")), None)
            order = page_order(json.loads(z.read(content))) if content else []
            read.capacity = len(order) * SLOTS_PER_PAGE or None  # no page list until the tablet opens it
            for p, pid in enumerate(order):
                member = next((n for n in names if n.endswith(f"{pid}.rm")), None)
                if member is None:
                    continue
                with z.open(member) as f:
                    page_strokes = rmfile.load_strokes_from(f, member, self.cfg.strokes)
                ticks = ticked_rows(page_strokes, scale)
                read.ticks |= {p * SLOTS_PER_PAGE + r for r in ticks}
                rows = written_rows(page_strokes, scale, self.cfg.lines)
                read.inked |= {p * SLOTS_PER_PAGE + r for r in ticks | rows.keys()}
                for row, row_strokes in rows.items():
                    slot = p * SLOTS_PER_PAGE + row
                    if slot in occupied:
                        continue  # writing next to an existing item: a note on it, not a new item
                    item = self._read_row(row_strokes, slot, pid, p + 1, row in ticks)
                    if item:
                        read.written.append(item)
        return read

    def _read_row(self, row_strokes: list, slot: int, page_id: str, page_index: int,
                  box_inked: bool) -> WrittenItem | None:
        line = self.ink.line(row_strokes)
        if line is None:
            return None
        return WrittenItem(slot=slot, page_id=page_id, page_index=page_index, text=line.text,
                           anchor=line.anchor, key=line.key, bbox=line.bbox, box_inked=box_inked)
