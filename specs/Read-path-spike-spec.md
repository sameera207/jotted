# rM Tasks — Read-path spike spec

Sep 28, 2026 · @Sam · revised Sep 29, 2026: tasks are decided from the recognised text, not the `[ ]` mark alone

## Purpose and goals

The spike proves the read path: a script pulls the Tasks notebook from reMarkable Cloud, reads each handwritten line, and says which lines are to-do tasks, anchored to stroke IDs that stay stable between syncs. It is strictly read-only. Nothing is written back to the cloud or the tablet.

Goals:

1. Authenticate against reMarkable Cloud with the Connect account, from a script.
2. Download one named notebook and parse its v6 `.rm` pages.
3. Group strokes into text lines.
4. Transcribe each line (Claude, vision) and decide whether it is a to-do (Jev, TypeSafe's System One model).
5. Report each line with its text, kind, probability and anchor stroke ID, plus a visual overlay.
6. Prove anchoring: existing tasks keep their anchor after the page is edited elsewhere.

Why the change: on real handwriting the geometric `[ ]` detector missed 3 of 4 checkboxes (multi-stroke and squat brackets), and loosening its thresholds let about 1 in 10 letter pairs through. Reading the text removes the dependence on how the box is drawn and also catches tasks written without a box. The geometric detector stays as the offline fallback.

Time box: one evening to the first report, one more session to tune detection.

## Scope

In scope:

- A Python CLI, `jotted`, with `auth`, `scan`, `analyse` and `diff` commands.
- Download through `rmapi` (the maintained [ddvk fork](https://github.com/ddvk/rmapi)); parsing through [rmscene](https://pypi.org/project/rmscene/).
- Line clustering.
- Handwriting recognition per line with Claude (vision), including whether the line starts with an empty or ticked checkbox.
- Task classification per line with Jev (`todo`, `note`, `heading`), using the transcribed text, the checkbox mark and neighbouring lines.
- Geometric checkbox detection (bracket pair and single-stroke box) as a fallback when the AI steps are switched off.
- A local cache of transcriptions and judgments keyed by stroke IDs, so re-runs cost nothing and work offline.
- A terminal table, a JSON report, and one SVG overlay per page.
- An offline mode that analyses an already downloaded notebook. With a warm cache, or with the AI steps off, it needs no network.

Out of scope (later phases):

- Strike-through detection, a task database, polling, or any server.
- Any write to the cloud or the tablet.
- Other checkbox styles (four-stroke boxes, circles) and multi-column pages.

## Prerequisites

Everything runs on your laptop; the tablet stays stock.

| Prerequisite | Why | Notes |
| --- | --- | --- |
| reMarkable 2 on software 3.x | Pages are stored in the v6 `.rm` format rmscene reads | Settings → General → About shows the version |
| Connect subscription with cloud sync on | The script reads the notebook from reMarkable Cloud | Already in place |
| A notebook named `Tasks` | The one document the script touches | Name is set in config; any lined template works |
| A test page in that notebook | Something to detect | 5–6 `[ ]` tasks, a few plain note lines, both checkbox styles |
| macOS or Linux, with git | Dev machine | Windows via WSL should work but is untested |
| Python 3.11 or newer | Runtime; 3.11 adds `tomllib` to the standard library | `python3 --version` |
| uv | Creates the virtualenv and installs dependencies | `brew install uv` or the [uv installer](https://docs.astral.sh/uv/) |
| rmapi (ddvk fork) | Cloud auth and download | A release binary from [ddvk/rmapi](https://github.com/ddvk/rmapi/releases), or `go install` from source |
| One-time code from my.remarkable.com | Registers rmapi as a desktop app on your account | Only needed once; the token is stored afterwards |
| Anthropic API key | Handwriting recognition | Exported as `ANTHROPIC_API_KEY` in the shell; the config names the variable |
| TypeSafe API key | Task classification with Jev | Exported as `TYPESAFE_API_KEY`; key from [console.typesafe.ai](https://console.typesafe.ai/) |

## Flow

`scan` fetches a fresh copy from the cloud; `analyse` runs on a copy already in the cache. From "Unpack pages" down, both paths run identical code, so detection can be tuned offline.

&#91;embedded content: read-path spike · two entry points, one analysis\]

Every step reads its settings from the one `config.toml`.

## Configuration

All settings live in one file, `config.toml`, at the repo root. `config.example.toml` is committed; `config.toml` is gitignored.

Rules:

- The CLI reads `./config.toml`, or the path in `JOTTED_CONFIG`. That is the only environment variable you ever set.
- rmapi's own settings are driven from this file. The wrapper sets `RMAPI_CONFIG` (token path) and `RMAPI_TRACE` for each rmapi call, so rmapi never reads `~/.rmapi`.
- Secrets are never pasted into the config. It points at files under `.secrets/`, created with 0700 permissions, or names the environment variable that already holds an API key (`api_key_env`).
- Detection thresholds are ratios of the line's median stroke height, not absolute units. They hold regardless of writing size or zoom.
- Later phases (recognition, server, sync) add new sections to this same file.
- `jotted config check` validates the file and prints the resolved values.

```toml
# jotted configuration: the single source of settings.
# Copy to config.toml (gitignored). Override the path with JOTTED_CONFIG.

[paths]
cache_dir   = "./cache"      # downloaded .rmdoc files and unpacked pages
output_dir  = "./out"        # reports, one timestamped folder per run
secrets_dir = "./.secrets"   # created with 0700 permissions

[rmapi]
binary     = "rmapi"                 # name on PATH, or an absolute path
token_file = "./.secrets/rmapi.conf" # passed to rmapi as RMAPI_CONFIG
timeout_s  = 120
trace      = false                   # true sets RMAPI_TRACE=1

[notebook]
name   = "Tasks"   # exact visible name in your library
folder = "/"       # where to search; "/" is the whole library
pages  = "all"     # "all", "last", or a list such as [1, 3]

[strokes]
ignore_tools = ["highlighter", "eraser", "eraser_area"]
min_points   = 2   # drop taps and dots

[lines]
band_tolerance       = 0.6  # centre within this x median height of the band joins it
min_vertical_overlap = 0.5  # or overlap of the band by this share of its own height
tall_stroke_factor   = 3.0  # taller strokes are placed after the first pass
merge_containment    = 0.6  # merge a line into another whose box holds this share of it (drawings)

[checkbox]
styles    = ["bracket_pair", "single_box"]
lead_zone = 3.0   # search this many line heights from the line's left edge
size_min  = 0.6   # checkbox height vs the line's median stroke height
size_max  = 2.5

# bracket_pair
bracket_aspect_min   = 1.5         # each bracket: height / width
bracket_height_ratio = [0.7, 1.4]  # left vs right bracket
bracket_gap          = [0.3, 2.0]  # gap between brackets, x bracket height
bracket_overlap_min  = 0.7         # vertical overlap of the two brackets

# single_box
box_aspect      = [0.6, 1.6]
box_closure_max = 0.25        # start-to-end distance / box diagonal
box_path_ratio  = [0.7, 1.5]  # stroke length / bbox perimeter

require_text   = true  # report an empty checkbox separately
min_confidence = 0.5

[output]
formats   = ["table", "json", "svg"]
svg_scale = 0.5

[logging]
level = "INFO"
```

```toml
[recognition]                    # handwriting to text, one Claude request per page
enabled     = true
model       = "claude-opus-5"
api_key_env = "ANTHROPIC_API_KEY"
effort      = "low"
timeout_s   = 120

[classification]                 # line to todo / note / heading, one Jev request per page
enabled          = true
model            = "jev-latest"
api_key_env      = "TYPESAFE_API_KEY"
todo_threshold   = 0.6    # P(todo) at or above this makes the line a task
continuation_spacing   = 0.75  # candidate wrap: this close under a line, as a share of the usual line spacing
continuation_threshold = 0.25  # merged unless Jev's P(continues) is below this
checkbox_is_task = false  # true: any line starting with a checkbox is a task, whatever Jev says
context_lines    = 2      # neighbouring lines given to Jev on each side
timeout_s        = 30
```

The threshold values are starting points to tune in the test plan, not measured values.

## Components

A small Python package with one module per flow step. Dependencies: `rmscene>=0.7`, `rich` (terminal table), `pillow` (line images), `anthropic` and `typesafe-sdk`; everything else is standard library.

```text
jotted/
├── pyproject.toml
├── config.example.toml
├── README.md
├── src/jotted/
│   ├── cli.py        # commands and argument parsing
│   ├── config.py     # load and validate config.toml
│   ├── cloud.py      # rmapi wrapper
│   ├── notebook.py   # unpack .rmdoc, page order
│   ├── strokes.py    # rmscene to Stroke objects
│   ├── lines.py      # line clustering
│   ├── checkbox.py   # geometric checkbox detection (fallback)
│   ├── recognise.py  # line images and transcription with Claude
│   ├── classify.py   # todo / note / heading with Jev
│   ├── aicache.py    # on-disk cache for both
│   └── report.py     # table, JSON, SVG
├── tests/fixtures/   # your own .rm pages
├── .secrets/         # gitignored
├── cache/            # gitignored
└── out/              # gitignored
```

| Module | Responsibility | Main interface |
| --- | --- | --- |
| `config.py` | Parse TOML with `tomllib` into frozen dataclasses; resolve relative paths against the config file; fail fast on unknown keys | `load(path) -> Config` |
| `cloud.py` | Run rmapi via `subprocess` with `RMAPI_CONFIG` set; parse `find --json`; download with `get --id` into the cache | `register()`, `find_notebook(cfg) -> DocRef`, `download(cfg, doc) -> Path` |
| `notebook.py` | Unzip the archive; read page order from `.content` (`cPages.pages`, falling back to `pages`); map page id to `.rm` file | `open_notebook(path) -> list[Page]` |
| `strokes.py` | Read blocks with rmscene; keep line items with a value (deleted strokes have none); drop ignored tools; compute bbox, path length, start/end points | `load_strokes(rm_path) -> list[Stroke]` |
| `lines.py` | Cluster strokes into lines (algorithm below) | `cluster(strokes, cfg) -> list[Line]` |
| `checkbox.py` | Classify each line's leading strokes | `detect(line, cfg) -> Checkbox or None` |
| `recognise.py` | Render each line's strokes to a PNG; send a page's uncached lines to Claude in one request; structured output `{n, checkbox, text}` per line | `transcribe(lines, cfg) -> dict[n, Transcript]` |
| `classify.py` | One Jev request per page: state holds the page's lines, one Choice question per line | `classify(transcripts, cfg) -> dict[n, Judgment]` |
| `aicache.py` | JSON files under `cache/ai/`, keyed by a hash of the stroke IDs, model and prompt version | `get(key)`, `put(key, value)` |
| `report.py` | Terminal table, `report.json`, one `page-NN.svg` overlay per page | `write(run, cfg)` |
| `cli.py` | Wire the commands together | see below |

| Command | What it does |
| --- | --- |
| `jotted auth` | Registers rmapi with a one-time code; stores the token at `rmapi.token_file` |
| `jotted config check` | Validates `config.toml` and prints resolved values |
| `jotted scan` | Cloud path: find, download, analyse, report |
| `jotted analyse <path>` | Offline path: analyse a cached `.rmdoc` or unpacked folder |
| `jotted diff <runA> <runB>` | Compares two runs' JSON; lists tasks whose anchor changed |

`Stroke` holds `id` (the rmscene CrdtId as a string), `tool`, `points`, `bbox`, `length`, `start` and `end`. `Line` holds its strokes in x order, its bbox, and its median stroke height.

## Detection algorithms

Both algorithms are pure geometry with no ML, and every threshold is relative to the median stroke height. Coordinates are rmscene's page units (an rM2 page is 1404 × 1872); only relative distances are used, so the origin doesn't matter.

### Line clustering

1. Compute the page's median stroke height `h`. Set aside strokes taller than `tall_stroke_factor × h` (long strikes, arrows, big loops).
2. Sort the remaining strokes by vertical centre and sweep top to bottom.
3. A stroke joins the current line if its centre is within `band_tolerance × h` of the line's mean centre, or it overlaps the line's band by at least `min_vertical_overlap` of its own height. Otherwise it starts a new line.
4. Assign each set-aside tall stroke to the line it overlaps most; if it overlaps none, report it as unassigned.
5. Merge a line into another when `merge_containment` or more of its box lies inside the other's box. A drawing (a box with a label and an arrow inside) is otherwise cut into several "lines"; ordinary text lines barely overlap.
6. Within each line, sort strokes by left edge and recompute the line's bbox and its own median height `lh`.

### Checkbox detection

Only the lead zone is examined: strokes whose left edge is within `lead_zone × lh` of the line's left edge.

Bracket pair `[ ]`, the first two lead strokes:

1. Each is tall and narrow: height / width ≥ `bracket_aspect_min`.
2. Their heights are similar (ratio in `bracket_height_ratio`) and they overlap vertically by at least `bracket_overlap_min`.
3. The gap between them is within `bracket_gap × bracket height`, and no other stroke's centre lies between them.
4. Height is within `size_min` to `size_max × lh`.

Single box, the first lead stroke:

1. Aspect ratio in `box_aspect` and height within `size_min` to `size_max × lh`.
2. Closed: distance from start to end point ≤ `box_closure_max × bbox diagonal`.
3. Box-like, not a scribble: path length / bbox perimeter in `box_path_ratio`.
4. No other stroke's centre lies inside it.

For either style:

- Confidence = the share of checks passed, with the size and closure checks weighted double. Lines at or above `min_confidence` are tasks.
- With `require_text` on, a checkbox with no strokes to its right is reported as `empty_checkbox`, not a task.
- The checkbox stroke IDs become the task's anchor. The strokes to the right are its text, which is recognised in a later phase.

### Recognition and classification

1. Render each line's strokes (checkbox included) to a PNG cropped to the line's bbox, about 160 px tall.
2. Send every uncached line of the page to Claude in one request, each image labelled with its line number. The structured output gives, per line: `drawing` (a diagram rather than writing), `checkbox` (`empty`, `checked` or `none`) and `text` (the words after any checkbox; a drawing's labels, joined with " → " along arrows).
3. Merge pieces of one item:
   - Drawings that touch vertically and overlap horizontally are one drawing (no model call).
   - Wrapped lines: geometry proposes a pair when the lower line sits closer than `continuation_spacing` of the page's usual line spacing, is not outdented, and starts with no checkbox or bullet. Jev is asked a Noul question (does it continue the line above?), given that layout as stated evidence. The pair is merged unless P < `continuation_threshold`. Geometry leads and Jev vetoes: on the first real page a genuine wrap scored about 0.3, separate neighbours 0.08–0.17.
   - A merged item keeps its first line's number and anchor, and reports `merged_lines`.
4. Send the page's transcripts to Jev in one request. The state is the page's written lines (`n`, `text`, `checkbox`); drawings and empty lines are left out, because with them in, Jev read them as odd blank lines and a neighbour's P(todo) fell from 0.92 to 0.45. Each line gets one Choice question, `todo`, `note` or `heading`, which may use up to `context_lines` neighbours as context.
5. Policy stays in code. A drawing is `drawing`. A line is a `task` when P(todo) ≥ `todo_threshold`, or when it has a checkbox and `checkbox_is_task` is on. A ticked checkbox makes a task `done`. A checkbox with no text is `empty_checkbox`. Everything else is `note` or `heading`.
6. The anchor is the line's first-written stroke: the lowest CrdtId among its strokes. Geometric checkbox IDs are still reported when the fallback detector finds them.
7. Transcripts and judgments are cached by a hash of the line's stroke IDs, so a line is only sent again when its strokes change.

## Output

Each run writes to `out/<YYYYMMDD-HHMMSS>/`: a terminal table, `report.json`, and one SVG overlay per page.

Terminal table, one row per line:

```text
Page 1 (7c41…)    lines: 4   tasks: 3   done: 0   empty: 0
 #  kind   p(todo)  box    text                        anchor
 1  task      0.93  empty  Check in with Adam          1:14
 2  task      0.88  empty  1-1 test                    1:32
 3  note      0.41  empty  Architecture discussion     1:45
 4  task      0.71  empty  O options                   1:74
```

`report.json` holds the same data plus geometry, for `diff` and later phases:

```json
{
  "run": "20260929-093015",
  "notebook": {"id": "…", "name": "Tasks", "version": 42},
  "pages": [{
    "index": 1, "id": "7c41…",
    "lines": [{
      "n": 1, "kind": "task", "text": "Check in with Adam",
      "checkbox": "empty", "p_todo": 0.93,
      "probabilities": {"todo": 0.93, "note": 0.05, "heading": 0.02},
      "anchor_id": "1:14",
      "stroke_ids": ["1:14", "1:15", "…"],
      "bbox": [x0, y0, x1, y1],
      "geometry": {"style": "bracket_pair", "confidence": 0.71, "checkbox_ids": ["1:14", "1:15"]}
    }],
    "unassigned_ids": []
  }]
}
```

The SVG overlay draws every stroke in grey, each line's band as a faint rectangle numbered to match the table, the transcribed text and kind under each band, and geometric checkboxes outlined (solid for tasks, dashed for empty). It is the main tuning tool: open it next to the tablet page and compare.

## Test plan and success criteria

The spike succeeds when T1–T5 pass on your own handwriting. T6 is a stretch.

| # | Test | How | Pass when |
| --- | --- | --- | --- |
| T1 | Auth and download | `jotted auth`, then `jotted scan` | The notebook lands in `cache/` and the page count matches the tablet |
| T2 | Basic detection | Page with 6 `[ ]` tasks and 4 note lines | 6 tasks, 0 false positives |
| T3 | Both styles | Page mixing bracket pairs and single boxes, 5 of each | At least 9 of 10 detected; no note line flagged |
| T3b | Tasks without a box | 4 action lines written without a checkbox, among 4 notes | At least 3 of 4 found as tasks; no note flagged |
| T3d | Drawings and wrapped lines | A diagram with labels and arrows, and a to-do that wraps onto a second line | The diagram is one `drawing` item, never a task; the wrapped to-do is one task |
| T3c | Transcription | Every line on the T2 and T3 pages | Text readable and correct apart from minor slips, in the table and JSON |
| T4 | Anchor stability | Scan; add a line mid-page on the tablet; wait for sync; scan again; `jotted diff` | Every pre-existing task keeps its anchor |
| T5 | Empty checkbox | A lone `[ ]` with nothing after it | Reported as `empty_checkbox`, not a task |
| T6 | Messy page | A real day's page with doodles, arrows and a crossed-out line | No crash; false positives are visible in the SVG and listed as a tuning follow-up |

Tuning loop: after the first `scan`, iterate with `jotted analyse` on the cached copy. Change one threshold in `config.toml` at a time and compare the SVG overlays. Once T2 and T3 pass, copy those pages' `.rm` files into `tests/fixtures/` with the expected results, so later threshold changes can't regress them.

## Risks and open questions

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Cloud API is undocumented and can change | `scan` breaks overnight | All cloud access sits in `cloud.py`; `analyse` keeps working on cached copies |
| rmapi's download format changes (archive name or layout) | Unpacking fails | `notebook.py` accepts any zip and locates `.content` and `.rm` files by extension, not fixed names |
| rmscene lags a firmware update | Unknown blocks | rmscene returns unreadable blocks instead of failing; log them and continue |
| The token file grants full access to your library | A leaked token exposes all notes | `.secrets/` is gitignored with 0700 permissions; revoke from my.remarkable.com if leaked |
| Strokes inside text-anchored groups carry offsets | Wrong positions near typed text | Keep the Tasks notebook handwriting-only for the spike; log grouped strokes |
| Handwriting variety | Missed or false tasks | Recognition reads the text instead of matching box shapes; geometric fallback; the SVG tuning loop and fixture tests |
| Notebook content leaves the laptop | Page images go to Anthropic and text to TypeSafe | Only the Tasks notebook is sent; `enabled = false` switches either step off |
| API cost and latency | Slow or costly scans | One request per page per service; cache by stroke IDs; `effort = "low"` for transcription |
| Ambiguous lines ("Architecture discussion") | Task or topic? | Jev's probability is kept; `todo_threshold` and `checkbox_is_task` are tunable |

Open questions:

- [ ] Which lined template will the Tasks notebook use? Line spacing could become a detection hint.
- [ ] Will the notebook have a page per day, or several days per page? This affects how dates are read later.
- [ ] Is the laptop the final host, or should the spike also run on the server that will host the todo app?

## After the spike

If T1–T5 pass, next come strike-through detection, then the task database and polling. The write-back spike follows those. Setup steps live in the repo's `README.md`; the template is `config.example.toml`.

Sources: [ddvk/rmapi](https://github.com/ddvk/rmapi) · [rmscene on PyPI](https://pypi.org/project/rmscene/) · [reTaskable](https://github.com/jdkruzr/reTaskable) · [Vellum package index](https://vellum.delivery/)
