"""The common to-do list: the core with fake adapters, the SQLite repository, the To-do
document layout and tick reading, and the web endpoints."""

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

from rmtasks import config, template  # noqa: E402
from rmtasks.adapters import todo_document  # noqa: E402
from rmtasks.adapters.sqlite_repo import SqliteRepository  # noqa: E402
from rmtasks.core import service  # noqa: E402
from rmtasks.core.model import (  # noqa: E402
    DocInfo, Judgment, PageInfo, PaperRead, Settings, SourceLine, TodoEntry, WrittenItem,
)
from rmtasks.store import Store  # noqa: E402
from rmtasks.strokes import make_stroke  # noqa: E402

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- fakes


class FakeSource:
    name = "fake"

    def __init__(self):
        self.docs: dict[str, DocInfo] = {}
        self.pages_: dict[str, list[PageInfo]] = {}
        self.lines_: dict[tuple[str, str], list[SourceLine]] = {}
        self.reads: list[tuple[str, str]] = []

    def add(self, doc_id, name, folder, modified, pages):
        """pages: {page_id: [(anchor, key, text), ...]}; a key "k1+k2" is a line of strokes k1 and k2."""
        self.docs[doc_id] = DocInfo("fake", doc_id, name, folder, modified)
        self.pages_[doc_id] = []
        for i, (pid, lines) in enumerate(pages.items(), start=1):
            digest = str(hash(tuple(lines)))
            self.pages_[doc_id].append(PageInfo(doc_id, pid, i, digest))
            self.lines_[(doc_id, pid)] = [
                SourceLine(anchor=a, key=k, text=t, bbox=(0, 100 * n, 500, 100 * n + 40), strokes=tuple(k.split("+")))
                for n, (a, k, t) in enumerate(lines)
            ]

    def list_documents(self):
        return list(self.docs.values())

    def folders(self):
        return sorted({d.folder for d in self.docs.values()})

    def pages(self, doc):
        return self.pages_[doc.id]

    def read_page(self, doc, page):
        self.reads.append((doc.id, page.id))
        return self.lines_[(doc.id, page.id)]

    def stroke_ids(self, doc, page):
        return {s for ln in self.lines_[(doc.id, page.id)] for s in ln.strokes}


class FakeJudge:
    """Lines containing 'TODO' are actions; '@name' makes them someone else's."""

    def __init__(self):
        self.judged: list[str] = []

    def judge(self, doc, page, lines, new):
        out = {}
        for ln in new:
            self.judged.append(ln.text)
            is_action = "TODO" in ln.text
            out[ln.anchor] = Judgment(p_action=0.95 if is_action else 0.1,
                                      owner="someone_else" if "@" in ln.text else "me")
        return out


class FakePublisher:
    def __init__(self, capacity=10):
        self.cap = capacity
        self.published: list[list[TodoEntry]] = []
        self.ticks: tuple[set[int], str] | None = None
        self.written: list[WrittenItem] = []
        self.occupied_seen: set[int] | None = None

    def capacity(self):
        return self.cap

    def publish(self, entries):
        self.published.append([dataclasses.replace(e) for e in entries])

    def read_paper(self, occupied):
        self.occupied_seen = set(occupied)
        if self.ticks is None:
            return None
        slots, marker = self.ticks
        return PaperRead(doc_id="todo-doc", marker=marker, ticks=set(slots),
                         written=[w for w in self.written if w.slot not in occupied])


@pytest.fixture
def repo(tmp_path):
    r = SqliteRepository(tmp_path / "db.sqlite")
    r.save_settings(Settings(watch=["/Meetings"], action_threshold=0.7))
    return r


def notes(source, modified="2026-10-01T10:00:00Z", extra=None):
    page1 = [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k20", "TODO book the retro room"),
             ("1:30", "k30", "@Simon TODO send the deck")]
    if extra:
        page1 = page1 + extra
    source.add("doc-a", "Weekly sync", "/Meetings", modified, {"p1": page1, "p2": [("1:40", "k40", "Notes only")]})
    source.add("doc-b", "Diary", "/Personal", modified, {"p1": [("1:50", "k50", "TODO private thing")]})


# ---------------------------------------------------------------- core: collect


def test_settings_watch_matching():
    s = Settings(watch=["/Meetings", "/Work/Notes/Standup"])
    doc = lambda folder, name="x": DocInfo("fake", "1", name, folder, "m")  # noqa: E731
    assert s.watches(doc("/Meetings")) and s.watches(doc("/Meetings/2026"))
    assert not s.watches(doc("/Meetingsx")) and not s.watches(doc("/Personal"))
    assert s.watches(doc("/Work/Notes", "Standup"))  # a single document
    assert Settings(watch=["/"]).watches(doc("/anything"))


def test_collect_reads_only_watched_documents_and_creates_actions(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    summary = service.collect(source, judge, repo)
    assert summary.docs_seen == 1 and summary.docs_changed == 1 and summary.pages_read == 2
    assert summary.actions_new == 2
    items = {i["text"]: i for i in repo.items() if i["kind"] == "action"}
    assert set(items) == {"TODO book the retro room", "@Simon TODO send the deck"}
    assert items["@Simon TODO send the deck"]["owner"] == "someone_else"
    assert items["TODO book the retro room"]["source"]["folder"] == "/Meetings"
    assert "TODO private thing" not in judge.judged  # an unwatched folder is never read


def test_collect_is_incremental_at_every_level(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    first_judged = len(judge.judged)

    # Nothing changed: the document is skipped before its pages are even listed.
    source.reads.clear()
    summary = service.collect(source, judge, repo)
    assert summary.docs_changed == 0 and not source.reads and len(judge.judged) == first_judged

    # One line added on page 1: page 2 is skipped by hash, and only the new line is judged.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    source.reads.clear()
    summary = service.collect(source, judge, repo)
    assert source.reads == [("doc-a", "p1")]
    assert summary.pages_skipped == 1 and summary.lines_judged == 1
    assert judge.judged[first_judged:] == ["TODO renew the licence"]


def test_erased_line_marks_its_action_missing(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-01T12:00:00Z",
               {"p1": [("1:10", "k10", "Agenda for the weekly"), ("1:30", "k30", "@Simon TODO send the deck")],
                "p2": [("1:40", "k40", "Notes only")]})
    summary = service.collect(source, judge, repo)
    assert summary.actions_missing == 1
    assert [i["text"] for i in repo.items() if i["kind"] == "action"] == ["@Simon TODO send the deck"]


def test_failed_document_is_retried_next_run(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)

    def boom(doc, page):
        raise RuntimeError("download failed")

    source.read_page = boom
    summary = service.collect(source, judge, repo)
    assert summary.errors and repo.doc_marker("fake", "doc-a") is None  # not marked as collected



# ---------------------------------------------------------------- core: "new writing only"


def from_now(repo, *doc_ids):
    s = repo.settings()
    s.from_now = sorted(set(s.from_now) | set(doc_ids))
    repo.save_settings(s)


def open_actions(repo):
    return sorted(i["text"] for i in repo.items() if i["kind"] == "action")


def test_from_now_records_existing_writing_without_reading_it(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    summary = service.collect(source, judge, repo)
    assert summary.pages_baselined == 2 and summary.pages_read == 0
    assert source.reads == [] and judge.judged == [] and open_actions(repo) == []
    assert repo.doc_marker("fake", "doc-a") == "2026-10-01T10:00:00Z"  # done: next run skips it
    assert service.collect(source, judge, repo).docs_changed == 0


def test_from_now_reads_appended_pages_in_full(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    service.collect(source, judge, repo)
    page1 = [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k20", "TODO book the retro room"),
             ("1:30", "k30", "@Simon TODO send the deck")]
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-02T10:00:00Z",
               {"p1": page1, "p2": [("1:40", "k40", "Notes only")],
                "p3": [("1:60", "k60", "Decided on Friday"), ("1:70", "k70", "TODO email the agenda")]})
    summary = service.collect(source, judge, repo)
    assert source.reads == [("doc-a", "p3")] and summary.pages_baselined == 0
    assert judge.judged == ["Decided on Friday", "TODO email the agenda"]
    assert open_actions(repo) == ["TODO email the agenda"]


def test_from_now_judges_only_new_ink_on_an_old_page(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    service.collect(source, judge, repo)
    # A new line under the old ones, and a tick added to an old line (its strokes grow).
    notes(source, modified="2026-10-02T10:00:00Z", extra=[("1:35", "k35", "TODO order the cake")])
    service.collect(source, judge, repo)
    assert source.reads == [("doc-a", "p1")]
    assert judge.judged == ["TODO order the cake"]  # the old lines go along as context only
    assert open_actions(repo) == ["TODO order the cake"]

    page1 = [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k20+k21", "TODO book the retro room ✓"),
             ("1:30", "k30", "@Simon TODO send the deck"), ("1:35", "k35", "TODO order the cake")]
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-03T10:00:00Z",
               {"p1": page1, "p2": [("1:40", "k40", "Notes only")]})
    service.collect(source, judge, repo)
    assert judge.judged[-1] == "TODO book the retro room ✓"  # an old line with new ink is new writing


def test_from_now_on_a_document_already_read_changes_nothing(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    from_now(repo, "doc-a")
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-02T10:00:00Z",
               {"p1": [("1:20", "k20", "TODO book the retro room")], "p2": [("1:40", "k40", "Notes only")],
                "p3": [("1:70", "k70", "TODO email the agenda")]})
    summary = service.collect(source, judge, repo)
    assert summary.pages_baselined == 0 and ("doc-a", "p3") in source.reads
    assert "TODO email the agenda" in open_actions(repo)


def test_from_now_chosen_mid_read_skips_the_remaining_pages(repo):
    source = FakeSource()
    notes(source)

    class MarkingJudge(FakeJudge):
        def judge(self, doc, page, lines, new):  # the user ticks "new writing only" while page 1 is read
            from_now(repo, "doc-a")
            return super().judge(doc, page, lines, new)

    summary = service.collect(source, MarkingJudge(), repo)
    assert summary.pages_read == 1 and summary.pages_baselined == 1
    assert source.reads == [("doc-a", "p1")]
    assert open_actions(repo) == ["@Simon TODO send the deck", "TODO book the retro room"]


def test_turning_from_now_off_reads_the_skipped_writing(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    service.collect(source, judge, repo)
    notes(source, modified="2026-10-02T10:00:00Z", extra=[("1:35", "k35", "TODO order the cake")])
    service.collect(source, judge, repo)
    judge.judged.clear()

    s = repo.settings()
    s.from_now = []
    repo.save_settings(s)
    summary = service.collect(source, judge, repo)
    assert summary.pages_read == 2 and repo.baselined_docs() == set()
    assert sorted(judge.judged) == ["@Simon TODO send the deck", "Agenda for the weekly", "Notes only",
                                    "TODO book the retro room"]  # the cake was judged already
    assert open_actions(repo) == ["@Simon TODO send the deck", "TODO book the retro room", "TODO order the cake"]


def test_pending_leaves_out_pages_that_will_only_be_recorded(repo):
    source = FakeSource()
    notes(source)
    from_now(repo, "doc-a")
    assert [(d.id, pages) for d, pages in service.pending(source, repo, fetch=True)] == [("doc-a", [])]

def test_web_edits_win_over_older_paper_changes(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    action = next(i for i in repo.items() if i["text"] == "TODO book the retro room")
    repo.edit_action(action["id"], text="Book the big retro room", status="done")
    # The paper line is rewritten in an older version of the document: the web edit stays.
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-01T09:00:00Z",
               {"p1": [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k21", "TODO book a retro room"),
                       ("1:30", "k30", "@Simon TODO send the deck")], "p2": [("1:40", "k40", "Notes only")]})
    service.collect(source, judge, repo)
    item = next(i for i in repo.items() if i["id"] == action["id"] and i["kind"] == "action")
    assert item["text"] == "Book the big retro room" and item["status"] == "done"
    assert item["paper_text"] == "TODO book a retro room"


def test_dismissed_actions_leave_the_list(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    action = next(i for i in repo.items() if i["kind"] == "action")
    repo.edit_action(action["id"], dismissed=True)
    assert action["id"] not in [i["id"] for i in repo.items() if i["kind"] == "action"]


def test_filters(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    assert [i["text"] for i in repo.items(owner="others")] == ["@Simon TODO send the deck"]
    assert [i["text"] for i in repo.items(owner="mine")] == ["TODO book the retro room"]
    assert len(repo.items(folder="/Meetings")) == 2 and repo.items(folder="/Personal") == []


# ---------------------------------------------------------------- core: the To-do document


def test_slots_are_permanent_and_ticks_mark_items_done(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    r = service.sync_todo(repo, pub)
    assert r["published"] and [e.slot for e in pub.published[-1]] == [0, 1]
    first = pub.published[-1][0]

    # Nothing changed: no republish.
    pub.ticks = (set(), "m1")
    assert not service.sync_todo(repo, pub)["published"]

    # A new action takes the next slot; existing ones keep theirs.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)
    assert [(e.text, e.slot) for e in pub.published[-1]][0] == (first.text, 0)
    assert pub.published[-1][-1].slot == 2

    # A tick on slot 0 marks that item done, once.
    pub.ticks = ({0}, "m2")
    r = service.sync_todo(repo, pub)
    assert r["ticked"] == 1 and pub.published[-1][0].done
    item = next(i for i in repo.items() if i["kind"] == "action" and i["text"] == first.text)
    repo.edit_action(item["id"], status="open")  # re-opened on the web
    assert service.sync_todo(repo, pub)["ticked"] == 0  # the old ink does not tick it again
    assert not next(e for e in pub.published[-1] if e.slot == 0).done


def test_include_others_setting(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    s = repo.settings()
    s.tablet_include_others = False
    repo.save_settings(s)
    service.sync_todo(repo, pub)
    assert [e.text for e in pub.published[-1]] == ["TODO book the retro room"]


def test_tasks_notebook_items_join_the_list_and_ticks_reach_them(tmp_path, repo):
    store = Store(repo.path)  # same database file
    with store.db() as db:
        db.execute("INSERT INTO notebooks (id, name, file_type) VALUES ('nb', 'Tasks', 'pdf')")
    task_id = store.add_web_task("nb", "Call the plumber")
    pub = FakePublisher()
    service.sync_todo(repo, pub)
    entry = next(e for e in pub.published[-1] if e.kind == "task")
    assert entry.text == "Call the plumber" and entry.source_label.startswith("Tasks")
    pub.ticks = ({entry.slot}, "m")
    assert service.sync_todo(repo, pub)["ticked"] == 1
    assert store.task(task_id)["status"] == "done"
    assert store.state("nb")["pending_push"]  # the Tasks notebook will be reprinted


# ---------------------------------------------------------------- the To-do PDF


def test_todo_pdf_has_fixed_page_count(tmp_path):
    entries = [TodoEntry("action", i, f"item {i}", i % 2 == 0, "Meetings › Weekly · p1", i) for i in range(45)]
    pdf = todo_document.build_pdf(tmp_path / "To-do.pdf", entries, pages=todo_document.PAGES)
    import re

    assert len(re.findall(rb"/Type /Page[^s]", pdf.read_bytes())) == todo_document.PAGES


def test_tick_on_a_checkbox_is_read_back():
    scale = config.TemplateConfig().scale
    x0, y0, x1, y1 = todo_document.slot_box(3)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2  # pt from the top-left

    def to_tablet(x_pt, y_pt):
        return ((x_pt * template.UNITS_PER_PT) - template.RM_W / 2) * scale, y_pt * template.UNITS_PER_PT * scale

    a, b = to_tablet(cx - 4, cy), to_tablet(cx + 5, cy + 4)
    tick = make_stroke("1:1", "fineliner", [a, to_tablet(cx, cy + 3), b])
    far = make_stroke("1:2", "fineliner", [to_tablet(200, cy), to_tablet(260, cy)])  # in the text, not the box
    assert todo_document.ticked_rows([tick, far], scale) == {3}


# ---------------------------------------------------------------- web endpoints


def test_todo_endpoints(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/rmtasks.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    cfg = config.load()

    from rmtasks.app import App
    from rmtasks.server import create_app

    store = Store(cfg.server.db)
    repo = SqliteRepository(cfg.server.db)
    repo.save_settings(Settings(watch=["/Meetings"]))
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)

    class PageSource(FakeSource):
        def page_strokes(self, doc_id, page_id):
            return [make_stroke("1:20", "fineliner", [(0, 100), (400, 140)])]

    with repo.db() as db:  # line boxes for the source images
        db.execute("UPDATE source_lines SET bbox = '[0, 100, 400, 140]'")
    app = App(cfg=cfg, store=store, repo=repo, source=PageSource(), judge=judge)
    client = create_app(cfg, app_=app, background=False).test_client()

    data = client.get("/api/todo").get_json()
    assert {i["text"] for i in data["items"]} == {"TODO book the retro room", "@Simon TODO send the deck"}
    assert client.get("/api/todo?owner=others").get_json()["items"][0]["owner"] == "someone_else"

    action = next(i for i in data["items"] if i["owner"] == "me")
    r = client.patch(f"/api/items/action/{action['id']}", json={"status": "done"}).get_json()
    assert next(i for i in r["items"] if i["id"] == action["id"])["status"] == "done"
    assert client.patch("/api/items/nope/1", json={}).status_code == 404

    line = client.get(f"/api/sources/doc-a/line/{action['source']['anchor']}.svg")
    assert line.status_code == 200 and line.data.startswith(b"<svg")
    page = client.get(f"/api/sources/doc-a/1/preview.svg?anchor={action['source']['anchor']}")
    assert page.status_code == 200 and b"#ffe066" in page.data  # the highlight

    assert client.put("/api/settings", json={"action_threshold": 2}).status_code == 400
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 400
    s = client.put("/api/settings", json={"watch": ["Meetings/", "/Work"], "todo_enabled": True}).get_json()
    assert s["watch"] == ["/Meetings", "/Work"] and s["todo_enabled"]
    assert client.put("/api/settings", json={"from_now": "doc-a"}).status_code == 400
    assert client.put("/api/settings", json={"from_now": ["doc-b", "doc-a", "doc-a"]}).get_json()["from_now"] == \
        ["doc-a", "doc-b"]


# ---------------------------------------------------------------- writing on the To-do document


def written(slot, text, box_inked=False):
    return WrittenItem(slot=slot, page_id="tp1", page_index=1, text=text, anchor=f"9:{slot}", key=f"k{slot}",
                       bbox=(-500, 1000, -100, 1060), box_inked=box_inked)


def test_writing_in_an_empty_row_adds_an_item_in_that_row(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # slots 0 and 1
    pub.ticks = (set(), "m1")
    pub.written = [written(2, "- Book flights to Sydney")]
    r = service.sync_todo(repo, pub)
    assert r["written"] == 1 and r["published"]
    entry = next(e for e in pub.published[-1] if e.slot == 2)
    assert entry.handwritten and entry.text == "Book flights to Sydney"  # bullet stripped
    assert entry.ink == (-500, 1000, -100, 1060)
    item = next(i for i in repo.items() if i["text"] == "Book flights to Sydney")
    assert item["owner"] == "me" and item["written"] and item["source"]["name"] == "To-do"
    assert repo.source_line("todo-doc", "9:2") is not None  # its handwriting image works

    # Read again: the row is now occupied, so nothing is added twice.
    assert service.sync_todo(repo, pub)["written"] == 0 and 2 in pub.occupied_seen

    # A newly collected action takes the next free slot, never the written one.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)
    assert next(e for e in pub.published[-1] if e.text == "TODO renew the licence").slot == 3


def test_a_box_drawn_with_a_new_item_is_not_a_tick(repo):
    pub = FakePublisher()
    pub.ticks = ({0}, "m1")
    pub.written = [written(0, "Call the bank", box_inked=True)]
    r = service.sync_todo(repo, pub)
    assert r["written"] == 1 and r["ticked"] == 0
    item = next(i for i in repo.items() if i["text"] == "Call the bank")
    assert item["status"] == "open"


def test_done_and_edited_handwritten_items(tmp_path, repo):
    pub = FakePublisher()
    pub.ticks = (set(), "m1")
    pub.written = [written(0, "Call the bank")]
    service.sync_todo(repo, pub)
    item = next(i for i in repo.items() if i["text"] == "Call the bank")
    repo.edit_action(item["id"], text="Call the bank about the card", status="done")
    service.sync_todo(repo, pub)
    e = pub.published[-1][0]
    assert e.handwritten and e.done and e.edited and e.text == "Call the bank about the card"
    pdf = todo_document.build_pdf(tmp_path / "t.pdf", pub.published[-1], scale=1.0525)
    assert pdf.read_bytes().startswith(b"%PDF")


def test_written_rows_groups_handwriting_by_row():
    scale = config.TemplateConfig().scale
    lines_cfg = config.LinesConfig()

    def tablet(x_pt, y_pt):
        return ((x_pt * template.UNITS_PER_PT) - template.RM_W / 2) * scale, y_pt * template.UNITS_PER_PT * scale

    top = todo_document.TOP + 4 * todo_document.ROW  # row 4
    letters = [make_stroke(f"1:{i}", "fineliner", [tablet(60 + 18 * i, top + 16), tablet(68 + 18 * i, top + 6),
                                                   tablet(74 + 18 * i, top + 17)]) for i in range(5)]
    descender = make_stroke("1:9", "fineliner", [tablet(80, top + 10), tablet(80, top + 30)])  # crosses into row 5
    box_tick = make_stroke("1:20", "fineliner", [tablet(30, top + 9), tablet(34, top + 13), tablet(38, top + 6)])
    rows = todo_document.written_rows(letters + [descender, box_tick], scale, lines_cfg)
    assert list(rows) == [4] and len(rows[4]) == 6  # the tick in the box is not writing


def test_a_deleted_todo_document_starts_again_from_the_top(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    pub.ticks = (set(), "m1")
    pub.written = [written(5, "Call the bank")]
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # written item in row 5; collected ones after it
    assert [e.slot for e in pub.published[-1]] == [5, 6, 7]
    assert pub.published[-1][0].handwritten

    # Deleted on the tablet: the next document is printed from row 0, handwriting as text.
    pub.ticks, pub.written = None, []
    r = service.sync_todo(repo, pub)
    assert r["published"] and [e.slot for e in pub.published[-1]] == [0, 1, 2]
    bank = next(e for e in pub.published[-1] if e.text == "Call the bank")
    assert not bank.handwritten and bank.ink is None and bank.source_label == "written on an earlier To-do"

    # The new document is read as ours from then on: a tick on row 0 counts.
    pub.ticks = ({0}, "m2")
    assert service.sync_todo(repo, pub)["ticked"] == 1


def test_a_replaced_todo_document_is_not_read_with_the_old_layout(repo):
    pub = FakePublisher()
    pub.ticks = (set(), "m1")
    pub.written = [written(3, "Call the bank")]
    service.sync_todo(repo, pub)
    assert repo.todo_doc_id() == "todo-doc"
    repo.set_todo_doc_id("older-doc")  # as if the document found now isn't the one we laid out
    pub.ticks = ({3}, "m2")
    r = service.sync_todo(repo, pub)
    assert r["ticked"] == 0 and r["published"] and pub.published[-1][0].slot == 0
