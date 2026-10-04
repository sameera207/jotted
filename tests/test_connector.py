"""The Claude connector (specs/Claude-connector-spec.md): items with a source, added once;
proposals that wait for the person; `jotted claude connect`; the agent's MCP tools."""

import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from jotted import api, schema  # noqa: E402
from jotted.adapters.sqlite_repo import SCHEMA, SqliteRepository  # noqa: E402
from jotted.integrations import claude_desktop  # noqa: E402
from test_contract import cfg, conforms, data, error, rpc, run  # noqa: E402, F401

GDOC = ["--source-kind", "gdoc", "--source-key", "doc1#h.x7q", "--source-title", "Platform sync, 29 Sep",
        "--source-url", "https://docs.google.com/document/d/doc1", "--excerpt", "Sam to send the estimate to Dana"]


def items(capsys, *argv) -> list[dict]:
    return data(capsys, "items", *argv)


def ids(found: list[dict]) -> list[int]:
    return [i["id"] for i in found]


# ---------------------------------------------------------------- added once per source line


def test_a_source_key_adds_an_item_once(cfg, capsys):
    first = data(capsys, "items", "add", "Send", "the", "estimate", *GDOC)
    conforms(first, schema.DATA["items add"])
    assert first["created"] and first["origin"] == "agent" and first["status"] == "open"
    assert first["source"]["kind"] == "gdoc" and first["source"]["excerpt"] == "Sam to send the estimate to Dana"
    assert first["page"] is None
    again = data(capsys, "items", "add", "Send", "the", "revised", "estimate", *GDOC)
    assert not again["created"] and again["id"] == first["id"] and again["text"] == "Send the estimate"
    assert ids(items(capsys, "--status", "any")) == [first["id"]]
    assert ids(items(capsys, "--source-key", "doc1#h.x7q")) == [first["id"]]


def test_a_dismissed_source_line_is_never_added_again(cfg, capsys):
    item = data(capsys, "items", "add", "Send", "the", "estimate", *GDOC)
    data(capsys, "items", "dismiss", str(item["id"]))
    again = data(capsys, "items", "add", "Send", "the", "estimate", *GDOC)
    assert not again["created"] and again["status"] == "dismissed" and again["id"] == item["id"]
    assert items(capsys) == [] and ids(items(capsys, "--status", "dismissed")) == [item["id"]]
    code, err = error(capsys, "items", "edit", str(item["id"]), "Something", "else")
    assert err["code"] == "conflict"


def test_a_batch_reports_each_item_and_adds_only_the_new_valid_ones(cfg, capsys, monkeypatch):
    kept = data(capsys, "items", "add", "Already", "here", "--source-kind", "jira", "--source-key", "PLAT-1")
    gone = data(capsys, "items", "add", "Turned", "down", "--source-kind", "jira", "--source-key", "PLAT-2")
    data(capsys, "items", "dismiss", str(gone["id"]))
    batch = [
        {"text": "Book the room", "source": {"kind": "gcal", "key": "evt-9"}},
        {"text": "Already here, reworded", "source": {"kind": "jira", "key": "PLAT-1"}},
        {"text": "Turned down", "source": {"kind": "jira", "key": "PLAT-2"}},
        {"text": "   ", "source": {"kind": "jira", "key": "PLAT-3"}},
        {"text": "Bad link", "source": {"kind": "gdoc", "key": "d", "url": "javascript:alert(1)"}},
        {"text": "Dana's", "owner": "others", "owner_name": "Dana", "source": {"kind": "gmail", "key": "m-1"}},
    ]
    result = data(capsys, "items", "add-batch", "--stdin", "--propose", stdin=json.dumps(batch),
                  monkeypatch=monkeypatch)
    conforms(result, schema.DATA["items add-batch"])
    outcomes = [r["outcome"] for r in result["results"]]
    assert outcomes == ["created", "existing", "dismissed", "invalid", "invalid", "created"]
    assert result["results"][1]["id"] == kept["id"] and result["results"][2]["id"] == gone["id"]
    assert "text" in result["results"][3]["error"]["message"] and "https" in result["results"][4]["error"]["message"]
    proposed = items(capsys, "--status", "proposed")
    assert [i["text"] for i in proposed] == ["Book the room", "Dana's"]
    assert proposed[1]["owner"] == "someone_else" and proposed[1]["owner_name"] == "Dana"
    assert ids(items(capsys)) == [kept["id"]]  # an open item already there stays open


def test_a_batch_that_is_not_a_list_or_too_long_fails_whole(cfg, capsys, monkeypatch):
    code, err = error(capsys, "items", "add-batch", "--stdin", stdin="{nope", monkeypatch=monkeypatch)
    assert err["code"] == "invalid"
    many = [{"text": f"item {n}"} for n in range(101)]
    code, err = error(capsys, "items", "add-batch", "--stdin", stdin=json.dumps(many), monkeypatch=monkeypatch)
    assert err["code"] == "invalid" and "100" in err["message"]
    code, err = error(capsys, "items", "add-batch")
    assert err["code"] == "usage" and "--stdin" in err["message"]
    assert items(capsys, "--status", "any") == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.com/x", "https://", "file:///etc/passwd"])
def test_only_https_links_are_kept(cfg, capsys, url):
    code, err = error(capsys, "items", "add", "Read", "it", "--source-kind", "gdoc", "--source-key", "k",
                      "--source-url", url)
    assert err["code"] == "invalid" and "https" in err["message"]


def test_text_is_one_plain_line_within_its_limits(cfg, capsys):
    item = data(capsys, "items", "add", "Call\x07 the\nplumber‮", "--owner-name", "Dana\tB")
    assert item["text"] == "Call the plumber" and item["owner_name"] == "Dana B" and item["owner"] == "someone_else"
    code, err = error(capsys, "items", "add", "x" * 501)
    assert err["code"] == "invalid" and "text" in err["message"]
    code, err = error(capsys, "items", "add", "Mine", "--owner", "mine", "--owner-name", "Dana")
    assert err["code"] == "invalid"
    code, err = error(capsys, "items", "add", "Kind", "--source-kind", "Google Docs", "--source-key", "k")
    assert err["code"] == "invalid" and "source.kind" in err["message"]


# ---------------------------------------------------------------- proposed, accepted


def test_proposed_items_stay_off_the_list_and_the_tablet_until_accepted(cfg, capsys):
    item = data(capsys, "items", "add", "Send", "the", "estimate", "--propose", *GDOC)
    assert item["status"] == "proposed"
    assert items(capsys) == [] and items(capsys, "--status", "all") == []
    assert ids(items(capsys, "--status", "proposed")) == ids(items(capsys, "--status", "any")) == [item["id"]]
    assert data(capsys, "status")["items"] == {"open": 0, "done": 0, "proposed": 1}
    repo = api.Jotted.open(cfg).repo
    assert repo.todo_entries(include_others=True) == []
    code, err = error(capsys, "items", "done", str(item["id"]))
    assert err["code"] == "conflict" and "accept" in err["message"]
    assert data(capsys, "items", "edit", str(item["id"]), "Send", "the", "revised", "estimate")["status"] == "proposed"

    accepted = data(capsys, "items", "accept", str(item["id"]))
    conforms(accepted, schema.DATA["items accept"])
    assert accepted == {"accepted": [item["id"]], "skipped": []}
    assert ids(items(capsys)) == [item["id"]]
    assert [e.item_id for e in repo.todo_entries(include_others=True)] == [item["id"]]  # on the next publish
    code, err = error(capsys, "items", "accept", str(item["id"]))
    assert err["code"] == "conflict"
    code, err = error(capsys, "items", "accept", "999")
    assert err["code"] == "not_found"


def test_accept_all_of_one_kind_and_skip_what_isnt_proposed(cfg, capsys):
    a = data(capsys, "items", "add", "A", "--propose", "--source-kind", "gmail", "--source-key", "m1")
    b = data(capsys, "items", "add", "B", "--propose", "--source-kind", "jira", "--source-key", "J-1")
    typed = data(capsys, "items", "add", "Typed")
    result = data(capsys, "items", "accept", str(a["id"]), str(typed["id"]), "999")
    assert result == {"accepted": [a["id"]], "skipped": [{"id": typed["id"], "reason": "not_proposed"},
                                                          {"id": 999, "reason": "not_found"}]}
    assert data(capsys, "items", "accept", "--all", "--source-kind", "gmail") == {"accepted": [], "skipped": []}
    assert data(capsys, "items", "accept", "--all")["accepted"] == [b["id"]]


def test_the_proposed_limit(cfg, capsys):
    data(capsys, "settings", "set", "proposed_limit", "10")
    for n in range(10):
        data(capsys, "items", "add", f"item {n}", "--propose", "--source-kind", "chat", "--source-key", str(n))
    code, err = error(capsys, "items", "add", "one more", "--propose", "--source-kind", "chat", "--source-key", "x")
    assert err["code"] == "conflict" and "accept or dismiss" in err["message"]
    data(capsys, "items", "add", "Typed", "items", "still", "go", "on")
    code, err = error(capsys, "settings", "set", "proposed_limit", "5")
    assert err["code"] == "invalid"
    code, err = error(capsys, "settings", "set", "mcp_add_mode", "yolo")
    assert err["code"] == "invalid"


def test_proposing_and_accepting_write_their_events(cfg, capsys):
    start = data(capsys, "events")["cursor"]
    item = data(capsys, "items", "add", "Send", "it", "--propose", *GDOC)
    data(capsys, "items", "edit", str(item["id"]), "Send", "it", "today")
    data(capsys, "items", "accept", str(item["id"]))
    found = data(capsys, "events", "--since", str(start))
    conforms(found, schema.DATA["events"])
    assert [e["type"] for e in found["events"]] == ["item.proposed", "item.proposed", "item.accepted", "item.added"]
    assert found["events"][0]["item"]["origin"] == "agent" and found["events"][-1]["item"]["status"] == "open"
    resumed = data(capsys, "events", "--since", str(found["events"][1]["cursor"]))  # a restart misses none
    assert [e["type"] for e in resumed["events"]] == ["item.accepted", "item.added"]


# ---------------------------------------------------------------- reading the list


def test_get_query_and_pages(cfg, capsys):
    made = [data(capsys, "items", "add", f"Task {n}", *(["--source-kind", "gdoc", "--source-key", f"k{n}",
                                                        "--source-title", "Retro notes"] if n == 4 else []))
            for n in range(5)]
    got = data(capsys, "items", "get", str(made[0]["id"]))
    conforms(got, schema.DATA["items get"])
    assert got["text"] == "Task 0"
    code, err = error(capsys, "items", "get", "999")
    assert err["code"] == "not_found"
    assert ids(items(capsys, "--query", "retro")) == [made[4]["id"]]  # the source title counts
    assert ids(items(capsys, "--query", "TASK 3")) == [made[3]["id"]]
    first = items(capsys, "--limit", "2")
    second = items(capsys, "--limit", "2", "--cursor", str(first[-1]["id"]))
    third = items(capsys, "--limit", "2", "--cursor", str(second[-1]["id"]))
    assert ids(first + second + third) == ids(made) and len(third) == 1
    code, err = error(capsys, "items", "--limit", "201")
    assert err["code"] == "invalid"
    assert ids(items(capsys, "--source-kind", "gdoc")) == [made[4]["id"]]


def test_edit_changes_the_owner(cfg, capsys):
    item = data(capsys, "items", "add", "Review", "the", "deck")
    edited = data(capsys, "items", "edit", str(item["id"]), "--owner-name", "Dana")
    assert edited["owner"] == "someone_else" and edited["owner_name"] == "Dana" and edited["text"] == "Review the deck"
    mine = data(capsys, "items", "edit", str(item["id"]), "--owner", "mine")
    assert mine["owner"] == "me" and mine["owner_name"] is None
    code, err = error(capsys, "items", "edit", str(item["id"]))
    assert err["code"] == "usage"


def test_older_databases_gain_the_new_fields(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:  # the previous release's tables
        db.executescript(SCHEMA)
        db.execute("ALTER TABLE actions ADD COLUMN written INTEGER NOT NULL DEFAULT 0")
        for n, (source, doc) in enumerate((("remarkable", "nb"), ("web", "web"), ("todo", "todo-doc"))):
            db.execute("""INSERT INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, text,
                            paper_text, owner, p_action, text_changed_at, status_changed_at, created_at, updated_at)
                          VALUES (?, ?, 'Notes', '/', 'p1', 1, ?, ?, ?, 'me', 0.9, 't', 't', ?, 't')""",
                       (source, doc, f"1:{n}", f"item {n}", f"item {n}", f"2026-01-0{n + 1}"))
    repo = SqliteRepository(path)
    SqliteRepository(path)  # safe to run twice
    found = repo.items()
    assert [(i["origin"], i["source"]["kind"], i["source"]["key"]) for i in found] == [
        ("remarkable", "remarkable", "nb:1:0"), ("web", None, None), ("todo", "todo", "todo-doc:1:2")]
    assert found[0]["page"] == {"doc_id": "nb", "doc_name": "Notes", "page": 1, "page_count": None, "anchor": "1:0"}
    assert all(i["status"] == "open" and i["owner_name"] is None for i in found)


# ---------------------------------------------------------------- the agent's tools


def test_mcp_proposes_whatever_the_add_mode_and_adds_by_it(cfg, capsys):
    proposals = [{"text": "Send the estimate", "source": {"kind": "gdoc", "key": "doc1#a", "excerpt": "Sam to..."}}]
    proposed, added, unsourced = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "items_propose", "arguments": {"items": proposals}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "items_add", "arguments": {"text": "Buy milk"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "items_propose", "arguments": {"items": [{"text": "No source"}]}}},
    ])
    results = json.loads(proposed["result"]["content"][0]["text"])["results"]
    assert results[0]["outcome"] == "created"
    assert data(capsys, "items", "get", str(results[0]["id"]))["status"] == "proposed"
    assert json.loads(added["result"]["content"][0]["text"])["status"] == "open"
    assert json.loads(unsourced["result"]["content"][0]["text"])["results"][0]["outcome"] == "invalid"

    data(capsys, "settings", "set", "mcp_add_mode", "propose_all")
    (added,) = rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "items_add", "arguments": {"text": "Buy bread"}}}])
    assert json.loads(added["result"]["content"][0]["text"])["status"] == "proposed"


def test_mcp_accepts_a_list_of_ids_and_reads_pages(cfg, capsys):
    made = [data(capsys, "items", "add", f"P{n}", "--propose", "--source-kind", "chat", "--source-key", str(n))
            for n in range(3)]
    accepted, page = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "items_accept", "arguments": {"ids": [made[0]["id"], made[1]["id"]]}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "items_list", "arguments": {"status": "any", "limit": 2}}},
    ])
    assert json.loads(accepted["result"]["content"][0]["text"])["accepted"] == [made[0]["id"], made[1]["id"]]
    listed = json.loads(page["result"]["content"][0]["text"])
    assert len(listed["items"]) == 2 and listed["next_cursor"] == made[1]["id"]


# ---------------------------------------------------------------- jotted claude connect


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    monkeypatch.setenv(claude_desktop.PATH_VAR, str(path))
    binary = tmp_path / "bin" / "jotted"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return path, binary


def test_connect_keeps_other_servers_backs_up_and_is_idempotent(cfg, capsys, desktop):
    path, binary = desktop
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    path.chmod(0o600)
    out = data(capsys, "claude", "connect", "--command", str(binary))
    conforms(out, schema.DATA["claude connect"])
    assert out["changed"] and out["restart_required"] and Path(out["backup_path"]).is_file()
    written = json.loads(path.read_text())
    assert written["theme"] == "dark" and written["mcpServers"]["other"] == {"command": "x"}
    assert written["mcpServers"]["jotted"]["command"] == str(binary)
    assert written["mcpServers"]["jotted"]["args"] == ["mcp"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = data(capsys, "claude", "connect", "--command", str(binary))
    assert not again["changed"] and again["backup_path"] is None
    assert len(list(path.parent.glob("*.bak-*"))) == 1

    status = data(capsys, "claude", "status")
    conforms(status, schema.DATA["claude status"])
    assert status["configured"] and status["command_exists"] and not status["admin"]

    assert data(capsys, "claude", "connect", "--command", str(binary), "--admin")["entry"]["args"] == ["mcp", "--admin"]
    gone = data(capsys, "claude", "disconnect")
    assert gone["changed"] and json.loads(path.read_text())["mcpServers"] == {"other": {"command": "x"}}
    assert not data(capsys, "claude", "status")["configured"]


def test_connect_leaves_a_broken_config_alone(cfg, capsys, desktop):
    path, binary = desktop
    path.write_text("{not json")
    code, err = error(capsys, "claude", "connect", "--command", str(binary))
    assert err["code"] == "config" and code == 2
    assert path.read_text() == "{not json" and not list(path.parent.glob("*.bak-*"))
    code, err = error(capsys, "claude", "connect", "--command", str(binary.parent / "nope"))
    assert err["code"] == "invalid"


def test_connect_dry_run_and_a_moved_binary(cfg, capsys, desktop):
    path, binary = desktop
    planned = data(capsys, "claude", "connect", "--command", str(binary), "--dry-run")
    assert planned["changed"] and not path.exists()
    data(capsys, "claude", "connect", "--command", str(binary))  # no file yet: created, nothing to back up
    binary.rename(binary.with_name("jotted-old"))
    status = data(capsys, "claude", "status")
    assert status["configured"] and not status["command_exists"]


def test_connect_carries_what_decides_the_data_and_the_bundled_copy(cfg, capsys, desktop, monkeypatch, tmp_path):
    path, binary = desktop
    monkeypatch.setenv("JOTTED_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JOTTED_BUNDLED", "1")
    env = data(capsys, "claude", "connect", "--command", str(binary))["entry"]["env"]
    assert env == {"JOTTED_CONFIG": str(cfg.source), "JOTTED_HOME": str(tmp_path / "home"), "JOTTED_BUNDLED": "1"}
    status = data(capsys, "claude", "status")
    assert status["env"] == env and status["installed"]
    monkeypatch.delenv("JOTTED_BUNDLED")
    again = data(capsys, "claude", "connect", "--command", str(binary))
    assert again["changed"] and "JOTTED_BUNDLED" not in again["entry"]["env"]  # what's set now wins


def test_status_says_whether_claude_desktop_is_installed(cfg, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv(claude_desktop.PATH_VAR, str(tmp_path / "nowhere" / "claude_desktop_config.json"))
    status = data(capsys, "claude", "status")
    conforms(status, schema.DATA["claude status"])
    assert not status["installed"] and not status["configured"] and status["env"] == {}


def test_connect_needs_claude_desktop(cfg, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv(claude_desktop.PATH_VAR, str(tmp_path / "nowhere" / "claude_desktop_config.json"))
    code, err = error(capsys, "claude", "connect", "--command", sys.executable)
    assert err["code"] == "config" and "Claude Desktop" in err["message"]


# ---------------------------------------------------------------- found in review


def _hide(cfg, item_id: int) -> None:
    """The item's line is gone from its page (erased, or no longer read as an action)."""
    with sqlite3.connect(cfg.server.db) as db:
        db.execute("UPDATE actions SET missing = 1 WHERE id = ?", (item_id,))


def test_a_source_key_whose_line_is_gone_is_found_not_a_crash(cfg, capsys, monkeypatch):
    item = data(capsys, "items", "add", "Send", "the", "estimate", *GDOC)
    _hide(cfg, item["id"])
    again = data(capsys, "items", "add", "Send", "the", "estimate", *GDOC)
    assert not again["created"] and again["id"] == item["id"]
    batch = [{"text": "Send the estimate", "source": {"kind": "gdoc", "key": "doc1#h.x7q"}},
             {"text": "Book the room", "source": {"kind": "gcal", "key": "evt-1"}}]
    result = data(capsys, "items", "add-batch", "--stdin", "--propose", stdin=json.dumps(batch),
                  monkeypatch=monkeypatch)
    assert [r["outcome"] for r in result["results"]] == ["existing", "created"]
    assert items(capsys) == []  # the hidden one stays off the list; the new one waits as a proposal


def test_one_item_failing_unexpectedly_doesnt_stop_a_batch(cfg, capsys, monkeypatch):
    real = api.Jotted._add

    def flaky(self, text, *rest):
        if text == "boom":
            raise RuntimeError("disk on fire")
        return real(self, text, *rest)

    monkeypatch.setattr(api.Jotted, "_add", flaky)
    batch = [{"text": "boom"}, {"text": "Fine"}]
    result = data(capsys, "items", "add-batch", "--stdin", stdin=json.dumps(batch), monkeypatch=monkeypatch)
    conforms(result, schema.DATA["items add-batch"])
    assert [r["outcome"] for r in result["results"]] == ["internal", "created"]
    assert "disk on fire" in result["results"][0]["error"]["message"]


def test_the_same_source_key_added_at_once_by_many_processes(tmp_path):
    import threading

    path = tmp_path / "db.sqlite"
    SqliteRepository(path)
    start = threading.Barrier(8)
    outcomes, failures = [], []

    def add():
        repo = SqliteRepository(path)  # its own connections, as another process would have
        start.wait()
        try:
            outcomes.append(repo.add_item("Send it", origin="agent", source={"kind": "gdoc", "key": "k"})[1])
        except Exception as e:  # noqa: BLE001 - the test reports it
            failures.append(repr(e))

    threads = [threading.Thread(target=add) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == [] and sorted(outcomes) == ["created"] + ["existing"] * 7


def test_a_repeated_id_is_accepted_once_not_skipped(cfg, capsys):
    item = data(capsys, "items", "add", "A", "--propose", "--source-kind", "chat", "--source-key", "1")
    result = data(capsys, "items", "accept", str(item["id"]), str(item["id"]))
    assert result == {"accepted": [item["id"]], "skipped": []}
