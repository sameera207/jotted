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
from rmtasks.core.model import DocInfo, Judgment, PageInfo, Settings, SourceLine, TodoEntry  # noqa: E402
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
        """pages: {page_id: [(anchor, key, text), ...]}"""
        self.docs[doc_id] = DocInfo("fake", doc_id, name, folder, modified)
        self.pages_[doc_id] = []
        for i, (pid, lines) in enumerate(pages.items(), start=1):
            digest = str(hash(tuple(lines)))
            self.pages_[doc_id].append(PageInfo(doc_id, pid, i, digest))
            self.lines_[(doc_id, pid)] = [
                SourceLine(anchor=a, key=k, text=t, bbox=(0, 100 * n, 500, 100 * n + 40))
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

    def capacity(self):
        return self.cap

    def publish(self, entries):
        self.published.append([dataclasses.replace(e) for e in entries])

    def read_ticks(self):
        return self.ticks


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
    text = (ROOT / "config.example.toml").read_text().replace('db   = "./data/rmtasks.db"', f'db = "{tmp_path}/db.sqlite"')
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
