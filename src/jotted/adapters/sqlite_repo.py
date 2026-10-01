"""SQLite implementation of the core's Repository port.

Every item lives in `actions`, whatever its origin (`source`): "remarkable" (collected
from a watched document), "todo" (written by hand in an empty row of the To-do
document) or "web" (added in the web app).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

from ..core.model import DocInfo, Judgment, PageInfo, Settings, SourceLine, TodoEntry, WrittenItem

BULLETS = "-–—•*·>"
WEB_DOC = "web"  # doc_id of items added in the web app


def clean_text(text: str) -> str:
    """An item's text without the bullet it was written with: "- test prod" -> "test prod"."""
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_docs (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    folder       TEXT NOT NULL,
    marker       TEXT,
    page_count   INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS source_pages (
    doc_id  TEXT NOT NULL,
    page_id TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    hash    TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_id)
);
CREATE TABLE IF NOT EXISTS source_baseline (
    doc_id  TEXT NOT NULL,
    page_id TEXT NOT NULL,
    strokes TEXT NOT NULL,  -- JSON list of the mark IDs on the page when it was recorded
    PRIMARY KEY (doc_id, page_id)
);
CREATE TABLE IF NOT EXISTS source_lines (
    doc_id   TEXT NOT NULL,
    page_id  TEXT NOT NULL,
    anchor   TEXT NOT NULL,
    key      TEXT NOT NULL,
    text     TEXT NOT NULL,
    bbox     TEXT NOT NULL,
    rows     TEXT,
    drawing  INTEGER NOT NULL DEFAULT 0,
    p_action REAL,
    owner    TEXT,
    PRIMARY KEY (doc_id, page_id, anchor)
);
CREATE TABLE IF NOT EXISTS actions (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    doc_id            TEXT NOT NULL,
    doc_name          TEXT NOT NULL,
    folder            TEXT NOT NULL,
    page_id           TEXT NOT NULL,
    page_index        INTEGER NOT NULL,
    anchor            TEXT NOT NULL,
    bbox              TEXT,
    text              TEXT NOT NULL,
    paper_text        TEXT NOT NULL,
    owner             TEXT NOT NULL,
    p_action          REAL NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    text_changed_at   TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    missing           INTEGER NOT NULL DEFAULT 0,
    dismissed         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (doc_id, anchor)
);
CREATE TABLE IF NOT EXISTS todo_slots (
    slot    INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,  -- always 'action' now; 'task' was the retired Tasks notebook
    item_id INTEGER NOT NULL,
    ticked  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, item_id)
);
CREATE TABLE IF NOT EXISTS todo_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _bbox(b) -> str:
    return json.dumps([round(v, 1) for v in b])


class SqliteRepository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)
            cols = {r["name"] for r in db.execute("PRAGMA table_info(actions)")}
            if "written" not in cols:  # 1 = written by hand on the To-do document itself
                try:
                    db.execute("ALTER TABLE actions ADD COLUMN written INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError as e:  # another app starting at the same time added it
                    if "duplicate column" not in str(e):
                        raise
            self._migrate_tasks(db)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate_tasks(db) -> None:
        """Once: turn the retired Tasks notebook's tasks into items.

        A handwritten task keeps its notebook and anchor stroke, so if that notebook is
        watched later its lines match these items instead of adding them again. A row it
        held on the To-do document stays its row. The old tables are left in place.
        """
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone() is None:
            return
        db.commit()
        db.execute("BEGIN IMMEDIATE")  # two apps starting at once: the second waits, then sees it done
        if db.execute("SELECT 1 FROM todo_meta WHERE key = 'tasks_migrated'").fetchone():
            return
        books = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM notebooks")}
        page_ids = {(r["notebook_id"], r["page_index"]): r["page_id"] for r in db.execute("SELECT * FROM pages")}
        stamp = now()
        for t in db.execute("SELECT * FROM tasks WHERE missing = 0 ORDER BY id").fetchall():
            paper = t["origin"] == "paper" and t["anchor_id"]
            if paper:
                doc_id, anchor, name = t["notebook_id"], t["anchor_id"], books.get(t["notebook_id"], "Tasks")
                page_index = t["page_index"] or 1
                page_id = page_ids.get((doc_id, page_index), "")
                rows = json.loads(t["rows"]) if t["rows"] else []
                bbox = [min(r[0] for r in rows), min(r[1] for r in rows),
                        max(r[2] for r in rows), max(r[3] for r in rows)] if rows else None
            else:
                doc_id, anchor, name, page_index, page_id, rows, bbox = WEB_DOC, uuid.uuid4().hex, "", 0, "", [], None
            cur = db.execute(
                """INSERT OR IGNORE INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, bbox,
                     text, paper_text, owner, p_action, status, text_changed_at, status_changed_at, created_at,
                     updated_at) VALUES (?, ?, ?, '/', ?, ?, ?, ?, ?, ?, 'me', 1.0, ?, ?, ?, ?, ?)""",
                ("remarkable" if paper else "web", doc_id, name, page_id, page_index, anchor,
                 _bbox(bbox) if bbox else None, t["text"], t["paper_text"] or t["text"], t["status"],
                 t["text_changed_at"], t["status_changed_at"], t["created_at"], stamp),
            )
            if cur.rowcount:
                item_id = cur.lastrowid
                if paper and page_id and bbox:  # so the web app can show the page and the handwritten line
                    db.execute("INSERT OR IGNORE INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, '')",
                               (doc_id, page_id, page_index))
                    db.execute(
                        """INSERT OR IGNORE INTO source_lines (doc_id, page_id, anchor, key, text, bbox, rows)
                           VALUES (?, ?, ?, '', ?, ?, ?)""",  # key '': judged afresh if the notebook is watched
                        (doc_id, page_id, anchor, t["paper_text"] or t["text"], _bbox(bbox), json.dumps(rows)),
                    )
            else:  # already collected from that notebook: the more recent tick or untick wins
                row = db.execute("SELECT id, status_changed_at FROM actions WHERE doc_id = ? AND anchor = ?",
                                 (doc_id, anchor)).fetchone()
                item_id = row["id"]
                if t["status_changed_at"] > row["status_changed_at"]:
                    db.execute("UPDATE actions SET status = ?, status_changed_at = ?, updated_at = ? WHERE id = ?",
                               (t["status"], t["status_changed_at"], stamp, item_id))
            if db.execute("SELECT 1 FROM todo_slots WHERE kind = 'action' AND item_id = ?", (item_id,)).fetchone():
                db.execute("DELETE FROM todo_slots WHERE kind = 'task' AND item_id = ?", (t["id"],))
            else:
                db.execute("UPDATE todo_slots SET kind = 'action', item_id = ? WHERE kind = 'task' AND item_id = ?",
                           (item_id, t["id"]))
        db.execute("DELETE FROM todo_slots WHERE kind = 'task'")  # tasks gone from their notebook
        db.execute("INSERT INTO todo_meta (key, value) VALUES ('tasks_migrated', ?)", (stamp,))

    # ------------------------------------------------------------ settings

    def settings(self) -> Settings:
        with self.db() as db:
            stored = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in stored.items() if k in known})

    def save_settings(self, settings: Settings) -> None:
        with self.db() as db:
            for k, v in asdict(settings).items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))

    # ------------------------------------------------------------ collection state

    def doc_marker(self, source: str, doc_id: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT marker FROM source_docs WHERE source = ? AND id = ?", (source, doc_id)).fetchone()
        return row["marker"] if row else None

    def save_doc(self, doc: DocInfo, page_count: int) -> None:
        with self.db() as db:
            db.execute(
                """INSERT INTO source_docs (source, id, name, folder, marker, page_count, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (source, id) DO UPDATE SET name = excluded.name, folder = excluded.folder,
                     marker = excluded.marker, page_count = excluded.page_count, collected_at = excluded.collected_at""",
                (doc.source, doc.id, doc.name, doc.folder, doc.modified, page_count, now()),
            )
            db.execute("UPDATE actions SET doc_name = ?, folder = ? WHERE doc_id = ?", (doc.name, doc.folder, doc.id))

    def page_hash(self, doc_id: str, page_id: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT hash FROM source_pages WHERE doc_id = ? AND page_id = ?",
                             (doc_id, page_id)).fetchone()
        return row["hash"] if row else None

    def save_baseline(self, doc: DocInfo, page: PageInfo, strokes: set[str]) -> None:
        with self.db() as db:
            db.execute(
                """INSERT INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, ?)
                   ON CONFLICT (doc_id, page_id) DO UPDATE SET idx = excluded.idx, hash = excluded.hash""",
                (doc.id, page.id, page.index, page.content_hash),
            )
            db.execute("INSERT OR REPLACE INTO source_baseline (doc_id, page_id, strokes) VALUES (?, ?, ?)",
                       (doc.id, page.id, json.dumps(sorted(strokes))))

    def baseline_strokes(self, doc_id: str, page_id: str) -> set[str] | None:
        with self.db() as db:
            row = db.execute("SELECT strokes FROM source_baseline WHERE doc_id = ? AND page_id = ?",
                             (doc_id, page_id)).fetchone()
        return set(json.loads(row["strokes"])) if row else None

    def baselined_docs(self) -> set[str]:
        with self.db() as db:
            return {r["doc_id"] for r in db.execute("SELECT DISTINCT doc_id FROM source_baseline")}

    def baseline_pages(self) -> dict[str, int]:
        """doc id -> how many of its pages were recorded as a baseline."""
        with self.db() as db:
            return {r["doc_id"]: r["n"] for r in db.execute(
                "SELECT doc_id, COUNT(*) AS n FROM source_baseline GROUP BY doc_id")}

    def clear_baseline(self, doc_id: str) -> None:
        """Baseline pages are read again in full; lines already judged on them keep their judgments."""
        with self.db() as db:
            pages = [r["page_id"] for r in db.execute("SELECT page_id FROM source_baseline WHERE doc_id = ?",
                                                      (doc_id,))]
            for pid in pages:
                db.execute("DELETE FROM source_pages WHERE doc_id = ? AND page_id = ?", (doc_id, pid))
                db.execute("DELETE FROM source_lines WHERE doc_id = ? AND page_id = ? AND p_action IS NULL",
                           (doc_id, pid))
            db.execute("DELETE FROM source_baseline WHERE doc_id = ?", (doc_id,))
            db.execute("UPDATE source_docs SET marker = NULL WHERE id = ?", (doc_id,))

    def line_keys(self, doc_id: str, page_id: str) -> dict[str, str]:
        with self.db() as db:
            return {r["anchor"]: r["key"] for r in db.execute(
                "SELECT anchor, key FROM source_lines WHERE doc_id = ? AND page_id = ?", (doc_id, page_id))}

    def save_page(self, doc: DocInfo, page: PageInfo, lines: list[SourceLine],
                  judgments: dict[str, Judgment], threshold: float) -> tuple[int, int, int]:
        stamp, paper_at = now(), to_utc(doc.modified)
        added = updated = missing = 0
        with self.db() as db:
            db.execute(
                """INSERT INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, ?)
                   ON CONFLICT (doc_id, page_id) DO UPDATE SET idx = excluded.idx, hash = excluded.hash""",
                (doc.id, page.id, page.index, page.content_hash),
            )
            present = {ln.anchor for ln in lines}
            for ln in lines:
                j = judgments.get(ln.anchor)
                prev = db.execute("SELECT * FROM source_lines WHERE doc_id = ? AND page_id = ? AND anchor = ?",
                                  (doc.id, page.id, ln.anchor)).fetchone()
                p_action = j.p_action if j else (prev["p_action"] if prev and prev["key"] == ln.key else None)
                owner = j.owner if j else (prev["owner"] if prev and prev["key"] == ln.key else None)
                db.execute(
                    """INSERT INTO source_lines (doc_id, page_id, anchor, key, text, bbox, rows, drawing, p_action, owner)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (doc_id, page_id, anchor) DO UPDATE SET key = excluded.key, text = excluded.text,
                         bbox = excluded.bbox, rows = excluded.rows, drawing = excluded.drawing,
                         p_action = excluded.p_action, owner = excluded.owner""",
                    (doc.id, page.id, ln.anchor, ln.key, ln.text, _bbox(ln.bbox),
                     json.dumps([[round(v, 1) for v in r] for r in (ln.rows or [ln.bbox])]), int(ln.drawing),
                     p_action, owner),
                )
                action = db.execute("SELECT * FROM actions WHERE doc_id = ? AND anchor = ?",
                                    (doc.id, ln.anchor)).fetchone()
                text = clean_text(ln.text)
                is_action = p_action is not None and p_action >= threshold and text and not ln.drawing
                if action is None:
                    if is_action:
                        db.execute(
                            """INSERT INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, bbox,
                                 text, paper_text, owner, p_action, text_changed_at, status_changed_at, created_at,
                                 updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (doc.source, doc.id, doc.name, doc.folder, page.id, page.index, ln.anchor, _bbox(ln.bbox),
                             text, text, owner or "unclear", p_action, paper_at, paper_at, stamp, stamp),
                        )
                        added += 1
                    continue
                fields_: dict = {"page_id": page.id, "page_index": page.index, "bbox": _bbox(ln.bbox), "missing": 0}
                if j is not None:  # the line changed and was judged again
                    fields_.update(p_action=p_action, owner=owner or action["owner"])
                    if not is_action:
                        fields_["missing"] = 1  # no longer reads as an action
                    if text and text != action["paper_text"]:
                        fields_["paper_text"] = text
                        if paper_at > action["text_changed_at"]:
                            fields_.update(text=text, text_changed_at=paper_at)
                    updated += 1
                self._update(db, "actions", action["id"], fields_, stamp)

            stored = db.execute("SELECT anchor FROM source_lines WHERE doc_id = ? AND page_id = ?",
                                (doc.id, page.id)).fetchall()
            for row in stored:
                if row["anchor"] in present:
                    continue
                db.execute("DELETE FROM source_lines WHERE doc_id = ? AND page_id = ? AND anchor = ?",
                           (doc.id, page.id, row["anchor"]))
                gone = db.execute("SELECT id FROM actions WHERE doc_id = ? AND anchor = ? AND missing = 0",
                                  (doc.id, row["anchor"])).fetchone()
                if gone:
                    self._update(db, "actions", gone["id"], {"missing": 1}, stamp)
                    missing += 1
        return added, updated, missing

    @staticmethod
    def _update(db, table: str, row_id: int, values: dict, stamp: str) -> None:
        values = {**values, "updated_at": stamp}
        cols = ", ".join(f"{k} = ?" for k in values)
        db.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*values.values(), row_id))

    # ------------------------------------------------------------ the combined list

    def items(self, status: str | None = None, owner: str | None = None, folder: str | None = None) -> list[dict]:
        """Every item on the list, for the web app."""
        out: list[dict] = []
        with self.db() as db:
            slots = {r["item_id"]: r["slot"] for r in db.execute("SELECT * FROM todo_slots")}
            for r in db.execute("SELECT * FROM actions WHERE dismissed = 0 AND missing = 0 ORDER BY created_at, id"):
                out.append({
                    "id": r["id"], "origin": r["source"], "text": r["text"], "paper_text": r["paper_text"],
                    "written": bool(r["written"]), "bbox": json.loads(r["bbox"]) if r["bbox"] else None,
                    "status": r["status"], "owner": r["owner"], "p_action": round(r["p_action"], 2),
                    "source": {"doc_id": r["doc_id"], "name": r["doc_name"], "folder": r["folder"],
                               "page": r["page_index"], "anchor": r["anchor"]},
                    "edited": r["source"] != "web" and r["text"] != r["paper_text"], "slot": slots.get(r["id"]),
                    "created_at": r["created_at"],
                })
        if status:
            out = [i for i in out if i["status"] == status]
        if owner == "mine":
            out = [i for i in out if i["owner"] in ("me", "unclear")]
        elif owner == "others":
            out = [i for i in out if i["owner"] == "someone_else"]
        if folder:
            f = "/" + folder.strip("/")
            out = [i for i in out if (i["source"]["folder"] + "/").startswith(f.rstrip("/") + "/")]
        return out

    def item(self, item_id: int) -> dict:
        found = next((i for i in self.items() if i["id"] == item_id), None)
        if found is None:
            raise KeyError(item_id)
        return found

    def add_item(self, text: str) -> int:
        """An item typed in the web app: yours, and printed on the To-do document at its next update."""
        text = clean_text(text)
        if not text:
            raise ValueError("The item is empty")
        stamp = now()
        with self.db() as db:
            cur = db.execute(
                """INSERT INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, text,
                     paper_text, owner, p_action, text_changed_at, status_changed_at, created_at, updated_at)
                   VALUES ('web', ?, '', '', '', 0, ?, ?, ?, 'me', 1.0, ?, ?, ?, ?)""",
                (WEB_DOC, uuid.uuid4().hex, text, text, stamp, stamp, stamp, stamp),
            )
            self._web_changed(db, stamp)
        return int(cur.lastrowid)

    @staticmethod
    def _web_changed(db, stamp: str) -> None:
        db.execute("INSERT INTO todo_meta (key, value) VALUES ('web_changed_at', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (stamp,))

    def edit_action(self, action_id: int, *, text: str | None = None, status: str | None = None,
                    dismissed: bool | None = None) -> dict:
        stamp = now()
        with self.db() as db:
            row = db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            values: dict = {}
            if text is not None and text.strip() and text.strip() != row["text"]:
                values.update(text=text.strip(), text_changed_at=stamp)
            if status is not None and status != row["status"]:
                if status not in ("open", "done"):
                    raise ValueError("status must be 'open' or 'done'")
                values.update(status=status, status_changed_at=stamp)
            if dismissed is not None:
                values["dismissed"] = int(dismissed)
            if values:
                self._update(db, "actions", action_id, values, stamp)
                self._web_changed(db, stamp)
        return self.item(action_id) if not dismissed else {}

    def source_line(self, doc_id: str, anchor: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT l.*, p.idx FROM source_lines l JOIN source_pages p "
                             "ON p.doc_id = l.doc_id AND p.page_id = l.page_id WHERE l.doc_id = ? AND l.anchor = ?",
                             (doc_id, anchor)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["bbox"] = json.loads(d["bbox"])
        d["rows"] = json.loads(d["rows"]) if d["rows"] else [d["bbox"]]
        return d

    def source_docs(self) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM source_docs ORDER BY folder, name")]

    def page_id(self, doc_id: str, index: int) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT page_id FROM source_pages WHERE doc_id = ? AND idx = ?", (doc_id, index)).fetchone()
        return row["page_id"] if row else None

    # ------------------------------------------------------------ the To-do document

    def todo_doc_id(self) -> str | None:
        return self.todo_meta().get("doc_id")

    def set_todo_doc_id(self, doc_id: str) -> None:
        with self.db() as db:
            db.execute("INSERT INTO todo_meta (key, value) VALUES ('doc_id', ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (doc_id,))

    def reset_todo(self) -> None:
        with self.db() as db:
            db.execute("DELETE FROM todo_slots")
            db.execute("DELETE FROM todo_meta WHERE key IN ('doc_id', 'fingerprint', 'paper_marker')")

    def todo_entries(self, include_others: bool) -> list[TodoEntry]:
        current = self.todo_doc_id()
        entries = []
        for i in self.items():
            if not include_others and i["owner"] == "someone_else":
                continue
            src = i["source"]
            # Handwriting is the item's text only while it is on the current document.
            on_paper = i["written"] and src["doc_id"] == current
            if on_paper:
                label = "written here"
            elif i["written"]:
                label = "written on an earlier To-do"
            elif i["origin"] == "web":
                label = "added in Jotted"
            else:
                folder = src["folder"].strip("/") or "Library"
                label = f"{folder} › {src['name']} · p{src['page']}"
                if i["owner"] == "someone_else":
                    label = "others · " + label
            entries.append(TodoEntry(item_id=i["id"], text=i["text"],
                                     done=i["status"] == "done", source_label=label, slot=i["slot"],
                                     handwritten=on_paper,
                                     ink=tuple(i["bbox"]) if on_paper and i.get("bbox") else None,
                                     edited=bool(i.get("edited"))))
        return entries

    def assign_slots(self, entries: list[TodoEntry], capacity: int, inked: set[int]) -> tuple[list[TodoEntry], int]:
        waiting = [e for e in entries if e.slot is None and not e.done]  # a done item never joins the document
        placed = [e for e in entries if e.slot is not None]
        taken = {e.slot for e in placed} | self.occupied_slots()
        free = [s for s in range(capacity) if s not in taken and s not in inked]
        # Short of rows: done items on rows without ink leave, and their rows are reused.
        releasable = [e for e in placed if e.done and e.slot not in inked]
        released = releasable[:max(0, len(waiting) - len(free))]
        with self.db() as db:
            for e in released:
                db.execute("DELETE FROM todo_slots WHERE slot = ?", (e.slot,))
                free.append(e.slot)
                e.slot = None
            free.sort()
            for e, slot in zip(waiting, free):
                db.execute("INSERT INTO todo_slots (slot, kind, item_id) VALUES (?, 'action', ?)", (slot, e.item_id))
                e.slot = slot
        return sorted((e for e in entries if e.slot is not None), key=lambda e: e.slot), max(0, len(waiting) - len(free))

    def occupied_slots(self) -> set[int]:
        with self.db() as db:
            return {r["slot"] for r in db.execute("SELECT slot FROM todo_slots")}

    def add_written(self, doc_id: str, items: list[WrittenItem]) -> int:
        """New items written by hand in empty rows of the To-do document: they keep that row,
        so the handwriting stays next to its checkbox."""
        if not items:
            return 0
        settings = self.settings()
        stamp = now()
        added = 0
        with self.db() as db:
            for w in items:
                if db.execute("SELECT 1 FROM todo_slots WHERE slot = ?", (w.slot,)).fetchone():
                    continue  # taken meanwhile
                text = clean_text(w.text)
                cur = db.execute(
                    """INSERT OR IGNORE INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor,
                         bbox, text, paper_text, owner, p_action, text_changed_at, status_changed_at, created_at,
                         updated_at, written) VALUES ('todo', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'me', 1.0, ?, ?, ?, ?, 1)""",
                    (doc_id, settings.todo_name, settings.todo_folder, w.page_id, w.page_index, w.anchor,
                     _bbox(w.bbox), text, text, stamp, stamp, stamp, stamp),
                )
                if not cur.rowcount:
                    continue
                # Its line, so the web app can show the handwriting like any collected action.
                db.execute("INSERT OR IGNORE INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, '')",
                           (doc_id, w.page_id, w.page_index))
                db.execute(
                    """INSERT OR REPLACE INTO source_lines (doc_id, page_id, anchor, key, text, bbox, rows, drawing,
                         p_action, owner) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1.0, 'me')""",
                    (doc_id, w.page_id, w.anchor, w.key, text, _bbox(w.bbox), json.dumps([list(w.bbox)])),
                )
                # A box that already had ink when the item was written isn't a tick.
                db.execute("INSERT INTO todo_slots (slot, kind, item_id, ticked) VALUES (?, 'action', ?, ?)",
                           (w.slot, cur.lastrowid, int(w.box_inked)))
                added += 1
        return added

    def apply_ticks(self, ticked_slots: set[int], marker: str) -> int:
        """A slot that gains ink marks its item done. Ink stays on paper, so each slot's tick
        counts once: re-opening the item on the web afterwards is not undone by the old tick."""
        stamp = now()
        changed = 0
        with self.db() as db:
            for slot in sorted(ticked_slots):
                row = db.execute("SELECT * FROM todo_slots WHERE slot = ? AND ticked = 0", (slot,)).fetchone()
                if row is None:
                    continue
                db.execute("UPDATE todo_slots SET ticked = 1 WHERE slot = ?", (slot,))
                db.execute("UPDATE actions SET status = 'done', status_changed_at = ?, updated_at = ? "
                           "WHERE id = ? AND status = 'open'", (stamp, stamp, row["item_id"]))
                changed += 1
            db.execute("INSERT INTO todo_meta (key, value) VALUES ('paper_marker', ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (marker,))
        return changed

    @staticmethod
    def _fingerprint(entries: list[TodoEntry]) -> str:
        data = json.dumps([(e.slot, e.text, e.done, e.source_label, e.handwritten, e.edited) for e in entries])
        return hashlib.sha256(data.encode()).hexdigest()

    def todo_needs_publish(self, entries: list[TodoEntry]) -> bool:
        with self.db() as db:
            row = db.execute("SELECT value FROM todo_meta WHERE key = 'fingerprint'").fetchone()
        return row is None or row["value"] != self._fingerprint(entries)

    def mark_todo_published(self, entries: list[TodoEntry]) -> None:
        with self.db() as db:
            for k, v in (("fingerprint", self._fingerprint(entries)), ("published_at", now())):
                db.execute("INSERT INTO todo_meta (key, value) VALUES (?, ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, v))

    def todo_meta(self) -> dict:
        with self.db() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT * FROM todo_meta")}
