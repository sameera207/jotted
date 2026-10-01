"""The core's ports. Adapters implement these; the core only ever sees these types."""

from __future__ import annotations

from typing import Protocol

from .model import CollectSummary, DocInfo, Judgment, PageInfo, PaperRead, Settings, SourceLine, TodoEntry, WrittenItem


class DocumentSource(Protocol):
    """Where handwriting or text comes from (the reMarkable library first)."""

    name: str

    def list_documents(self) -> list[DocInfo]:
        """Every document the source can offer, with change markers. Cheap: no content."""

    def folders(self) -> list[str]:
        """Folder paths, for the settings picker."""

    def pages(self, doc: DocInfo) -> list[PageInfo]:
        """The document's pages with content hashes (fetches the document if needed)."""

    def read_page(self, doc: DocInfo, page: PageInfo) -> list[SourceLine]:
        """The page as lines. May call recognition; unchanged lines should come from a cache."""

    def stroke_ids(self, doc: DocInfo, page: PageInfo) -> set[str]:
        """IDs of every mark on the page. Cheap: no recognition."""


class ActionJudge(Protocol):
    def judge(self, doc: DocInfo, page: PageInfo, lines: list[SourceLine],
              new: list[SourceLine]) -> dict[str, Judgment]:
        """Judgments for `new` (keyed by anchor), with every line of the page as context."""


class Repository(Protocol):
    def settings(self) -> Settings: ...
    def save_settings(self, settings: Settings) -> None: ...

    def doc_marker(self, source: str, doc_id: str) -> str | None:
        """The change marker stored for a document after it was last fully collected."""

    def save_doc(self, doc: DocInfo, page_count: int) -> None: ...

    def page_hash(self, doc_id: str, page_id: str) -> str | None: ...

    def save_baseline(self, doc: DocInfo, page: PageInfo, strokes: set[str]) -> None:
        """Record a page's existing marks without reading it; they are never judged."""

    def baseline_strokes(self, doc_id: str, page_id: str) -> set[str] | None:
        """The marks recorded as the page's baseline; None if it has none."""

    def baselined_docs(self) -> set[str]:
        """Documents with at least one baseline page."""

    def clear_baseline(self, doc_id: str) -> None:
        """Forget a document's baseline so its earlier writing is read on the next collect."""

    def line_keys(self, doc_id: str, page_id: str) -> dict[str, str]:
        """anchor -> key of every line stored for the page."""

    def save_page(self, doc: DocInfo, page: PageInfo, lines: list[SourceLine],
                  judgments: dict[str, Judgment], threshold: float) -> tuple[int, int, int]:
        """Store the page's lines and turn judged lines into action items.
        Returns (new actions, updated actions, actions now missing)."""

    def todo_entries(self, include_others: bool) -> list[TodoEntry]:
        """Everything that belongs on the To-do document, slot assigned or not."""

    def assign_slots(self, entries: list[TodoEntry]) -> list[TodoEntry]:
        """Give unslotted entries the next free slots, permanently."""

    def occupied_slots(self) -> set[int]:
        """Slots already holding an item."""

    def add_written(self, doc_id: str, items: list[WrittenItem]) -> int:
        """Create items for rows written by hand on the To-do document, in those rows."""

    def apply_ticks(self, ticked_slots: set[int], marker: str) -> int:
        """Mark items done whose slot gained a tick on paper. Returns how many changed."""

    def todo_needs_publish(self, entries: list[TodoEntry]) -> bool:
        """Whether these entries differ from what was last published."""

    def mark_todo_published(self, entries: list[TodoEntry]) -> None: ...


class TodoPublisher(Protocol):
    """The list where you can tick it: a document on the tablet."""

    def capacity(self) -> int: ...

    def publish(self, entries: list[TodoEntry]) -> None: ...

    def read_paper(self, occupied: set[int]) -> PaperRead | None:
        """Ticks in checkboxes, and new items written in rows outside `occupied`. None if the
        document doesn't exist yet."""


class Progress(Protocol):
    def __call__(self, message: str) -> None: ...


__all__ = ["ActionJudge", "CollectSummary", "DocumentSource", "Progress", "Repository", "TodoPublisher"]
