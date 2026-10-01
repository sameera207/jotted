import json
import os
import random
import stat
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

from rmtasks import analysis, cli, cloud, config, notebook, strokes  # noqa: E402
from rmtasks.checkbox import candidate, detect  # noqa: E402
from rmtasks.lines import Line, cluster  # noqa: E402
from rmtasks.strokes import make_stroke  # noqa: E402

ROOT = Path(__file__).parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # AI steps off: tests never call external APIs (they are stubbed where exercised).
    text = (ROOT / "config.example.toml").read_text().replace("enabled     = true", "enabled     = false")
    text = text.replace("enabled          = true", "enabled          = false")
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    return config.load()


def to_strokes(paths, first_id=100):
    return [make_stroke(f"1:{first_id + i}", "fineliner", p) for i, p in enumerate(paths)]


# ---------------------------------------------------------------- config


def test_example_config_loads_and_resolves_paths(cfg, tmp_path):
    assert cfg.paths.cache_dir == (tmp_path / "cache").resolve()
    assert cfg.rmapi.token_file == (tmp_path / ".secrets/rmapi.conf").resolve()
    assert cfg.checkbox.bracket_gap == [0.3, 2.0]


@pytest.mark.parametrize(
    "extra, message",
    [
        ("[nope]\n", "unknown section"),
        ("[lines]\nband_tolrance = 1\n", "unknown key"),
        ('[notebook]\npages = "first"\n', "notebook.pages"),
        ("[checkbox]\nbox_aspect = [2, 1]\n", "greater than max"),
        ('[checkbox]\nstyles = ["circle"]\n', "unknown style"),
    ],
)
def test_config_rejects_bad_values(tmp_path, extra, message):
    p = tmp_path / "c.toml"
    p.write_text(extra)
    with pytest.raises(config.ConfigError, match=message):
        config.load(p)


# ---------------------------------------------------------------- strokes


def test_load_strokes_drops_deleted_ignored_and_dots(tmp_path, cfg):
    paths = [[(0, 0), (10, 10)], [(5, 5)]]  # the second is a single-point dot
    data = synth.rm_bytes(paths, deleted=[500])
    hl = synth.rm_bytes([[(0, 0), (50, 0)]], tool=synth.si.Pen.HIGHLIGHTER_2)
    (tmp_path / "a.rm").write_bytes(data)
    (tmp_path / "b.rm").write_bytes(hl)

    got = strokes.load_strokes(tmp_path / "a.rm", cfg.strokes)
    assert [s.id for s in got] == ["1:100"]
    assert got[0].tool == "fineliner"
    assert got[0].bbox == (0, 0, 10, 10)
    assert strokes.load_strokes(tmp_path / "b.rm", cfg.strokes) == []


# ---------------------------------------------------------------- lines and checkboxes


def test_cluster_separates_lines_and_places_tall_strokes(cfg):
    rng = random.Random(3)
    paths = synth.text(0, 100, 3, rng) + synth.text(0, 190, 3, rng)
    tall = [(-40, 80), (-40, 125)]  # margin mark overlapping line 1
    far = [(900, 1000), (900, 1400)]  # overlaps nothing
    lines, unassigned = cluster(to_strokes(paths + [tall, far]), cfg.lines)
    assert len(lines) == 2
    assert lines[0].strokes[0].points[0] == tall[0]
    assert [s.points[0] for s in unassigned] == [far[0]]


def test_demo_page_detection_matches_expected(cfg):
    paths, expected = synth.demo_page()
    lines, _ = cluster(to_strokes(paths), cfg.lines)
    got = []
    for ln in lines:
        cb = detect(ln, cfg.checkbox)
        got.append("note" if cb is None else ("task" if cb.text else "empty_checkbox"))
    assert got == expected


def test_letters_do_not_pass_as_brackets(cfg):
    # Two tall narrow letters close together, e.g. "ll": aspect passes, gap must not.
    paths = [[(0, 0), (4, 40)], [(8, 0), (12, 40)]] + synth.text(20, 20, 2)
    line = Line(to_strokes(paths))
    line.strokes.sort(key=lambda s: s.x0)
    assert detect(line, cfg.checkbox) is None


def test_scribble_is_not_a_box(cfg):
    rng = random.Random(5)
    scribble = [(rng.uniform(0, 40), rng.uniform(0, 40)) for _ in range(30)] + [(1, 1)]
    paths = [[(0, 0)] + scribble] + synth.text(60, 20, 2)
    line = Line(sorted(to_strokes(paths), key=lambda s: s.x0))
    cb = candidate(line, cfg.checkbox)
    assert cb is None or cb.style != "single_box" or cb.checks["path_ratio"] is False


def test_lone_checkbox_has_no_text(cfg):
    line = Line(to_strokes(synth.bracket_pair(0, 20)))
    assert detect(line, cfg.checkbox).text == []


# ---------------------------------------------------------------- notebook and end to end


def test_notebook_page_order_and_selection(tmp_path, cfg):
    p = synth.rmdoc(tmp_path / "x.rmdoc", [synth.rm_bytes([[(0, 0), (1, 1)]])] * 3)
    nb = notebook.open_notebook(p, tmp_path / "unpacked")
    assert [pg.index for pg in nb.pages] == [1, 2, 3]
    assert nb.name == "Tasks" and nb.id == "doc-0001"
    assert all(pg.rm_path and pg.rm_path.is_file() for pg in nb.pages)
    assert [pg.index for pg in notebook.select_pages(nb.pages, "last")] == [3]
    assert [pg.index for pg in notebook.select_pages(nb.pages, [1, 3, 9])] == [1, 3]


def _scan(tmp_path, paths, ids, name):
    doc = synth.rmdoc(tmp_path / f"{name}.rmdoc", [synth.rm_bytes(paths, ids=ids)])
    assert cli.main(["analyse", str(doc)]) == 0
    return sorted((tmp_path / "out").iterdir())[-1]


def test_analyse_then_diff_proves_anchor_stability(tmp_path, cfg, capsys):
    paths, expected = synth.demo_page()
    ids = list(range(100, 100 + len(paths)))
    run_a = _scan(tmp_path, paths, ids, "a")
    report = json.loads((run_a / "report.json").read_text())
    kinds = [ln["kind"] for ln in report["pages"][0]["lines"]]
    assert kinds == expected
    assert (run_a / "page-01.svg").read_text().startswith("<svg")

    # Edit elsewhere: a new note line mid-page, with fresh stroke IDs. Existing strokes keep theirs.
    new = synth.text(-600, 245, 3)
    run_b = _scan(tmp_path, paths + new, ids + list(range(900, 900 + len(new))), "b")
    assert cli.main(["diff", str(run_a), str(run_b)]) == 0
    assert "missing 0" in capsys.readouterr().out

    # If the strokes were re-created (new IDs), diff must fail.
    shifted = [i + 5000 for i in ids]
    run_c = _scan(tmp_path, paths, shifted, "c")
    assert cli.main(["diff", str(run_a), str(run_c)]) == 1
    assert "MISSING" in capsys.readouterr().out


# ---------------------------------------------------------------- cloud wrapper, with a fake rmapi


FAKE_RMAPI = """#!/bin/sh
echo "$RMAPI_CONFIG|$RMAPI_TRACE|$*" >> "{log}"
case "$*" in
  "ls /") read code; echo "token-for-$code" > "$RMAPI_CONFIG" ;;
  "-ni -json find /") echo 'Refreshing tree...'; echo '[{{"id":"abc","name":"Tasks","type":"DocumentType","version":42,"parent":""}},{{"id":"f1","name":"Tasks","type":"CollectionType","version":0,"parent":""}}]' ;;
  "-ni get --id abc") cp "{doc}" "Tasks.rmdoc" ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""


def test_cloud_flow_with_fake_rmapi(tmp_path, cfg, monkeypatch):
    doc = synth.rmdoc(tmp_path / "src.rmdoc", [synth.rm_bytes([[(0, 0), (5, 5)]])])
    calls = tmp_path / "calls.log"
    fake = tmp_path / "rmapi"
    fake.write_text(FAKE_RMAPI.format(log=calls, doc=doc))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RMAPI_TRACE", "1")  # must not leak through when trace = false

    cloud.register(cfg, "abcd1234")
    token = cfg.rmapi.token_file
    assert token.read_text().strip() == "token-for-abcd1234"
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600

    ref = cloud.find_notebook(cfg)
    assert (ref.id, ref.version) == ("abc", 42)
    path = cloud.download(cfg, ref)
    assert path == cfg.paths.cache_dir / "abc.rmdoc"
    nb = notebook.open_notebook(path, cfg.paths.cache_dir / "unpacked")
    assert nb.version == 42  # from the sidecar written by download()

    for line in calls.read_text().splitlines():
        conf, trace, _ = line.split("|")
        assert conf == str(token) and trace == ""


# ---------------------------------------------------------------- recognition and classification (stubbed)

from rmtasks import classify as classify_mod, recognise, report  # noqa: E402
from rmtasks.aicache import AICache  # noqa: E402
from rmtasks.classify import Judgment  # noqa: E402
from rmtasks.recognise import Transcript  # noqa: E402


def _result(t=None, j=None, geo="note"):
    line = Line(to_strokes([[(0, 0), (10, 10)]]))
    return report.LineResult(line=line, kind="note", checkbox=None, geometric_kind=geo, transcript=t, judgment=j)


def _j(p_todo, choice=None):
    rest = (1 - p_todo) / 2
    probs = {"todo": p_todo, "note": rest, "heading": rest}
    return Judgment(n=1, choice=choice or max(probs, key=probs.get), probabilities=probs, confidence=0.9)


@pytest.mark.parametrize(
    "t, j, checkbox_is_task, expected",
    [
        (None, None, False, "task"),  # recognition off: geometry decides
        (Transcript(1, "empty", ""), None, False, "empty_checkbox"),
        (Transcript(1, "empty", "call Bob"), _j(0.9), False, "task"),
        (Transcript(1, "checked", "call Bob"), _j(0.9), False, "done"),
        (Transcript(1, "none", "buy milk"), _j(0.8), False, "task"),  # no box, still a task
        (Transcript(1, "empty", "o options"), _j(0.5), False, "note"),
        (Transcript(1, "empty", "o options"), _j(0.5), True, "task"),  # the box wins when configured
        (Transcript(1, "none", "Monday 29 Sep"), _j(0.05, "heading"), False, "heading"),
        (Transcript(1, "empty", "call Bob"), None, False, "task"),  # classification off
    ],
)
def test_kind_policy(cfg, t, j, checkbox_is_task, expected):
    import dataclasses

    cfg = dataclasses.replace(cfg, classification=dataclasses.replace(cfg.classification, checkbox_is_task=checkbox_is_task))
    assert analysis.decide(_result(t, j, geo="task"), cfg) == expected


def test_render_line_is_png(cfg):
    line = Line(to_strokes(synth.bracket_pair(0, 20) + synth.text(60, 20, 2)))
    png = recognise.render_line(line)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_anchor_is_first_written_stroke():
    line = Line([make_stroke(i, "fineliner", [(0, 0), (1, 1)]) for i in ("2:5", "1:9", "1:3")])
    assert line.anchor_id == "1:3"


class _FakeAnthropic:
    calls = 0

    def __init__(self, **kw):
        self.beta = self
        self.messages = self

    def create(self, **kw):
        _FakeAnthropic.calls += 1
        ns = [int(b["text"].split()[1].rstrip(":")) for b in kw["messages"][0]["content"]
              if b["type"] == "text" and b["text"].startswith("Line ")]
        body = json.dumps({"lines": [{"n": n, "drawing": False, "checkbox": "empty", "text": f"line {n}"} for n in ns]})
        from types import SimpleNamespace as NS
        return NS(stop_reason="end_turn", content=[NS(type="text", text=body)],
                  usage=NS(input_tokens=1, output_tokens=1), _request_id="req_test")


def test_transcribe_sends_only_uncached_lines(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(recognise.anthropic, "Anthropic", _FakeAnthropic)
    monkeypatch.setenv(cfg.recognition.api_key_env, "test-key")
    _FakeAnthropic.calls = 0
    lines_, _ = cluster(to_strokes(synth.demo_page()[0]), cfg.lines)
    cache = AICache(tmp_path / "ai")
    got = recognise.transcribe(lines_, cfg.recognition, cache)
    assert _FakeAnthropic.calls == 1 and len(got) == len(lines_)
    again = recognise.transcribe(lines_, cfg.recognition, cache)
    assert _FakeAnthropic.calls == 1 and all(t.cached for t in again.values())


def test_classify_builds_one_question_per_line_and_caches(tmp_path, cfg, monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def system_one(self, state, questions):
            from types import SimpleNamespace as NS
            seen["state"], seen["questions"] = state, questions
            choices = {q: NS(choice="todo", probabilities={"todo": 0.8, "note": 0.15, "heading": 0.05}, confidence=0.7)
                       for q in questions}
            return NS(choices=choices, model="jev-test", request_id="req_test")

    monkeypatch.setattr(classify_mod, "TypeSafeClient", FakeClient)
    monkeypatch.setenv(cfg.classification.api_key_env, "test-key")
    ts = {1: Transcript(1, "empty", "call Bob"), 2: Transcript(2, "empty", ""), 3: Transcript(3, "none", "notes")}
    cache = AICache(tmp_path / "ai")
    got = classify_mod.classify(ts, cfg.classification, cache)
    assert set(seen["questions"]) == {"line_1", "line_3"}  # the empty line is not sent
    assert [ln["n"] for ln in seen["state"]["lines"]] == [1, 3]  # empty lines stay out of Jev's state
    assert got[1].p_todo == 0.8
    seen.clear()
    assert classify_mod.classify(ts, cfg.classification, cache)[3].cached and not seen


# ---------------------------------------------------------------- drawings and wrapped lines


def _line(paths, n, first_id):
    ln = Line(to_strokes(paths, first_id))
    ln.n = n
    return ln


def test_drawing_fragments_merge_into_one_line(cfg):
    # A tall box with a label and an arrow inside: the band sweep cuts it into pieces.
    box = [[(0, 0), (200, 0), (200, 150), (0, 150), (0, 0)]]
    label = synth.text(20, 40, 1)
    arrow = [[(20, 110), (180, 110)]]
    text_line = synth.text(0, 300, 2)
    lines_, _ = cluster(to_strokes(box + label + arrow + text_line), cfg.lines)
    assert len(lines_) == 2
    assert lines_[0].bbox[3] <= 160 and lines_[1].bbox[1] > 250


def test_continuation_candidates_use_page_spacing(cfg):
    ts = {n: Transcript(n, "none", t) for n, t in [(1, "buy milk"), (2, "call mum"), (3, "email the landlord about"),
                                                   (4, "the broken heater"), (5, "- pay rent"), (6, "tidy desk")]}
    ys = {1: 100, 2: 200, 3: 300, 4: 345, 5: 390, 6: 500}  # 4 sits tight under 3; 5 is tight but bulleted
    lines_ = [_line(synth.text(0, ys[n], 2), n, 100 * n) for n in ts]
    got = classify_mod.continuation_candidates(lines_, ts, cfg.classification)
    assert [(a, b) for a, b, _, _ in got] == [(3, 4)]
    assert got[0][2] < 0.75


def test_indented_line_under_a_bullet_is_a_candidate_at_normal_spacing(cfg):
    ts = {1: Transcript(1, "none", "- get the new items to prod"), 2: Transcript(2, "none", "- get connectors involved in"),
          3: Transcript(3, "none", "testing"), 4: Transcript(4, "none", "- pay rent")}
    lines_ = [_line(synth.text(0, 100, 3), 1, 100), _line(synth.text(0, 200, 3), 2, 200),
              _line(synth.text(250, 300, 1), 3, 300), _line(synth.text(0, 400, 2), 4, 400)]
    got = classify_mod.continuation_candidates(lines_, ts, cfg.classification)
    assert [(a, b, ind) for a, b, _, ind in got] == [(2, 3, True)]


def test_loose_descender_joins_its_row(cfg):
    word = synth.text(0, 100, 3)
    descender = [[(40, 92), (40, 145)]]  # the p of "prod": overlaps the row by ~45%, hangs below it
    lines_, _ = cluster(to_strokes(word + descender + synth.text(0, 260, 2)), cfg.lines)
    assert len(lines_) == 2


def test_adjacent_drawings_pair_up(cfg):
    ts = {1: Transcript(1, "none", "", drawing=True), 2: Transcript(2, "none", "", drawing=True),
          3: Transcript(3, "none", "later", drawing=False)}
    lines_ = [_line([[(0, 0), (100, 0), (100, 90), (0, 90)]], 1, 100),
              _line([[(10, 95), (90, 96)]], 2, 200),
              _line(synth.text(0, 200, 1), 3, 300)]
    assert classify_mod.adjacent_drawings(lines_, ts, cfg.classification) == [(1, 2)]


def test_merge_continuations_keeps_first_line_and_anchor():
    a, b, c = _line(synth.text(0, 100, 1), 1, 100), _line(synth.text(0, 140, 1), 2, 200), _line(synth.text(0, 180, 1), 3, 300)
    ts = {1: Transcript(1, "empty", "email the landlord"), 2: Transcript(2, "none", "about the"),
          3: Transcript(3, "none", "broken heater")}
    merged_lines, merged_ts, parts = analysis.merge_continuations([a, b, c], ts, [(1, 2), (2, 3)])
    assert [ln.n for ln in merged_lines] == [1] and parts == {1: [1, 2, 3]}
    assert merged_ts[1].text == "email the landlord about the broken heater"
    assert merged_ts[1].checkbox == "empty"
    assert merged_lines[0].anchor_id == a.anchor_id


def test_drawing_kind(cfg):
    assert analysis.decide(_result(Transcript(1, "none", "aws → CI", drawing=True), None), cfg) == "drawing"


# ---------------------------------------------------------------- PDF template (write path)

import re  # noqa: E402

from rmtasks import template  # noqa: E402


def test_to_pdf_maps_page_corners():
    assert template.to_pdf(-702, 0) == (0, template.PAGE_H)
    assert template.to_pdf(702, 1872) == (template.PAGE_W, 0)
    assert template.to_pdf(0, 936) == (template.PAGE_W / 2, template.PAGE_H / 2)
    x, y = template.to_pdf(-425 * 1.0525, 1118 * 1.0525, 1.0525)
    assert abs(x - (702 - 425) / 3) < 1e-6 and abs(y - (template.PAGE_H - 1118 / 3)) < 1e-6


def test_zones(cfg):
    z = template.zones(cfg.template)
    assert z.of(50) == "header" and z.of(900) == "body" and z.of(1800) == "footer"


def test_build_keeps_requested_page_count(tmp_path, cfg):
    pdf = template.build(tmp_path / "T.pdf", cfg.template, {2: template.PageState(strikes=[(-300, 200, 600)],
                                                                                 footer=["buy milk"])}, page_count=3)
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF") and len(re.findall(rb"/Type /Page[^s]", data)) == 3


def test_zoned_analysis_reads_only_body_as_tasks(tmp_path, cfg):
    z = template.zones(cfg.template)
    header = synth.text(-600, z.header_bottom / 2, 1)
    body = synth.bracket_pair(-600, 700) + synth.text(-480, 700, 2)
    footer = synth.bracket_pair(-600, z.footer_top + 100) + synth.text(-480, z.footer_top + 100, 2)
    doc = synth.rmdoc(tmp_path / "t.rmdoc", [synth.rm_bytes(header + body + footer)], file_type="pdf")
    run = analysis.analyse_notebook(doc, cfg)
    kinds = [(r.zone, r.kind) for r in run.pages[0].lines]
    assert kinds == [("header", "header"), ("body", "task"), ("footer", "footer")]
    assert run.notebook["file_type"] == "pdf" and run.page_count == 1


# ---------------------------------------------------------------- task store and web app

from rmtasks.server import create_app  # noqa: E402
from rmtasks.store import Store  # noqa: E402


def _template_run(tmp_path, cfg, paths, ids=None, name="t"):
    doc = synth.rmdoc(tmp_path / f"{name}.rmdoc", [synth.rm_bytes(paths, ids=ids)], file_type="pdf")
    return analysis.analyse_notebook(doc, cfg)


def _stub_ai(monkeypatch, texts):
    """Transcripts by line order; every line with text is a todo."""
    def transcribe(lines_, cfg_, cache):
        return {ln.n: Transcript(ln.n, "none", texts.get(ln.n, f"task {ln.n}")) for ln in lines_}

    def judge(ts, cfg_, cache):
        return {n: Judgment(n, "todo", {"todo": 0.9, "note": 0.05, "heading": 0.05}, 0.9) for n in ts}

    monkeypatch.setattr(recognise, "transcribe", transcribe)
    monkeypatch.setattr(classify_mod, "classify", judge)
    monkeypatch.setattr(classify_mod, "continuations", lambda *a: [])


@pytest.fixture
def ai_cfg(cfg):
    import dataclasses

    return dataclasses.replace(
        cfg,
        recognition=dataclasses.replace(cfg.recognition, enabled=True),
        classification=dataclasses.replace(cfg.classification, enabled=True),
    )


BODY = synth.text(-600, 600, 2) + synth.text(-600, 800, 2)


def test_store_pull_then_web_edits_become_print_instructions(tmp_path, ai_cfg, monkeypatch):
    _stub_ai(monkeypatch, {})
    store = Store(tmp_path / "db.sqlite")
    run = _template_run(tmp_path, ai_cfg, BODY)
    s = store.apply_run(run, "2026-09-29T10:00:00Z")
    assert s.new == 2
    nid = run.notebook["id"]
    tasks = [t for p in store.state(nid)["pages"] for t in p["tasks"]]
    first, second = tasks
    store.edit_task(first["id"], status="done")
    store.edit_task(second["id"], text="new wording")
    store.add_web_task(nid, "from the web")
    assert store.state(nid)["pending_push"]

    pages, snapshot = store.page_states(nid)
    ps = pages[1]
    assert len(ps.strikes) == 1 and abs(ps.strikes[0][2] - 600) < 20  # done: struck through
    assert len(ps.moved) == 1 and ps.moved[0][1] == 1  # edited: outlined and numbered, not struck
    assert abs((ps.moved[0][0][0][1] + ps.moved[0][0][0][3]) / 2 - 800) < 20
    assert ps.footer == [("new wording", False, 1), ("from the web", False)]

    store.record_push(nid, run.run_id, {}, snapshot)
    assert not store.state(nid)["pending_push"]
    store.edit_task(first["id"], status="open")  # a change after the snapshot stays pending
    assert store.state(nid)["pending_push"]


def test_template_draws_moved_marks_and_numbered_footer(tmp_path, cfg):
    ps = template.PageState(moved=[([(-600, 580, -200, 620), (-600, 640, -300, 680)], 1)],
                            footer=[("new wording", False, 1), ("web task", True)])
    pdf = template.build(tmp_path / "m.pdf", cfg.template, {1: ps}, page_count=1)
    assert pdf.read_bytes().startswith(b"%PDF")


def test_scheduler_coalesces_bursts_of_edits():
    import time as _time

    from rmtasks.app import Scheduler

    class FakeApp:
        pushes = 0

        def tasks_push_if_pending(self):
            FakeApp.pushes += 1
            return True

        def sync_todo(self):
            return {}

    sched = Scheduler(FakeApp(), push_delay_s=0.2)
    import threading

    threading.Thread(target=sched._push_loop, daemon=True).start()
    for _ in range(5):
        sched.push_soon()
        _time.sleep(0.05)
    assert sched.describe()["push"]["scheduled"]
    for _ in range(40):
        if FakeApp.pushes:
            break
        _time.sleep(0.05)
    _time.sleep(0.3)
    assert FakeApp.pushes == 1 and not sched.describe()["push"]["scheduled"]


def test_most_recent_change_wins(tmp_path, ai_cfg, monkeypatch):
    _stub_ai(monkeypatch, {1: "call Bob"})
    store = Store(tmp_path / "db.sqlite")
    run = _template_run(tmp_path, ai_cfg, BODY)
    store.apply_run(run, "2026-09-29T10:00:00Z")
    nid = run.notebook["id"]
    task = next(t for p in store.state(nid)["pages"] for t in p["tasks"] if t["text"] == "call Bob")

    store.edit_task(task["id"], text="call Bob about invoices")  # web edit, now

    # The paper text changes, but in a notebook version older than the web edit: the web wins.
    _stub_ai(monkeypatch, {1: "call Robert"})
    store.apply_run(_template_run(tmp_path, ai_cfg, BODY, name="t2"), "2026-09-29T10:05:00Z")
    t = store.task(task["id"])
    assert t["text"] == "call Bob about invoices" and t["paper_text"] == "call Robert"

    # A newer paper change wins over the web edit.
    _stub_ai(monkeypatch, {1: "call Roberta"})
    store.apply_run(_template_run(tmp_path, ai_cfg, BODY, name="t3"), "2999-01-01T00:00:00Z")
    assert store.task(task["id"])["text"] == "call Roberta"


def test_task_missing_from_page_is_flagged(tmp_path, ai_cfg, monkeypatch):
    _stub_ai(monkeypatch, {})
    store = Store(tmp_path / "db.sqlite")
    ids = list(range(100, 100 + len(BODY)))
    run = _template_run(tmp_path, ai_cfg, BODY, ids=ids)
    store.apply_run(run, "2026-09-29T10:00:00Z")
    first_line = synth.text(-600, 600, 2)
    run2 = _template_run(tmp_path, ai_cfg, first_line, ids=ids[: len(first_line)], name="t2")
    assert store.apply_run(run2, "2026-09-29T10:05:00Z").missing == 1


def test_web_api(tmp_path, ai_cfg, monkeypatch):
    _stub_ai(monkeypatch, {})
    store = Store(tmp_path / "db.sqlite")
    run = _template_run(tmp_path, ai_cfg, BODY)

    from rmtasks import sync

    def fake_pull(cfg_, store_, console=None):
        summary = store_.apply_run(run, "2026-09-29T10:00:00Z")
        return sync.PullResult(doc=None, run=run, summary=summary, rmdoc=None)

    pushes = []
    monkeypatch.setattr(sync, "pull", fake_pull)
    monkeypatch.setattr(sync, "push", lambda cfg_, store_, **kw: pushes.append(1))
    import dataclasses

    app_cfg = dataclasses.replace(
        ai_cfg,
        notebook=dataclasses.replace(ai_cfg.notebook, name=run.notebook["name"]),
        server=dataclasses.replace(ai_cfg.server, auto_push_delay_s=0),
    )
    client = create_app(app_cfg, store, background=False).test_client()

    assert client.get("/").status_code == 200
    assert client.get("/api/state").get_json()["notebook"] is None
    assert client.post("/api/tasks", json={"text": "x"}).status_code == 400  # not pulled yet

    r = client.post("/api/pull").get_json()
    assert r["summary"]["new"] == 2 and r["state"]["writable"]
    r = client.post("/api/tasks", json={"text": "Book the retro room"})
    assert r.status_code == 201
    web_id = r.get_json()["task"]["id"]
    paper_id = r.get_json()["state"]["pages"][0]["tasks"][0]["id"]

    t = client.patch(f"/api/tasks/{paper_id}", json={"status": "done"}).get_json()["task"]
    assert t["status"] == "done"
    assert client.delete(f"/api/tasks/{paper_id}").status_code == 400  # paper tasks can't be deleted
    assert client.delete(f"/api/tasks/{web_id}").status_code == 200
    assert client.patch("/api/tasks/999", json={"status": "done"}).status_code == 404
    import time as _time

    assert client.get("/api/state").get_json()["background"]["push"]["scheduled"]  # edits schedule a write


def test_bullets_are_stripped_from_task_text():
    from rmtasks.store import clean_text

    assert clean_text("- test prod") == "test prod"
    assert clean_text("• call Bob") == "call Bob"
    assert clean_text("buy milk") == "buy milk"
