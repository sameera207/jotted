"""Domain types for the common to-do list."""

from __future__ import annotations

from dataclasses import dataclass, field

BBox = tuple[float, float, float, float]

OWNERS = ("me", "someone_else", "unclear")


@dataclass(frozen=True)
class DocInfo:
    """A document in a source, with a marker that changes whenever its content does."""
    source: str  # the source plugin's name, e.g. "remarkable"
    id: str
    name: str
    folder: str  # "/Meeting notes"
    modified: str  # opaque change marker (the source's own, e.g. a cloud timestamp)

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
    marks: tuple[str, ...] = ()  # IDs of the marks (pen strokes, say) the line is made of


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
    include_others: bool = True  # the To-do document lists other people's actions too
    todo_enabled: bool = False  # create and keep the To-do document on the device
    todo_name: str = "To-do"
    todo_folder: str = "/"
    # Document IDs where only writing added from now on is read: what is already on their
    # pages when they are first collected is recorded as a baseline and never judged.
    from_now: list[str] = field(default_factory=list)

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
    pages_baselined: int = 0
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
    item_id: int
    text: str
    done: bool
    source_label: str
    slot: int | None  # 0-based slot on the document; None until first printed
    handwritten: bool = False  # written by hand in its row: print only the checkbox, not the text
    ink: BBox | None = None  # where that handwriting is (source units), for the done strike-through
    edited: bool = False  # text changed on the web since it was written


@dataclass
class WrittenItem:
    """A new item written by hand in an empty row of the To-do document."""
    slot: int
    page_id: str
    page_index: int
    text: str
    anchor: str
    key: str
    bbox: BBox  # source units
    box_inked: bool  # its checkbox already had ink when first read (not counted as a tick)


@dataclass
class PaperRead:
    """What the To-do document says on paper."""
    doc_id: str
    marker: str
    ticks: set[int] = field(default_factory=set)
    written: list[WrittenItem] = field(default_factory=list)
    inked: set[int] = field(default_factory=set)  # slots with any ink: never given to a new item
    capacity: int | None = None  # slots the document has; None until the device has laid out its pages
