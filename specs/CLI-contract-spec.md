# Jotted — CLI contract spec

Oct 3, 2026 · @Sam · proposed

## Purpose

The CLI comes first. `jotted` is the product's one public interface, and everything else is a wrapper around it: the desktop app (Tauri), an MCP server for Claude, Codex or other agents, shell scripts, anything someone builds later.

A wrapper only runs `jotted …` and reads JSON. It never imports Jotted's Python, reads the SQLite database, edits `config.toml`, or types into a prompt. If a wrapper needs something it can't get that way, the CLI is missing a command.

This spec sets the contract (output, errors, exit codes), the commands a wrapper needs, and the changes needed to get there from today's CLI.

## Decisions

| Question | Decision |
| --- | --- |
| Public interface | The `jotted` command with `--json`. Nothing else is promised to wrappers |
| The local HTTP API (`server.py`) | Stays, for the browser web app and for the CLI's fast path (below). Internal: not a contract, and can change with the CLI |
| Prompts | Only when stdin is a terminal and `--json` is off. Every secret or code can also come on stdin (`--stdin`) |
| `jotted auth` | Renamed `jotted connect`: it connects your reMarkable, it isn't a login. `auth` stays as a hidden alias for one release |
| Live updates | `jotted events --follow` prints one JSON line per change, read from an event log in the database. No daemon needed for it |
| Speed | When `jotted serve` is running, CLI commands are handed to it instead of starting the work themselves. Same commands, same output |
| Versioning | Every JSON output carries `"v": 1`. Only breaking changes bump it, after a release of deprecation warnings |
| Setup | Split into steps a wrapper can check and run one at a time. Source plugins contribute their own steps |
| Agents | `jotted mcp` serves the operations as MCP tools, built from the same operation registry as the CLI. Keys are never set through MCP |
| Desktop app | Tauri, in its own repo (`jotted-desktop`). It bundles a pinned `jotted` release as a sidecar and runs only `jotted` commands |
| Repos | Two: `jotted-cli` (CLI, open source) and `jotted-desktop` (the app). They share only the contract and its version |

## The output contract

### Success and failure

With `--json`, stdout carries exactly one JSON document (or, for `events --follow`, one per line), and nothing else.

```json
{"v": 1, "ok": true, "data": { … }}
```

```json
{"v": 1, "ok": false, "error": {"code": "busy", "message": "Your reMarkable is busy with another job; try again in a moment.", "retry": true}}
```

- `data` is what the operation returns today (plain dicts and lists from `api.Jotted`).
- `message` is written for the person, so a wrapper can show it as is.
- `code` is for programs: wrappers branch on it, never on `message`.
- Logs and progress go to stderr. With `--json`, progress lines are JSON too: `{"v": 1, "progress": "Reading Weekly sync, page 3"}`.

Today `--json` prints the bare result, and errors as `{"error", "status"}`. Both change to the envelope.

### Error codes and exit codes

| `code` | Exit | Meaning | Today |
| --- | --- | --- | --- |
| — | 0 | Done | |
| `invalid` | 1 | Bad input: unknown item, bad setting value | `Invalid` |
| `not_found` | 1 | No such item, document or page | `NotFound` |
| `conflict` | 1 | Can't do that in this state | `Conflict` |
| `usage` | 2 | Bad arguments | argparse exits 2 |
| `config` | 2 | `config.toml` is invalid | `ConfigError`, exit 2 |
| `not_set_up` | 3 | A setup step is missing; `error.step` names it | new |
| `busy` | 4 | Another job holds the device; `retry: true` | `Busy`, exit 1 |
| `not_connected` | 5 | The device or its cloud can't be reached, or the connection was revoked | `SourceError`, exit 1 |
| `model_error` | 6 | No key, a rejected key, or the model failed | `ModelError`, exit 1 |
| `interrupted` | 130 | Ctrl+C | 130 |

`ApiError` and its subclasses get a `code` attribute. `cli.main` builds the envelope from it and maps exceptions without one through the table above.

### Values

- IDs are stable: an item keeps its `id` for life.
- Times are ISO 8601 in UTC (`2026-10-03T04:10:00Z`).
- Paths are the device's own (`/Meeting Notes/Weekly sync`).
- Fields are only ever added within a version. A wrapper ignores fields it doesn't know.

## Commands

Status: **same** (exists, only the envelope changes), **change**, **new**.

### Setting up

| Command | Does | Status |
| --- | --- | --- |
| `setup status` | Every setup step, done or not, in order (example below) | new |
| `setup prepare` | Make the app folder and download rmapi (checksum checked). The reMarkable plugin's step | new (split out of `setup`) |
| `connect [--stdin] [--replace]` | Connect your reMarkable with a one-time code. Fails with `conflict` if already connected, unless `--replace` | change (was `auth`; no `input()`/`getpass` when `--stdin`) |
| `ai` | The language model (provider, model, key set or not) and the Jev plugin | same |
| `ai provider NAME` | Choose the LLM adapter: `anthropic` today, `openai` once its adapter exists | new |
| `ai model NAME` | Choose the model; it has to read images | new |
| `ai key llm\|jev [--stdin]` | Check a key with its provider and save it | same |
| `ai remove jev` | Turn the Jev plugin off | same |
| `plugins` | Installed source plugins, and which one is chosen | new |
| `setup` | The interactive walkthrough, for people in a terminal: runs the steps above in order | change (built on the steps) |

`setup status --json`:

```json
{"v": 1, "ok": true, "data": {"complete": false, "steps": [
  {"id": "app_folder", "done": true},
  {"id": "remarkable.rmapi", "done": true, "detail": "checksum verified"},
  {"id": "remarkable.connect", "done": false, "command": "connect --stdin"},
  {"id": "llm", "done": false, "provider": "anthropic", "command": "ai key llm --stdin"},
  {"id": "jev", "done": false, "optional": true, "command": "ai key jev --stdin"},
  {"id": "folders", "done": false, "command": "watch add PATH"}
]}}
```

Each step names the command that completes it, so a wrapper can walk setup without knowing the steps in advance. Steps a source plugin adds are prefixed with its name.

### The to-do list

| Command | Status |
| --- | --- |
| `items [list] [--status open\|done\|all] [--owner mine\|others] [--folder F]` | same |
| `items add TEXT` / `edit ID TEXT` / `done ID` / `reopen ID` / `dismiss ID` | same |

### What is read

| Command | Status |
| --- | --- |
| `library` | same |
| `watch add\|remove\|from-now\|read-all PATH` | same |
| `settings` / `settings set KEY VALUE` | same |

### Doing the work now

| Command | Status |
| --- | --- |
| `collect [--dry-run]` | same |
| `todo [--force]` | same |
| `check` | same |
| `status` | same |

### Where an item came from

| Command | Status |
| --- | --- |
| `image page DOC PAGE [--anchor A] [-o FILE]` | same |
| `image line DOC ANCHOR [-o FILE]` | same |

With `--json` and no `-o`, the SVG goes in `data.svg`.

### Live updates

| Command | Does | Status |
| --- | --- | --- |
| `events [--since CURSOR] [--follow]` | Changes since a cursor; `--follow` keeps printing them as they happen | new |

```json
{"v": 1, "cursor": 1842, "at": "2026-10-03T04:12:09Z", "type": "item.added", "item": { … }}
{"v": 1, "cursor": 1843, "at": "2026-10-03T04:12:09Z", "type": "check.finished", "new": 2, "updated": 0, "missing": 0}
```

Types: `item.added`, `item.changed` (edited, done, reopened, from the app or a tick on the tablet), `item.removed`, `check.started`, `check.finished`, `todo.published`, `settings.changed`, `source.error`.

Every operation that changes something writes an event to an `events` table in the same transaction as the change. `events --follow` reads that table every second or so (one indexed query), so it sees changes made by any process: `jotted serve`, a CLI command, an MCP call. A wrapper keeps the last `cursor` and passes it as `--since` after a restart, so it misses nothing. Old events are pruned after a week.

### Running and the rest

| Command | Does | Status |
| --- | --- | --- |
| `serve [--no-browser] [--port 0]` | Background checking, the web app, and the CLI's fast path. `--port 0` picks a free port | change (`--no-browser`, port 0, writes `serve.json`) |
| `start` | For people: set up what's missing, then open the web app | same |
| `update` | Update from GitHub. Off in a bundled build (`JOTTED_BUNDLED=1`): the app updates it | change |
| `version` | Release version, contract version and the oldest one still accepted (`contract`, `contract_min`), source plugin, Python | new |
| `schema` | Every command: its arguments, options and the shape of `data` | new |
| `mcp` | MCP server on stdio | new |
| `config check` | same | same |

## The fast path

Starting Python and importing the Anthropic SDK, Flask and reportlab takes about a second per command, which is fine for scripts and agents but slow for a click in the app.

1. `jotted serve` writes `<data_dir>/serve.json`: `{"pid", "port", "token", "version"}`, readable by your user only. It removes the file when it stops.
2. A CLI command checks for that file. If the server is alive and on the same version, the command is sent to it (`POST /api/op/<operation>` with the arguments and the token) and its envelope is printed as is.
3. Otherwise the command runs in process, as today. `--local` forces that.
4. Commands import the heavy libraries only when they need them, so the in-process path starts faster too.

Either way the output, errors and exit codes are identical; a test runs the command suite both ways and compares.

## MCP and agents

`jotted mcp` is a stdio MCP server. Its tools come from the operation registry (`api.OPERATIONS`) and `jotted schema`, so a new operation becomes a tool without extra work:

- Tools: `items_list`, `items_add`, `items_edit`, `items_done`, `items_reopen`, `items_dismiss`, `library`, `watch`, `settings_get`, `settings_set`, `check`, `status`, `setup_status`.
- Not exposed: `ai key`, `connect` and anything else that takes a secret. Keys don't pass through an agent. `setup_status` tells the agent which command the person should run.
- Each tool runs the same code as the CLI command and returns its `data`, or its `error` as an MCP tool error.

For Codex, Claude Code and other agents with a shell, a short skill that says "run `jotted --json …`, see `jotted schema`" may be enough without MCP.

## The desktop app on the contract

The Tauri app bundles a pinned `jotted` release as a sidecar (see Two repos) and does everything through it:

1. **Open:** `jotted --json version`; stop with an update message if its contract isn't one the app supports. Then `jotted --json setup status`. If not complete, show the Welcome and setup screens.
2. **Setup screens:** each one runs its step's command: `connect --stdin` with the one-time code, `ai provider`, `ai key llm --stdin`, `ai key jev --stdin`, `watch add`.
3. **Run:** start `jotted serve --no-browser --port 0` and `jotted --json events --follow`, both kept running while the app is open (or in the tray).
4. **Screens:** the To-do window runs `items`, `items done ID` and so on. "Where it came from" runs `image page` with the item's anchor. Notebooks runs `library` and `watch`. Settings runs `settings`, `ai` and `settings set`.
5. **Updates:** `events` lines update the window and the tray badge.

## Two repos: versioning and releases

The CLI and the desktop app live in separate repos and ship on their own schedules. The contract is the only thing they share.

| Repo | Holds | Licence |
| --- | --- | --- |
| `jotted-cli` (this one) | The engine, the CLI, plugins, `jotted mcp`, the browser web app | Open source |
| `jotted-desktop` | The Tauri app: screens, tray, setup flow, packaging, signing, its updater | Separate; can be closed |

### What the CLI repo publishes

Each tagged release (`v0.4.0`) publishes:

- A standalone build per OS with Python inside, so the app needs no Python of its own: `jotted-<version>-macos-arm64`, `-macos-x64`, later `-windows-x64`. Each one has a SHA-256 checksum next to it.
- `schema.json`: the output of `jotted --json schema` for that release.
- Release notes that list contract changes on their own: added commands or fields, and anything deprecated.

The `uv tool install` route keeps working for people using the CLI directly.

### Version numbers

- **The release version** (`0.4.0`) follows semver for the CLI as a product.
- **The contract version** (`"v": 1` in every output) changes only when a wrapper would break: a removed or renamed command, option or field, or a changed meaning. Adding a command, option, field, event type or error code doesn't change it.
- `jotted version --json` reports both, plus the oldest contract version the build still accepts: `{"version": "0.4.0", "contract": 1, "contract_min": 1}`.
- Deprecation: anything to be removed keeps working for at least one release and prints a warning to stderr. Removing it is what bumps `v`.

### How the app uses a release

- `jotted-desktop` pins one CLI release in a file (`jotted.lock`: version and checksums). CI downloads that build, checks the checksum, and puts it inside the app as a Tauri sidecar.
- When the app starts, it runs `jotted --json version`. If `contract` isn't one it supports, it shows "This version of Jotted needs updating" instead of half-working.
- The app updates the CLI by shipping a new app version with a new pin, through its own updater. The bundled CLI never updates itself: `selfupdate.py` is off when `JOTTED_BUNDLED=1`, which the app sets.
- For development, `JOTTED_BIN=/path/to/jotted` makes the app use a local build instead of the pinned one.

### Changing the contract

1. Add the command or field in `jotted`, with its tests, and release.
2. In `jotted-desktop`, bump the pin, refresh the recorded outputs (below), and use it.

Because additions don't break older wrappers, the two never need to land together. A `v` bump does: the CLI ships the new contract version while still accepting the old one (`contract_min`) for a release, and the app moves within that window.

### Testing across the line

- **In `jotted`:** the contract tests from this spec (envelope, error codes, no prompts under `--json`, both paths agree), plus a check that `schema.json` only changed in allowed ways unless `v` was bumped.
- **In `jotted-desktop`:** a fake `jotted` that answers from recorded outputs, so screens can be built and tested with no tablet, key or Python. The outputs are recorded from the real pinned CLI against a test fixture library (the synthetic pages in `tests/`), never written by hand, so they can't drift from what it really prints.
- **Across both:** a nightly job in `jotted-desktop` runs the app's main flows against the real pinned build, and against `jotted`'s `main`, to catch a breaking change before it's released.

### Signing and packaging

The app repo signs and notarizes the whole bundle, the sidecar included (macOS needs every executable inside signed). The CLI repo's builds are signed too, so people downloading them directly get no warnings.

## Changes from today

1. **Envelope, codes and version.** `code` on `ApiError` and the other errors; `cli.main` prints the envelope and returns the exit codes above; `version`.
2. **`connect`.** Rename `auth` (keep a hidden alias). `--stdin` and `--replace`; no `input()` in `plugins/remarkable/setup.py` when either is given.
3. **Setup as steps.** Split `onboarding.run` into steps with a check and a run. The plugin interface gains `setup_steps()` and loses the terminal-bound `setup(ui, redo)`, which becomes the walkthrough on top of the steps. Add `setup status` and `setup prepare`.
4. **Provider and model.** `ai provider` and `ai model`, saved with the other settings in the database and overriding `config.toml`'s defaults.
5. **Events.** The `events` table, written by the repository in the same transaction as each change; `events`.
6. **Fast path.** `serve.json`, `/api/op/<operation>`, `--local`, lazy imports.
7. **`schema`**, built from argparse and the operation registry.
8. **`mcp`.**
9. **Release builds.** A standalone build per OS and `schema.json` on each tagged release; `version --json` with `contract` and `contract_min`; `JOTTED_BUNDLED` turns off self-update.
10. **Tests.** Extend the architecture test: every operation appears in `schema`; every command run with stdin closed and `--json` either succeeds or fails with a code, never waiting on a prompt; every output matches the envelope; the fast path and the in-process path give the same results.

## Stages

1. Contract: changes 1, 2, 7, and the envelope and prompt tests.
2. Release builds: change 9, so the app repo has something to pin.
3. Setup as steps: changes 3 and 4.
4. Events: change 5.
5. Fast path: change 6.
6. MCP: change 8.
7. `jotted-desktop`, against stages 1–5. It can start after stage 2, building screens on the fake CLI.

## Open questions

- **OpenAI.** `ai provider openai` needs an `adapters/openai_llm.py` that implements `read()` and the judge methods; the prompts are already shared.
- **The browser web app.** Keep it next to the desktop app, or retire it once the desktop app ships?
- **Building the standalone CLI.** PyInstaller, Nuitka or a bundled `uv` environment: size, start-up time and how well each signs.
- **Windows.** rmapi's Windows binary, the app folder location, and file permissions for the keys and `serve.json`.
