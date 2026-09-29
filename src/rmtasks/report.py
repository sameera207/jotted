"""Terminal table, report.json and one SVG overlay per page."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from rich.console import Console
from rich.markup import escape as escape_markup
from rich.table import Table

from .checkbox import Checkbox
from .classify import Judgment
from .config import Config
from .lines import Line
from .recognise import Transcript
from .strokes import BBox, Stroke

# rM2 page in rmscene units: x is centred on 0, y runs down from the top.
PAGE_FRAME: BBox = (-702.0, 0.0, 702.0, 1872.0)
ACCENT = "#e8590c"


KINDS_WITH_BOX = ("task", "done", "empty_checkbox")
TASK_KINDS = ("task", "done")


@dataclass
class LineResult:
    line: Line
    kind: str  # "task", "done", "empty_checkbox", "note", "heading" or "drawing"
    checkbox: Checkbox | None  # best geometric candidate, set even when rejected
    geometric_kind: str = "note"  # what the geometric detector alone would say
    transcript: Transcript | None = None
    judgment: Judgment | None = None
    merged: list[int] = field(default_factory=list)  # line numbers folded into this one
    zone: str = "body"  # "header", "body" or "footer" on a template page

    @property
    def label(self) -> str:
        return "+".join(map(str, self.merged)) if len(self.merged) > 1 else str(self.line.n)

    @property
    def text(self) -> str | None:
        return self.transcript.text if self.transcript else None

    @property
    def box(self) -> str:
        if self.transcript:
            return self.transcript.checkbox
        return "empty" if self.geometric_kind in KINDS_WITH_BOX else "none"

    def to_json(self) -> dict:
        cb, j = self.checkbox, self.judgment
        data = {
            "n": self.line.n,
            "merged_lines": self.merged if len(self.merged) > 1 else None,
            "zone": self.zone,
            "kind": self.kind,
            "text": self.text,
            "checkbox": self.box,
            "p_todo": round(j.p_todo, 3) if j else None,
            "probabilities": {k: round(v, 3) for k, v in j.probabilities.items()} if j else None,
            "anchor_id": self.line.anchor_id,
            "stroke_ids": [s.id for s in self.line.strokes],
            "bbox": [round(v, 1) for v in self.line.bbox],
            "median_height": round(self.line.median_height, 1),
            "geometry": None,
        }
        if cb is not None:
            data["geometry"] = {
                "kind": self.geometric_kind,
                "style": cb.style,
                "confidence": round(cb.confidence, 3),
                "checkbox_ids": cb.ids,
                "checkbox_bbox": [round(v, 1) for v in cb.bbox],
                "checks": cb.checks,
            }
        return data


@dataclass
class PageResult:
    index: int
    id: str
    strokes: list[Stroke]
    lines: list[LineResult] = field(default_factory=list)
    unassigned: list[Stroke] = field(default_factory=list)
    date: str | None = None  # the header's text, on a template page

    def count(self, kind: str) -> int:
        return sum(1 for r in self.lines if r.kind == kind)

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "id": self.id,
            "date": self.date,
            "stroke_count": len(self.strokes),
            "lines": [r.to_json() for r in self.lines],
            "unassigned_ids": [s.id for s in self.unassigned],
        }


@dataclass
class Run:
    run_id: str
    out_dir: Path
    notebook: dict
    source: str
    pages: list[PageResult] = field(default_factory=list)
    page_count: int = 0  # pages in the notebook, analysed or not

    def to_json(self) -> dict:
        return {
            "run": self.run_id,
            "notebook": self.notebook,
            "source": self.source,
            "pages": [p.to_json() for p in self.pages],
        }


def write(run: Run, cfg: Config, console: Console | None = None) -> None:
    console = console or Console()
    formats = cfg.output.formats
    run.out_dir.mkdir(parents=True, exist_ok=True)
    if "table" in formats:
        print_table(run, console)
    if "json" in formats:
        (run.out_dir / "report.json").write_text(json.dumps(run.to_json(), indent=2) + "\n")
    if "svg" in formats:
        for page in run.pages:
            (run.out_dir / f"page-{page.index:02d}.svg").write_text(render_svg(page, cfg.output.svg_scale))
    console.print(f"[dim]Run written to {run.out_dir}[/dim]")


def print_table(run: Run, console: Console) -> None:
    for page in run.pages:
        console.print(
            f"[bold]Page {page.index}[/bold] ({page.id[:4]}…)"
            + (f"  date: {escape_markup(page.date)}" if page.date else "")
            + f"    lines: {len(page.lines)}   "
            f"tasks: {page.count('task')}   done: {page.count('done')}   empty: {page.count('empty_checkbox')}"
            + (f"   unassigned: {len(page.unassigned)}" if page.unassigned else "")
        )
        if not page.lines:
            console.print("  [dim](no strokes)[/dim]\n")
            continue
        table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
        for col, justify in (("#", "right"), ("kind", "left"), ("p(todo)", "right"), ("box", "left"),
                             ("text", "left"), ("anchor", "left")):
            table.add_column(col, justify=justify, overflow="fold")
        styles = {"task": "green", "done": "cyan", "empty_checkbox": "yellow", "note": "dim", "heading": "blue", "drawing": "magenta",
                  "header": "blue", "footer": "dim"}
        for r in page.lines:
            j = r.judgment
            table.add_row(
                r.label,
                f"[{styles[r.kind]}]{r.kind}[/]",
                f"{j.p_todo:.2f}" if j else "-",
                r.box,
                escape_markup(r.text) if r.text else "[dim]-[/dim]",
                r.line.anchor_id,
            )
        console.print(table)
        console.print()


def _union(boxes: list[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _polyline(s: Stroke, color: str, width: float = 2.0) -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in s.points)
    return (
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linecap="round" stroke-linejoin="round"><title>{escape(s.id)} {escape(s.tool)}</title></polyline>'
    )


def _rect(b: BBox, pad: float, **attrs: str) -> str:
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return (
        f'<rect x="{b[0] - pad:.1f}" y="{b[1] - pad:.1f}" '
        f'width="{b[2] - b[0] + 2 * pad:.1f}" height="{b[3] - b[1] + 2 * pad:.1f}" {extra}/>'
    )


def render_svg(page: PageResult, scale: float) -> str:
    """Strokes in grey; line bands numbered like the table, coloured by kind and labelled with
    the transcribed text; geometric checkbox candidates outlined (solid when the geometric
    detector alone calls it a task, dashed for empty, dotted grey when rejected)."""
    frame = _union([PAGE_FRAME] + [s.bbox for s in page.strokes])
    label_room = max((len(r.text or "") + 16) * 11 for r in page.lines) if page.lines else 0
    frame = (frame[0], frame[1], max(frame[2], max((r.line.bbox[2] for r in page.lines), default=0) + label_room), frame[3])
    margin = 40.0
    vx, vy = frame[0] - margin, frame[1] - margin
    vw, vh = frame[2] - frame[0] + 2 * margin, frame[3] - frame[1] + 2 * margin

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}" '
        f'width="{vw * scale:.0f}" height="{vh * scale:.0f}" font-family="Helvetica, Arial, sans-serif">',
        f'<rect x="{vx:.1f}" y="{vy:.1f}" width="{vw:.1f}" height="{vh:.1f}" fill="#ffffff"/>',
        _rect(PAGE_FRAME, 0, fill="none", stroke="#e5e5e5", stroke_width="2"),
    ]
    colours = {"task": "#2f9e44", "done": "#0c8599", "empty_checkbox": "#f08c00", "heading": "#1c7ed6",
               "drawing": "#ae3ec9"}
    for r in page.lines:
        b = r.line.bbox
        fill = colours.get(r.kind, "#868e96")
        out.append(_rect(b, 6, fill=fill, fill_opacity="0.07", stroke=fill, stroke_opacity="0.35", stroke_width="1.5"))
        out.append(
            f'<text x="{b[0] - 14:.1f}" y="{(b[1] + b[3]) / 2:.1f}" font-size="26" fill="{fill}" '
            f'text-anchor="end" dominant-baseline="middle">{r.label}</text>'
        )
        label = r.kind
        if r.judgment:
            label += f" {r.judgment.p_todo:.2f}"
        if r.text is not None:
            label += f" · {r.text}"
        out.append(
            f'<text x="{b[2] + 20:.1f}" y="{(b[1] + b[3]) / 2:.1f}" font-size="20" fill="{fill}" '
            f'dominant-baseline="middle">{escape(label)}</text>'
        )
    for s in page.strokes:
        out.append(_polyline(s, "#8a8a8a"))
    for s in page.unassigned:
        out.append(_polyline(s, "#e03131", 3.0))
    for r in page.lines:
        cb = r.checkbox
        if cb is None:
            continue
        if r.geometric_kind == "task":
            out.append(_rect(cb.bbox, 8, fill="none", stroke=ACCENT, stroke_width="3"))
        elif r.geometric_kind == "empty_checkbox":
            out.append(_rect(cb.bbox, 8, fill="none", stroke=ACCENT, stroke_width="3", stroke_dasharray="12 8"))
        else:
            out.append(_rect(cb.bbox, 8, fill="none", stroke="#adb5bd", stroke_width="2", stroke_dasharray="3 6"))
        failed = [k for k, ok in cb.checks.items() if not ok]
        label = f"{cb.style} {cb.confidence:.2f}" + (" ✗ " + ", ".join(failed) if failed else "")
        out.append(
            f'<text x="{cb.bbox[0] - 8:.1f}" y="{cb.bbox[1] - 14:.1f}" font-size="14" fill="#868e96">'
            f"{escape(label)}</text>"
        )
    out.append("</svg>\n")
    return "\n".join(out)
