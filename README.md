# Jotted

Turns your handwritten notes into a to-do list. It works with reMarkable today. It reads the notebooks you choose through reMarkable Cloud, works out which lines are tasks and whose they are, and keeps one list in a local web app. Tick items there or on the tablet.

It runs on your computer. Nothing goes through a server of ours: images of new lines go to your language model (Claude, from Anthropic) to be read and judged, with your own API key. The optional Jev plugin, from TypeSafe, can do the judging instead.

## Quick start

You need a reMarkable with cloud sync (a Connect subscription), an [Anthropic API key](https://console.anthropic.com/settings/keys), and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv tool install git+https://github.com/sameera207/jotted
jotted start
```

`jotted start` walks you through the rest, then opens the app in your browser:

1. It creates a folder for settings, data and keys (`~/Library/Application Support/jotted` on a Mac, `~/.local/share/jotted` on Linux).
2. It downloads [rmapi](https://github.com/ddvk/rmapi), which talks to reMarkable Cloud, and checks the download against its published checksum.
3. It connects your reMarkable with a one-time code from my.remarkable.com.
4. It asks for your API keys and checks them. They're saved in that folder, readable by your user only. Keys exported in your shell take precedence.
5. It opens the app at Settings: tick the folders whose notes should feed your list.

Run `jotted start` again whenever you want the app; finished steps are skipped. `jotted setup` goes through the steps again, to reconnect the tablet or change a key. Leave the terminal window open while you use the app.

### Updates

`jotted start` checks GitHub first. When `main` has moved on since you installed, it runs `uv tool upgrade jotted` and starts again in the new version. If GitHub can't be reached, or the upgrade fails, it carries on with the version you have.

- An app that is already running keeps its old code: stop it with Ctrl+C, then `jotted start` again.
- `jotted update` updates without starting the app.
- `jotted start --no-update`, or `JOTTED_NO_UPDATE=1`, skips the check.

Only an install from GitHub updates itself. A checkout run with `uv run`, or an install pinned to a tag or commit (`git+…@v1`), never does.

So shipping a fix is: commit it to `main` and push. Each install picks it up the next time it starts.

## Development setup

To work on Jotted itself. A `config.toml` in the folder you run from takes precedence over the app folder.

### 1. Get the code and dependencies

```bash
git clone <your-repo-url> jotted
cd jotted
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
cp src/jotted/config.example.toml config.toml
```

Edit `config.toml` if you need to (the defaults work for a first run), then check it:

```bash
uv run jotted config check
```

Every setting lives in this one file, including rmapi's token location. You never set rmapi environment variables yourself. To keep the config somewhere else, point `JOTTED_CONFIG` at it.

### 4. Connect to reMarkable Cloud (once)

```bash
uv run jotted auth
```

1. The command asks for a one-time code.
2. Sign in at my.remarkable.com and open the page for connecting a desktop app to get the code.
3. Paste the code. The token is saved to `.secrets/rmapi.conf`.

That token grants full access to your library. Never commit it. If it leaks, revoke the device on my.remarkable.com and run `auth` again.

### 5. Run it

```bash
uv run jotted serve
```

Then open http://127.0.0.1:8765, tick a folder in Settings, and write a line like "book the retro room" in a document in it. `uv run jotted collect --dry-run` shows what changed without reading anything, and `uv run jotted collect` reads it.

Each page's new lines are sent to the LLM as images in one request, and their transcripts to the judge (the LLM, or Jev when it's on) in one request. Both results are cached under `cache/ai/`, keyed by the line's stroke IDs, so reading a page again makes no API calls. Diagrams are never actions. A line that wraps onto a second line is merged into one item: close spacing proposes the merge, and the judge can veto it.

Run the tests with `uv run pytest`. They use synthetic pages, plus a fake `rmapi` for the cloud wrapper.

## The common to-do list

`jotted serve` runs a local web app at http://127.0.0.1:8765. It collects action items from the folders you choose and keeps them in one list. It also keeps that list on the tablet as a To-do document you can tick with the pen.

1. Open **Settings**, load your folders, tick the ones to watch (for example `/Meeting Notes`), and save.
2. In the background, the app checks the tablet every minute. Only documents whose cloud copy changed are downloaded; only pages whose content changed are parsed; only lines with new strokes are read. The judge decides whether each line is an action and who owns it.
3. **To-do** shows everything: collected actions, with an image of the handwritten line and a link to its page, and items you type into the empty line at the bottom. Filter by open/done, mine/others and source. Mark "×" on a line that isn't an action, or to delete an item you typed.
4. Turn on **To-do document on the tablet** in Settings. Each item gets a fixed slot with a printed checkbox. Tick a box with the pen and the item is marked done on the next check. Write in an empty row and it becomes a new item. The document has two pages. When its rows run out, Jotted deletes it and prints a fresh one with only the open items.

There used to be a separate Tasks notebook as well. It's gone: write tasks in any watched notebook, in an empty row of the To-do document, or in the web app. The first time a new version starts, the old notebook's tasks become items on the list and keep their rows on the To-do document. To keep using that notebook, watch it like any other; its lines match the items they already became.

### The language model and plugins

Settings shows the **language model**: its adapter, model and key. It reads handwriting, and judges which lines are actions unless a plugin does. You can replace its key there; the adapter and model are set in `config.toml` (`[llm] provider` and `model`).

**Jev**, from TypeSafe, is an optional plugin. Add its key in Settings (or during `jotted setup`) and Jev judges actions and owners instead, with its own threshold slider. Remove the key and the LLM judges again. Keys typed in Settings are checked with the provider first and saved like the ones from setup; a key exported in your shell wins, and can only be removed there.

From the command line: `jotted library`, `jotted watch add "/Meeting Notes"`, `jotted collect --dry-run` (what changed, nothing read), `jotted collect`, `jotted todo`.

Code layout: `jotted/core` holds the model, ports and services and imports no adapter. `jotted/adapters` holds the reMarkable library, SQLite and To-do document implementations, and the AI providers. `jotted/llm.py` is the LLM port: one model that reads handwriting and judges lines (`llm.provider`; Claude in `adapters/anthropic_llm.py` today). It explains how to add a provider. Jev (`adapters/typesafe_judge.py`) implements only the judging half of that port, and `classify.judge_for` picks it while its key is set. `jotted/app.py` wires them together and runs the background scheduler. See `specs/Common-todo-spec.md`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `auth` or `collect` fails with an auth error | Delete `.secrets/rmapi.conf` and run `jotted auth` again |
| Need to see what rmapi is doing | Set `rmapi.trace = true` in `config.toml` |
| Warnings about unreadable blocks | Newer firmware than rmscene knows: `uv lock --upgrade-package rmscene && uv sync` |
| A page's latest writing is missing | The tablet hadn't finished syncing; wait for the next check, or click **Check now** |

## Layout

```text
src/jotted/config.example.toml   every setting with its default; `jotted start` copies it
config.toml           your settings (gitignored)
src/jotted/          cli, config, cloud, strokes, lines, recognise, classify, app, server
src/jotted/core/     model, ports and services (imports no adapter)
src/jotted/adapters/ reMarkable library, To-do document, SQLite, AI providers
.secrets/             rmapi token (gitignored)
cache/                downloaded documents (gitignored)
```
