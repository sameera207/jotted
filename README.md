# rmtasks

Reads a handwritten task notebook from a reMarkable 2 through reMarkable Cloud, transcribes each line with Claude, and asks Jev (TypeSafe) whether it is a to-do.

This is the **read-path spike**: it is read-only and never writes to the cloud or the tablet. See the spec doc for the design, detection algorithms and test plan.

## Prerequisites

- reMarkable 2 on software 3.x, with a **Connect** subscription and cloud sync switched on
- A notebook named `Tasks` (or whatever you set in `config.toml`), handwriting only
- macOS or Linux, with `git`
- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- [rmapi, ddvk fork](https://github.com/ddvk/rmapi): the original `juruen/rmapi` is archived and no longer works with the cloud
- `ANTHROPIC_API_KEY` and `TYPESAFE_API_KEY` exported in your shell (for recognition and classification). Set `enabled = false` in `[recognition]` / `[classification]` to run without them; lines are then judged by the geometric checkbox detector.

## Setup

### 1. Get the code and dependencies

```bash
git clone <your-repo-url> rmtasks
cd rmtasks
uv sync
```

### 2. Install rmapi

Download the binary for your OS from the [ddvk/rmapi releases](https://github.com/ddvk/rmapi/releases), then put it on your `PATH`:

```bash
chmod +x rmapi
mv rmapi /usr/local/bin/        # or ~/.local/bin
command -v rmapi                # should print its path
```

Or build it from source with Go:

```bash
git clone https://github.com/ddvk/rmapi && cd rmapi && go install
```

If you don't put it on your `PATH`, set `rmapi.binary` in the config to its absolute path.

### 3. Create your config

```bash
cp config.example.toml config.toml
```

Edit `config.toml`. For a first run, the only value you're likely to change is `notebook.name`. Then check it:

```bash
uv run rmtasks config check
```

Every setting lives in this one file, including rmapi's token location. You never set rmapi environment variables yourself. To keep the config somewhere else, point `RMTASKS_CONFIG` at it.

### 4. Connect to reMarkable Cloud (once)

```bash
uv run rmtasks auth
```

1. The command asks for a one-time code.
2. Sign in at my.remarkable.com and open the page for connecting a desktop app to get the code.
3. Paste the code. The token is saved to `.secrets/rmapi.conf`.

That token grants full access to your library. Never commit it. If it leaks, revoke the device on my.remarkable.com and run `auth` again.

### 5. Prepare a test page on the tablet

Create a page in the `Tasks` notebook with:

- 5–6 tasks, each starting with a `[ ]` (and a couple drawn as a single box)
- 3–4 ordinary lines of notes
- one lone `[ ]` with nothing after it

Wait for the cloud-sync icon to settle before scanning.

### 6. Scan

```bash
uv run rmtasks scan
```

This finds the notebook, downloads it into `cache/`, analyses each page and prints a table. Each run also writes:

- `out/<timestamp>/report.json`: tasks, confidence and checkbox stroke IDs
- `out/<timestamp>/page-NN.svg`: an overlay showing line bands and detected checkboxes

Open the SVG next to the tablet page to see what was detected.

Each page's lines are sent to Claude as images in one request, and the transcripts to Jev in one request. Both results are cached under `cache/ai/`, keyed by the line's stroke IDs, so re-running `analyse` on the same notebook makes no API calls. Diagrams are reported as `drawing` items, never tasks. A to-do that wraps onto a second line is merged into one item (shown as `10+11` in the table). Close spacing proposes the merge, and Jev can veto it. A line is a task when Jev's P(todo) is at least `classification.todo_threshold`; set `checkbox_is_task = true` to also treat every line that starts with a checkbox as a task.

### 7. Tune offline

Once there's a copy in the cache, iterate without touching the network:

```bash
uv run rmtasks analyse cache/<notebook-id>.rmdoc
```

`scan` prints the exact cache path. Change one threshold at a time in the `[lines]` or `[checkbox]` section of `config.toml`, re-run, and compare the overlays.

In the overlay, solid orange boxes are tasks, dashed ones are empty checkboxes, and each label lists the checks that failed. `checkbox.required_checks` names the checks a style must pass before it's scored at all. Without that gate, ordinary letters such as `ll` pass enough of the weak checks to clear `min_confidence`.

Run the tests with `uv run pytest`. They use synthetic pages, plus a fake `rmapi` for the cloud wrapper.

### 8. Check anchor stability

1. Run `scan`.
2. Add a new line somewhere mid-page on the tablet and let it sync.
3. Run `scan` again, then compare the two runs:

```bash
uv run rmtasks diff out/<first-run> out/<second-run>
```

Every task that existed in the first run should keep identical checkbox IDs.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `auth` or `scan` fails with an auth error | Delete `.secrets/rmapi.conf` and run `rmtasks auth` again |
| Need to see what rmapi is doing | Set `rmapi.trace = true` in `config.toml` |
| "Notebook not found" | `notebook.name` must match the visible name exactly; narrow `notebook.folder` if names repeat |
| Warnings about unreadable blocks | Newer firmware than rmscene knows: `uv lock --upgrade-package rmscene && uv sync` |
| Page count doesn't match the tablet | The tablet hadn't finished syncing; wait and re-run `scan` |

## Layout

```text
config.example.toml   template for the single config file
config.toml           your settings (gitignored)
src/rmtasks/          cli, config, cloud, notebook, strokes, lines, checkbox, report
tests/fixtures/       .rm pages with known expected results
.secrets/             rmapi token (gitignored)
cache/                downloaded notebooks (gitignored)
out/                  run reports (gitignored)
```
