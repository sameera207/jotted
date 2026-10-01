"""Domain types for the common to-do list."""

from __future__ import annotations

from dataclasses import dataclass, field

BBox = tuple[float, float, float, float]

OWNERS = ("me", "someone_else", "unclear")


@dataclass(frozen=True)
class DocInfo:
    """A document in a source, with a marker that changes whenever its content does."""
    source: str  # adapter name, e.g. "remarkable"
    id: str
    name: str
    folder: str  # "/Meeting notes"
    modified: str  # opaque change marker (a timestamp for reMarkable)

    @property
    def path(self) -> str:
        return (self.folder.rstrip("/") + "/" + self.name) if self.folder != "/" else "/" + self.name


@dataclass(frozen=True)
class PageInfo:
    doc_id: str
    id: str
    index: int  # 1-based
    content_hash: str  # changes whenever the page's content does


@dataclass
class SourceLine:
    """One line read from a page. `anchor` stays the same while the line exists; `key` changes
    whenever its content does (for handwriting: a hash of its stroke IDs)."""
    anchor: str
    key: str
    text: str
    bbox: BBox
    rows: list[BBox] = field(default_factory=list)
    drawing: bool = False
    checkbox: str = "none"


@dataclass
class Judgment:
    p_action: float
    owner: str  # one of OWNERS
    owner_probs: dict[str, float] = field(default_factory=dict)


@dataclass
class Settings:
    """What to collect and how; edited in the web UI, stored by the repository."""
    watch: list[str] = field(default_factory=list)  # folder paths (recursive) or document paths
    action_threshold: float = 0.7
    poll_interval_s: int = 60
    tablet_include_others: bool = True
    todo_enabled: bool = False  # create and keep the To-do document on the tablet
    todo_name: str = "To-do"
    todo_folder: str = "/"

    def watches(self, doc: DocInfo) -> bool:
        for w in self.watch:
            w = "/" + w.strip("/") if w.strip("/") else "/"
            if doc.path == w or w == "/" or doc.path.startswith(w + "/"):
                return True
        return False


@dataclass
class CollectSummary:
    docs_seen: int = 0
    docs_changed: int = 0
    pages_read: int = 0
    pages_skipped: int = 0
    lines_judged: int = 0
    actions_new: int = 0
    actions_updated: int = 0
    actions_missing: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TodoEntry:
    """One row of the To-do document: an item and where it is printed."""
    kind: str  # "action" (collected) or "task" (the Tasks notebook)
    item_id: int
    text: str
    done: bool
    source_label: str
    slot: int | None  # 0-based slot on the document; None until first printed
