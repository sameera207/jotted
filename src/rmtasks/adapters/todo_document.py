"""The To-do document on the tablet: a generated PDF with one checkbox per item.

Items sit in fixed slots (page, row) so a tick drawn on paper never drifts onto
another item when the list changes. The page count never changes after creation:
`put --content-only` keeps the page list, and the ink with it.

Ticks are read with the same calibrated mapping as the Tasks template
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

from .. import cloud, strokes, template
from ..config import Config
from ..core.model import TodoEntry
from ..notebook import page_order

log = logging.getLogger("rmtasks.todo")

PAGES = 20
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


def build_pdf(path: Path, entries: list[TodoEntry], pages: int = PAGES) -> Path:
    w, h = template.PAGE_W, template.PAGE_H
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
        c.drawString(BOX_X, 18, "Tick a box to mark it done. Items keep their place; new ones are added at the end.")
        c.showPage()
    c.save()
    return path


def ticked_rows(page_strokes: list, scale: float) -> set[int]:
    """Rows on one page whose checkbox area holds the centre of a stroke."""
    rows = set()
    for s in page_strokes:
        x_pt = (s.cx / scale + template.RM_W / 2) / template.UNITS_PER_PT
        y_pt = (s.cy / scale) / template.UNITS_PER_PT
        for row in range(SLOTS_PER_PAGE):
            x0, y0, x1, y1 = slot_box(row)
            if x0 - HIT_PAD <= x_pt <= x1 + HIT_PAD and y0 - HIT_PAD <= y_pt <= y1 + HIT_PAD:
                rows.add(row)
    return rows


class TodoDocument:
    """TodoPublisher for the reMarkable cloud."""

    def __init__(self, cfg: Config, name: str, folder: str):
        self.cfg, self.name, self.folder = cfg, name, folder

    def capacity(self) -> int:
        return PAGES * SLOTS_PER_PAGE

    def _find(self) -> cloud.DocRef | None:
        return cloud.find_document(self.cfg, self.name, self.folder)

    def publish(self, entries: list[TodoEntry]) -> None:
        existing = self._find()
        with tempfile.TemporaryDirectory() as tmp:
            pdf = build_pdf(Path(tmp) / f"{self.name}.pdf", entries)
            cloud.upload_pdf(self.cfg, pdf, content_only=existing is not None, folder=self.folder)
        log.info("published %d item(s) to %s", len(entries), self.name)

    def read_ticks(self) -> tuple[set[int], str] | None:
        doc = self._find()
        if doc is None:
            return None
        path = self.cfg.paths.cache_dir / f"{doc.id}.rmdoc"
        sidecar = path.with_suffix(".json")
        if not (path.is_file() and sidecar.is_file() and json.loads(sidecar.read_text()).get("modified") == doc.modified):
            path = cloud.download(self.cfg, doc)
        slots: set[int] = set()
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            content = next((n for n in names if n.endswith(".content")), None)
            order = page_order(json.loads(z.read(content))) if content else []
            for p, pid in enumerate(order):
                member = next((n for n in names if n.endswith(f"{pid}.rm")), None)
                if member is None:
                    continue
                with z.open(member) as f:
                    page_strokes = strokes.load_strokes_from(f, member, self.cfg.strokes)
                slots |= {p * SLOTS_PER_PAGE + r for r in ticked_rows(page_strokes, self.cfg.template.scale)}
        return slots, doc.modified
