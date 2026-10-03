"""The Jotted widget (specs/Claude-widget-spec.md): one self-contained, inert document served
as `ui://jotted/list`, `show_list` that opens it, and tools only the widget can call."""

import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from jotted import mcp, mcp_ui  # noqa: E402
from test_contract import cfg, data  # noqa: E402, F401

UI = Path(mcp_ui.__file__).parent
UI_HOST = {"capabilities": {"extensions": {mcp.UI_EXTENSION: {"mimeTypes": [mcp_ui.MIME]}}}}


def rpc(messages: list[dict], **serve) -> list[dict]:
    out = io.StringIO()
    mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out, **serve)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def message(n: int, method: str, **params) -> dict:
    return {"jsonrpc": "2.0", "id": n, "method": method, "params": params}


def as_widget_host(*messages: dict, **serve) -> list[dict]:
    """A session with a host that renders MCP Apps; the initialize answer is dropped."""
    return rpc([message(0, "initialize", **UI_HOST), *messages], **serve)[1:]


# ---------------------------------------------------------------- the document


def test_the_resource_is_one_document_with_everything_inlined(cfg):
    listed, read = as_widget_host(message(1, "resources/list"), message(2, "resources/read", uri=mcp_ui.URI))
    (resource,) = listed["result"]["resources"]
    assert resource["uri"] == "ui://jotted/list" and resource["mimeType"] == "text/html;profile=mcp-app"
    assert resource["_meta"]["ui"]["csp"] == {}  # no network at all
    (content,) = read["result"]["contents"]
    html = content["text"]
    assert content["mimeType"] == mcp_ui.MIME and html.startswith("<!doctype html>")
    assert "/* LIST_CSS */" not in html and "/* SCRIPTS */" not in html
    assert "const Bridge" in html and "const View" in html and "--paper" in html
    assert html.index("const Bridge") < html.index("const View")  # list.js uses Bridge
    assert html.count("<script") == 1 and html.count("<style") == 1
    (missing,) = rpc([message(1, "resources/read", uri="ui://jotted/nope")])
    assert missing["error"]["code"] == -32002


def test_the_widget_loads_nothing_from_the_network():
    html = mcp_ui.load_html()
    without_comments = re.sub(r"/\*.*?\*/|<!--.*?-->|^\s*//.*$", "", html, flags=re.S | re.M)
    assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", without_comments.replace('"https://"', ""))
    assert not re.search(r"<script[^>]*\bsrc=|<link[^>]*\bhref=|@import|url\(", html, flags=re.I)
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "WebSocket" not in html


def test_the_scripts_never_turn_data_into_markup_or_code():
    for name in mcp_ui.SCRIPTS:
        code = (UI / name).read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert banned not in code, f"{name} uses {banned}"
        assert not re.search(r"set(Timeout|Interval)\(\s*[\"'`]", code), f"{name} has a string timer"


def test_the_widget_stays_within_its_size_budget():
    assert len(mcp_ui.load_html().encode()) < 100_000
    assert len((UI / "list.js").read_text().splitlines()) < 1500


def test_ui_dir_serves_edits_without_reinstalling(cfg, tmp_path):
    for name in ("list.html", "list.css", *mcp_ui.SCRIPTS):
        (tmp_path / name).write_text((UI / name).read_text())
    (tmp_path / "list.css").write_text("/* edited */ body { color: red; }")
    (read,) = as_widget_host(message(1, "resources/read", uri=mcp_ui.URI), ui_dir=str(tmp_path))
    assert "/* edited */" in read["result"]["contents"][0]["text"]


def test_a_script_that_would_close_its_tag_is_refused(tmp_path):
    for name in ("list.html", "list.css", *mcp_ui.SCRIPTS):
        (tmp_path / name).write_text((UI / name).read_text())
    (tmp_path / "list.js").write_text("const x = '</script>';")
    try:
        mcp_ui.load_html(tmp_path)
    except ValueError as e:
        assert "list" in str(e) or "scripts" in str(e)
    else:
        raise AssertionError("a closing tag inside a script was inlined")


# ---------------------------------------------------------------- tools


def test_widget_only_tools_need_a_host_that_renders_the_widget(cfg):
    plain = {t["name"] for t in rpc([message(1, "tools/list")])[0]["result"]["tools"]}
    widget_only = {"items_add_typed", "items_changes", "page_image", "line_image"}
    assert not plain & widget_only and "show_list" in plain
    (refused,) = rpc([message(1, "tools/call", name="page_image", arguments={"doc_id": "d", "page": 1})])
    assert refused["result"]["isError"] and "widget" in refused["result"]["content"][0]["text"]

    (listed,) = as_widget_host(message(1, "tools/list"))
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert widget_only <= set(tools)
    for name in widget_only:
        assert tools[name]["_meta"]["ui"]["visibility"] == ["app"]
    assert tools["show_list"]["_meta"]["ui"]["resourceUri"] == mcp_ui.URI
    assert "follow" not in tools["items_changes"]["inputSchema"]["properties"]  # it would never return
    assert "out" not in tools["page_image"]["inputSchema"]["properties"]
    assert "agent" not in tools["items_add_typed"]["inputSchema"]["properties"]


def test_show_list_gives_the_model_text_and_the_widget_data(cfg, capsys):
    mine = data(capsys, "items", "add", "Call", "the", "plumber")
    data(capsys, "items", "add", "Review", "the", "deck", "--owner-name", "Sam")
    done = data(capsys, "items", "add", "Order", "ink")
    data(capsys, "items", "done", str(done["id"]))
    proposed = data(capsys, "items", "add", "Send", "the", "estimate", "--propose", "--agent",
                    "--source-kind", "gdoc", "--source-key", "d#1", "--excerpt", "Sam to send it")
    (shown,) = rpc([message(1, "tools/call", name="show_list", arguments={})])
    result = shown["result"]
    assert not result["isError"]
    text = result["content"][0]["text"]
    assert text.startswith("1 open for the person, 1 for others, 1 proposed, 1 done.")
    assert f"#{proposed['id']} Send the estimate" in text and "(Sam)" in text
    widget = result["structuredContent"]
    assert set(widget) >= {"as_of", "cursor", "counts", "items", "next_cursor"}
    assert widget["counts"] == {"open_mine": 1, "open_others": 1, "proposed": 1, "done": 1}
    assert [i["id"] for i in widget["items"]][0] == proposed["id"]  # proposals first
    assert widget["items"][0]["source"]["excerpt"] == "Sam to send it"
    assert {i["id"] for i in widget["items"]} >= {mine["id"], done["id"]} and widget["next_cursor"] is None
    assert all("bbox" not in i and "p_action" not in i for i in widget["items"])
    assert widget["as_of"].endswith("Z") and isinstance(widget["cursor"], int)


def test_the_widget_adds_as_the_person_and_follows_changes(cfg, capsys):
    start = data(capsys, "events")["cursor"]
    added, changes = as_widget_host(
        message(1, "tools/call", name="items_add_typed", arguments={"text": "Water the plants", "owner": "mine"}),
        message(2, "tools/call", name="items_changes", arguments={"since": start}))
    item = added["result"]["structuredContent"]
    assert item["origin"] == "web" and item["status"] == "open"  # typed by the person, not the agent
    events = changes["result"]["structuredContent"]["events"]
    assert [e["type"] for e in events] == ["item.added"] and "bbox" not in events[0]["item"]


def test_images_never_reach_the_model_as_text(cfg, monkeypatch):
    from jotted import cli

    monkeypatch.setattr(cli, "invoke", lambda argv, stdin=None: {"v": 1, "ok": True, "data": {"svg": "<svg/>"}})
    (image,) = as_widget_host(message(1, "tools/call", name="page_image",
                                      arguments={"doc_id": "d", "page": 2, "anchor": "1:4", "width": 600}))
    result = image["result"]
    assert result["structuredContent"] == {"mime": "image/svg+xml", "svg": "<svg/>"}
    assert "<svg" not in result["content"][0]["text"]


# ---------------------------------------------------------------- packaging


def test_the_wheel_ships_the_widget_but_not_its_dev_files(tmp_path):
    root = Path(__file__).parent.parent
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=root, check=True,
                   capture_output=True)
    (wheel,) = tmp_path.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for name in ("__init__.py", "list.html", "list.css", "bridge.js", "list.js"):
        assert f"jotted/mcp_ui/{name}" in names
    assert not any("/mcp_ui/dev/" in n for n in names)
