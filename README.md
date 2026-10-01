# rmtasks

Turns handwriting on your reMarkable into a to-do list. It reads the notebooks you choose through reMarkable Cloud, works out which lines are tasks and whose they are, and keeps one list in a local web app. Tick items there or on the tablet.

It runs on your computer. Nothing goes through a server of ours: images of new lines go to Anthropic to be read, and their text goes to TypeSafe to be judged, each with your own API key.

## Quick start

You need a reMarkable with cloud sync (a Connect subscription), an [Anthropic API key](https://console.anthropic.com/settings/keys), a [TypeSafe](https://typesafe.ai) key, and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv tool install git+https://github.com/sameera207/rmtasks
rmtasks start
```

`rmtasks start` walks you through the rest, then opens the app in your browser:

1. It creates a folder for settings, data and keys (`~/Library/Application Support/rmtasks` on a Mac, `~/.local/share/rmtasks` on Linux).
2. It downloads [rmapi](https://github.com/ddvk/rmapi), which talks to reMarkable Cloud, and checks the download against its published checksum.
3. It connects your reMarkable with a one-time code from my.remarkable.com.
4. It asks for your API keys and checks them. They're saved in that folder, readable by your user only. Keys exported in your shell take precedence.
5. It opens the app at Settings: tick the folders whose notes should feed your list.

Run `rmtasks start` again whenever you want the app; finished steps are skipped. `rmtasks setup` goes through the steps again, to reconnect the tablet or change a key. Leave the terminal window open while you use the app.

## Development setup

To work on rmtasks itself. A `config.toml` in the folder you run from takes precedence over the app folder.

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
cp src/rmtasks/config.example.toml config.toml
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

## The common to-do list

`rmtasks serve` runs a local web app at http://127.0.0.1:8765. It collects action items from the folders you choose and keeps them in one list. It also keeps that list on the tablet as a To-do document you can tick with the pen.

1. Open **Settings**, load your folders, tick the ones to watch (for example `/Meeting Notes`), and save.
2. In the background, the app checks the tablet every minute. Only documents whose cloud copy changed are downloaded; only pages whose content changed are parsed; only lines with new strokes are sent to Claude (as images) and Jev (as text). Jev decides whether each line is an action and who owns it.
3. **To-do** shows everything: collected actions, with an image of the handwritten line and a link to its page, plus the Tasks notebook's tasks. Filter by open/done, mine/others and source. Mark "×" on a line that isn't an action.
4. Turn on **To-do document on the tablet** in Settings. Each item gets a fixed slot with a printed checkbox. Tick a box with the pen and the item is marked done on the next check.

From the command line: `rmtasks library`, `rmtasks watch add "/Meeting Notes"`, `rmtasks collect --dry-run` (what changed, nothing read), `rmtasks collect`, `rmtasks todo`.

Code layout: `rmtasks/core` holds the model, ports and services and imports no adapter. `rmtasks/adapters` holds the reMarkable library, SQLite and To-do document implementations, and the AI providers behind two ports: handwriting readers (`recognition.provider`, Anthropic today) and line judges (`classification.provider`, TypeSafe's Jev today). `rmtasks/recognise.py` and `rmtasks/classify.py` explain how to add a provider. `rmtasks/app.py` wires them together and runs the background scheduler. See `specs/Common-todo-spec.md`.

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
src/rmtasks/config.example.toml   every setting with its default; `rmtasks start` copies it
config.toml           your settings (gitignored)
src/rmtasks/          cli, config, cloud, notebook, strokes, lines, checkbox, report
tests/fixtures/       .rm pages with known expected results
.secrets/             rmapi token (gitignored)
cache/                downloaded notebooks (gitignored)
out/                  run reports (gitignored)
```
