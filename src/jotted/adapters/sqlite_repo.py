"""SQLite implementation of the core's Repository port.

Shares the database file with the Tasks-notebook store (`jotted.store`), so the
combined list and the To-do document can include those tasks too.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from ..core.model import DocInfo, Judgment, PageInfo, Settings, SourceLine, TodoEntry, WrittenItem
from ..store import clean_text, now, to_utc

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
    kind    TEXT NOT NULL,
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
                db.execute("ALTER TABLE actions ADD COLUMN written INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _has_tasks(self, db) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone() is not None

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
        """Collected actions plus the Tasks notebook's tasks, as one list for the web app."""
        out: list[dict] = []
        with self.db() as db:
            slots = {(r["kind"], r["item_id"]): r["slot"] for r in db.execute("SELECT * FROM todo_slots")}
            for r in db.execute("SELECT * FROM actions WHERE dismissed = 0 AND missing = 0 ORDER BY created_at, id"):
                out.append({
                    "kind": "action", "id": r["id"], "text": r["text"], "paper_text": r["paper_text"],
                    "written": bool(r["written"]), "bbox": json.loads(r["bbox"]) if r["bbox"] else None,
                    "status": r["status"], "owner": r["owner"], "p_action": round(r["p_action"], 2),
                    "source": {"doc_id": r["doc_id"], "name": r["doc_name"], "folder": r["folder"],
                               "page": r["page_index"], "anchor": r["anchor"]},
                    "edited": r["text"] != r["paper_text"], "slot": slots.get(("action", r["id"])),
                    "created_at": r["created_at"],
                })
            if self._has_tasks(db):
                nb = {n["id"]: n for n in db.execute("SELECT * FROM notebooks")}
                for r in db.execute("SELECT * FROM tasks WHERE missing = 0 ORDER BY created_at, id"):
                    book = nb.get(r["notebook_id"])
                    out.append({
                        "kind": "task", "id": r["id"], "text": r["text"], "paper_text": r["paper_text"],
                        "status": r["status"], "owner": "me", "p_action": None,
                        "source": {"doc_id": r["notebook_id"], "name": book["name"] if book else "Tasks",
                                   "folder": "", "page": r["page_index"], "anchor": r["anchor_id"],
                                   "origin": r["origin"]},
                        "edited": r["origin"] == "paper" and r["paper_text"] is not None and r["text"] != r["paper_text"],
                        "slot": slots.get(("task", r["id"])), "created_at": r["created_at"],
                    })
        if status:
            out = [i for i in out if i["status"] == status]
        if owner == "mine":
            out = [i for i in out if i["owner"] in ("me", "unclear")]
        elif owner == "others":
            out = [i for i in out if i["owner"] == "someone_else"]
        if folder:
            f = "/" + folder.strip("/")
            out = [i for i in out if i["kind"] == "action" and (i["source"]["folder"] + "/").startswith(f.rstrip("/") + "/")]
        return out

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
                db.execute("INSERT INTO todo_meta (key, value) VALUES ('web_changed_at', ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (stamp,))
        return next(i for i in self.items() if i["kind"] == "action" and i["id"] == action_id) if not dismissed else {}

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
            on_paper = bool(i.get("written")) and src["doc_id"] == current
            if on_paper:
                label = "written here"
            elif i["kind"] == "action" and i.get("written"):
                label = "written on an earlier To-do"
            elif i["kind"] == "action":
                folder = src["folder"].strip("/") or "Library"
                label = f"{folder} › {src['name']} · p{src['page']}"
                if i["owner"] == "someone_else":
                    label = "others · " + label
            else:
                label = f"{src['name']}" + (f" · p{src['page']}" if src.get("page") else "")
            entries.append(TodoEntry(kind=i["kind"], item_id=i["id"], text=i["text"],
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
                db.execute("INSERT INTO todo_slots (slot, kind, item_id) VALUES (?, ?, ?)", (slot, e.kind, e.item_id))
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
                if row["kind"] == "action":
                    db.execute("UPDATE actions SET status = 'done', status_changed_at = ?, updated_at = ? "
                               "WHERE id = ? AND status = 'open'", (stamp, stamp, row["item_id"]))
                elif self._has_tasks(db):
                    task = db.execute("SELECT notebook_id, status FROM tasks WHERE id = ?", (row["item_id"],)).fetchone()
                    if task and task["status"] == "open":
                        db.execute("UPDATE tasks SET status = 'done', status_changed_at = ?, updated_at = ? WHERE id = ?",
                                   (stamp, stamp, row["item_id"]))
                        # the Tasks notebook needs reprinting (its strike-through)
                        db.execute("UPDATE notebooks SET web_changed_at = ? WHERE id = ?", (stamp, task["notebook_id"]))
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




