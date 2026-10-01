"""SQLite task store: the source of truth between the paper and the web.

Paper tasks are keyed by (notebook, anchor stroke). Each field keeps the time it
last changed, and the most recent change wins:

- A web edit is timed when it is made.
- A paper change is timed by the notebook's cloud modification time, the only
  clock the tablet gives us. It is recorded when a pull first sees it.

`paper_text` and `paper_status` remember what the paper said at the last pull,
so a pull only counts as a paper change when the paper itself changed. Our
printed strike-throughs are not ink, so they never read back as paper changes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import report, template

SCHEMA = """
CREATE TABLE IF NOT EXISTS notebooks (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    file_type      TEXT NOT NULL,
    page_count     INTEGER NOT NULL DEFAULT 0,
    paper_modified TEXT,
    last_pull_at   TEXT,
    last_push_at   TEXT,
    web_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    notebook_id TEXT NOT NULL,
    page_index  INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    date        TEXT,
    has_ink     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (notebook_id, page_index)
);
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY,
    notebook_id       TEXT NOT NULL,
    origin            TEXT NOT NULL CHECK (origin IN ('paper', 'web')),
    anchor_id         TEXT,
    page_index        INTEGER,
    text              TEXT NOT NULL,
    paper_text        TEXT,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    paper_status      TEXT,
    text_changed_at   TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    rows              TEXT,
    strike_from       REAL,
    missing           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (notebook_id, anchor_id)
);
CREATE TABLE IF NOT EXISTS syncs (
    id          INTEGER PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    at          TEXT NOT NULL,
    run_id      TEXT,
    detail      TEXT
);
"""

TASK_KINDS = ("task", "done")
BULLETS = "-–—•*·>"


def clean_text(text: str) -> str:
    """A task's text without the bullet it was written with: "- test prod" -> "test prod"."""
    return text.strip().lstrip(BULLETS).strip()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def to_utc(stamp: str | None) -> str:
    """Normalise a cloud timestamp (RFC 3339, maybe nanoseconds) to our format; now() if unusable."""
    if not stamp:
        return now()
    s = stamp.strip().replace("Z", "+00:00")
    if "." in s:  # trim sub-second digits beyond microseconds
        head, _, rest = s.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits):]
        s = f"{head}.{digits[:6]}{tz}"
    try:
        return datetime.fromisoformat(s).astimezone(UTC).isoformat(timespec="microseconds")
    except ValueError:
        return now()


@dataclass
class PullSummary:
    new: int = 0
    text_from_paper: int = 0
    status_from_paper: int = 0
    missing: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)
            cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)")}
            if "web_changed_at" not in cols:  # stores created before this column existed
                db.execute("ALTER TABLE notebooks ADD COLUMN web_changed_at TEXT")
                # Unknown whether older web edits were pushed: treat them as pending.
                db.execute("UPDATE notebooks SET web_changed_at = ?", (now(),))

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ pull: paper -> store

    def apply_run(self, run: report.Run, paper_modified: str | None) -> PullSummary:
        """Fold a scan into the store. Paper changes win when newer than the web's."""
        nb = run.notebook
        paper_at = to_utc(paper_modified)
        stamp = now()
        summary = PullSummary()
        with self.db() as db:
            db.execute(
                """INSERT INTO notebooks (id, name, file_type, page_count, paper_modified, last_pull_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET name = excluded.name, file_type = excluded.file_type,
                     page_count = excluded.page_count, paper_modified = excluded.paper_modified,
                     last_pull_at = excluded.last_pull_at""",
                (nb["id"], nb["name"], nb.get("file_type", "notebook"), run.page_count, paper_at, stamp),
            )
            seen: set[str] = set()
            for page in run.pages:
                db.execute(
                    """INSERT INTO pages (notebook_id, page_index, page_id, date, has_ink) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (notebook_id, page_index) DO UPDATE SET page_id = excluded.page_id,
                         date = excluded.date, has_ink = excluded.has_ink""",
                    (nb["id"], page.index, page.id, page.date, int(bool(page.strokes))),
                )
                for r in page.lines:
                    if r.zone != "body":
                        continue
                    anchor = r.line.anchor_id
                    row = db.execute("SELECT * FROM tasks WHERE notebook_id = ? AND anchor_id = ?",
                                     (nb["id"], anchor)).fetchone()
                    if row is None and r.kind not in TASK_KINDS:
                        continue
                    seen.add(anchor)
                    text = clean_text(r.text or "")
                    paper_status = "done" if r.kind == "done" else "open"
                    rows = json.dumps([list(b) for b in r.line.row_boxes])
                    strike_from = r.checkbox.bbox[2] if (r.box != "none" and r.checkbox is not None) else None
                    if row is None:
                        db.execute(
                            """INSERT INTO tasks (notebook_id, origin, anchor_id, page_index, text, paper_text, status,
                                 paper_status, text_changed_at, status_changed_at, rows, strike_from, created_at,
                                 updated_at)
                               VALUES (?, 'paper', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (nb["id"], anchor, page.index, text, text, paper_status, paper_status, paper_at,
                             paper_at, rows, strike_from, stamp, stamp),
                        )
                        summary.new += 1
                        continue
                    updates = {"page_index": page.index, "rows": rows, "strike_from": strike_from, "missing": 0}
                    if text and text != row["paper_text"] and clean_text(row["paper_text"] or "") == text:
                        # Same words, stored before bullets were stripped: tidy, not a change.
                        updates["paper_text"] = text
                        if row["text"] == row["paper_text"]:
                            updates["text"] = text
                    elif text and text != row["paper_text"]:  # the paper's text changed
                        updates["paper_text"] = text
                        if paper_at > row["text_changed_at"]:
                            updates.update(text=text, text_changed_at=paper_at)
                            summary.text_from_paper += 1
                    paper_now = updates.get("paper_text", row["paper_text"]) or ""
                    if "text" not in updates and row["text"] != paper_now and clean_text(row["text"]) == paper_now:
                        updates["text"] = paper_now  # only the bullet differs: tidy, not a web edit
                    if paper_status != row["paper_status"]:  # the paper's status changed
                        updates["paper_status"] = paper_status
                        if paper_at > row["status_changed_at"]:
                            updates.update(status=paper_status, status_changed_at=paper_at)
                            summary.status_from_paper += 1
                    self._update(db, row["id"], updates, stamp)

            analysed = [p.index for p in run.pages]
            if analysed:
                marks = ",".join("?" * len(analysed))
                gone = db.execute(
                    f"""SELECT id, anchor_id FROM tasks WHERE notebook_id = ? AND origin = 'paper' AND missing = 0
                        AND page_index IN ({marks})""", (nb["id"], *analysed)).fetchall()
                for row in gone:
                    if row["anchor_id"] not in seen:
                        self._update(db, row["id"], {"missing": 1}, stamp)
                        summary.missing += 1
            db.execute("INSERT INTO syncs (notebook_id, kind, at, run_id, detail) VALUES (?, 'pull', ?, ?, ?)",
                       (nb["id"], stamp, run.run_id, json.dumps(summary.as_dict())))
        return summary

    @staticmethod
    def _web_changed(db: sqlite3.Connection, notebook_id: str, stamp: str) -> None:
        """Any web change (add, edit, tick, un-tick, delete) means the PDF needs reprinting."""
        db.execute("UPDATE notebooks SET web_changed_at = ? WHERE id = ?", (stamp, notebook_id))

    @staticmethod
    def _update(db: sqlite3.Connection, task_id: int, fields: dict, stamp: str) -> None:
        fields = {**fields, "updated_at": stamp}
        cols = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    # ------------------------------------------------------------ web edits

    def add_web_task(self, notebook_id: str, text: str) -> int:
        stamp = now()
        with self.db() as db:
            cur = db.execute(
                """INSERT INTO tasks (notebook_id, origin, text, status, text_changed_at, status_changed_at,
                     created_at, updated_at) VALUES (?, 'web', ?, 'open', ?, ?, ?, ?)""",
                (notebook_id, text.strip(), stamp, stamp, stamp, stamp),
            )
            self._web_changed(db, notebook_id, stamp)
            return int(cur.lastrowid)

    def edit_task(self, task_id: int, *, text: str | None = None, status: str | None = None) -> dict:
        stamp = now()
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            fields: dict = {}
            if text is not None and text.strip() and text.strip() != row["text"]:
                fields.update(text=text.strip(), text_changed_at=stamp)
            if status is not None and status != row["status"]:
                if status not in ("open", "done"):
                    raise ValueError("status must be 'open' or 'done'")
                fields.update(status=status, status_changed_at=stamp)
            if fields:
                self._update(db, task_id, fields, stamp)
                self._web_changed(db, row["notebook_id"], stamp)
        return self.task(task_id)

    def delete_web_task(self, task_id: int) -> None:
        with self.db() as db:
            row = db.execute("SELECT origin, notebook_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["origin"] != "web":
                raise ValueError("only web tasks can be deleted; mark a paper task done instead")
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._web_changed(db, row["notebook_id"], now())

    # ------------------------------------------------------------ reads

    def task(self, task_id: int) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_dict(row)

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["rows"] = json.loads(d["rows"]) if d["rows"] else []
        d["edited"] = d["origin"] == "paper" and d["paper_text"] is not None and d["text"] != d["paper_text"]
        return d

    def notebook(self, notebook_id: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        return dict(row) if row else None

    def notebook_by_name(self, name: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM notebooks WHERE name = ? ORDER BY last_pull_at DESC", (name,)).fetchone()
        return dict(row) if row else None

    def state(self, notebook_id: str) -> dict:
        """Everything the web page shows: pages with their tasks, and sync status."""
        with self.db() as db:
            nb = db.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
            pages = {r["page_index"]: dict(r) for r in db.execute(
                "SELECT * FROM pages WHERE notebook_id = ? ORDER BY page_index", (notebook_id,))}
            tasks = [self._task_dict(r) for r in db.execute(
                "SELECT * FROM tasks WHERE notebook_id = ? ORDER BY origin, page_index, id", (notebook_id,))]
        # A handwritten task that left the page (erased, or merged into another line) is only
        # worth showing if the web changed it; otherwise it would linger as clutter.
        tasks = [t for t in tasks if not (t["missing"] and not t["edited"] and t["status"] == t["paper_status"])]
        current = self.current_page(notebook_id)
        pending = bool(nb) and (nb["web_changed_at"] or "") > (nb["last_push_at"] or "")
        grouped: dict[int, dict] = {
            idx: {"index": idx, "date": pg.get("date"), "tasks": []} for idx, pg in pages.items() if pg["has_ink"]
        }
        for t in tasks:
            idx = t["page_index"] or current
            page = grouped.setdefault(idx, {"index": idx, "date": (pages.get(idx) or {}).get("date"), "tasks": []})
            page["tasks"].append(t)
        return {
            "notebook": dict(nb) if nb else None,
            "current_page": current,
            "pending_push": pending,
            "pages": [grouped[k] for k in sorted(grouped)],
        }

    def current_page(self, notebook_id: str) -> int:
        """Where new web tasks go: the last page with any ink, else page 1."""
        with self.db() as db:
            row = db.execute("SELECT MAX(page_index) AS p FROM pages WHERE notebook_id = ? AND has_ink = 1",
                             (notebook_id,)).fetchone()
        return int(row["p"]) if row and row["p"] else 1

    # ------------------------------------------------------------ push: store -> PDF

    def page_states(self, notebook_id: str, pin: bool = True) -> tuple[dict[int, template.PageState], str]:
        """What to print on each page, and the time of this snapshot (pass it to record_push):
        - a paper task done on the web: a strike through each of its rows
        - a paper task edited on the web: its handwriting outlined and numbered (not struck:
          a strike means done), and the new text in the footer under the same number
        - a web task: a footer row on its page (struck through when done)"""
        current = self.current_page(notebook_id)
        pages: dict[int, template.PageState] = {}
        labels: dict[int, int] = {}
        stamp = now()
        with self.db() as db:
            rows = db.execute("SELECT * FROM tasks WHERE notebook_id = ? AND missing = 0 ORDER BY created_at, id",
                              (notebook_id,)).fetchall()
            for row in rows:
                t = self._task_dict(row)
                if t["origin"] == "web" and t["page_index"] is None:
                    if pin:  # pin to the page it is first printed on; a preview only shows where it would go
                        self._update(db, t["id"], {"page_index": current}, stamp)
                    t["page_index"] = current
                ps = pages.setdefault(t["page_index"] or current, template.PageState())
                done = t["status"] == "done"
                if t["origin"] == "web":
                    ps.footer.append((t["text"], done))
                    continue
                if t["edited"]:
                    page = t["page_index"] or current
                    labels[page] = labels.get(page, 0) + 1
                    ps.moved.append(([tuple(r) for r in t["rows"]], labels[page]))
                    ps.footer.append((t["text"], done, labels[page]))
                elif done and t["paper_status"] != "done":
                    for i, (x0, y0, x1, y1) in enumerate(t["rows"]):
                        start = t["strike_from"] if (i == 0 and t["strike_from"] is not None) else x0
                        ps.strikes.append((start, x1, (y0 + y1) / 2))
        return pages, stamp

    def record_push(self, notebook_id: str, run_id: str, detail: dict, snapshot_at: str | None = None) -> None:
        """snapshot_at: when page_states read the store. Changes after it stay pending."""
        stamp = snapshot_at or now()
        with self.db() as db:
            db.execute("UPDATE notebooks SET last_push_at = ? WHERE id = ?", (stamp, notebook_id))
            db.execute("INSERT INTO syncs (notebook_id, kind, at, run_id, detail) VALUES (?, 'push', ?, ?, ?)",
                       (notebook_id, stamp, run_id, json.dumps(detail)))

    def recent_syncs(self, notebook_id: str, limit: int = 10) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM syncs WHERE notebook_id = ? ORDER BY id DESC LIMIT ?", (notebook_id, limit))]
