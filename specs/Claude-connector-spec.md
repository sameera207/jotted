# Claude connector: CLI contract changes

Status: draft v0.1
Target repo: `jotted-cli` (this file belongs in `specs/`)
Related: `docs/cli-contract.md`, `docs/schema.json`, `specs/Common-todo-spec.md`

> Drafted from the README only. Before implementing, reconcile names, envelope details and the
> `--json` output of `image` commands with `docs/cli-contract.md` and `docs/schema.json`, which
> this draft has not seen. Anything marked **Verify** is an assumption.

## 1. Goal

Let an agent app (Claude Desktop first; Codex, Cursor and others later) read and edit the Jotted
to-do list through `jotted mcp`, so a person can:

- see the list inside the agent app, including the handwriting each item came from;
- add, edit, tick, reopen and dismiss items from there;
- have the agent read other sources through its own connectors (Drive, Gmail, Confluence, Calendar)
  and hand Jotted the action items it found, without flooding the list or the tablet.

Everything stays local. Nothing in this spec adds a network listener, an account or a hosted service.

## 2. Principles

1. The CLI stays the one public interface. Every capability below is a `jotted` command first;
   `jotted mcp`, the web app and the desktop app are thin callers of `api.Jotted`.
2. Changes are additive. Envelope `v: 1` is unchanged; existing fields keep their meaning; clients
   that ignore unknown fields keep working.
3. Keys never go through MCP (unchanged).
4. Content that an agent read from another document is data, never instructions. Jotted stores it,
   shows it with its source, and never acts on it.
5. A person decides what reaches the tablet. Items an agent inferred wait for acceptance.

## 3. Data model changes

### 3.1 Item fields (added)

| Field | Type | Notes |
| --- | --- | --- |
| `origin` | `handwritten` / `typed` / `tablet` / `agent` | How the item arrived. Set once, never edited. |
| `source` | object or null | Where the item came from. See 3.2. |
| `page` | object or null | Handwriting location, only for `origin: handwritten`. See 3.3. |
| `owner_name` | string or null | Display name when `owner` is `others`. Max 80 chars. |

`owner` keeps its current values (`mine`, `others`). `status` gains `proposed` (3.4).

### 3.2 `source`

```json
{
  "kind": "gdoc",
  "key": "1AbC...#h.x7q",
  "title": "Platform sync, 29 Sep",
  "url": "https://docs.google.com/document/d/1AbC...",
  "excerpt": "...Sam to send the revised estimate to Dana by Friday."
}
```

| Field | Rules |
| --- | --- |
| `kind` | Slug `[a-z0-9_-]{1,32}`. Known values: `remarkable`, `gdoc`, `gmail`, `confluence`, `jira`, `gcal`, `chat`, `other`. Unknown slugs are accepted. |
| `key` | Stable identifier of the line, message or ticket the item came from. Max 200 chars. Required for `origin: agent`; optional otherwise. |
| `title` | Max 200 chars. |
| `url` | `https` only. Anything else is rejected with `invalid`. Never fetched by Jotted. |
| `excerpt` | The text the item was taken from. Max 500 chars, control characters stripped. Rendered as plain text everywhere. |

`(kind, key)` is unique across all items, including dismissed ones (3.5).

Handwritten items get `source.kind = "remarkable"` and `source.key = "<doc_id>:<anchor>"` by
migration, so one dedup rule covers every origin.

### 3.3 `page`

```json
{ "doc_id": "...", "doc_name": "Retro prep", "page": 2, "page_count": 5, "anchor": "..." }
```

A reference, not an image. Clients fetch the image separately (section 4.7). `page_count` is new;
it lets a UI show "page 2 of 5".

### 3.4 Status lifecycle

```
          add (agent, inferred)                accept
 (none) ------------------------> proposed ------------> open <--> done
                                      |                    |
                                      +-------- dismiss ---+--> dismissed
```

- `proposed` items are not published to the To-do document on the tablet, are not counted as open,
  and never take a row.
- `accept` moves `proposed` to `open`. The next `todo` publish picks the item up as usual.
- `dismiss` works from `proposed` and `open`. `reopen` works from `done` only.
- `items --status all` keeps its current meaning (open and done). `proposed` and `dismissed` are
  listed only when asked for explicitly, or with `--status any`.

### 3.5 Dismissal memory

An item with a `source.key` is kept as a tombstone (`status: dismissed`) when dismissed, so an agent
that re-reads the same document next week does not bring it back. Items without a `source.key` may
be hard-deleted as today. Tombstones are hidden from every list except `--status dismissed|any`.

### 3.6 Storage migration

SQLite migration adds columns for `origin`, `source_kind`, `source_key`, `source_title`,
`source_url`, `excerpt`, `owner_name`, `page_count`, and a unique index on
`(source_kind, source_key)` where `source_key` is not null. Existing rows are backfilled with
`origin` (`handwritten`, `typed` or `tablet`, from what is known) and a `remarkable` source key
where an anchor exists. The migration must be re-runnable.

## 4. Commands

### 4.1 `jotted items add` (extended)

```
jotted items add TEXT
  [--owner mine|others] [--owner-name NAME]
  [--source-kind KIND --source-key KEY] [--source-title T] [--source-url U] [--excerpt X]
  [--propose]
```

- Without `--propose` the item is created `open`.
- With `--propose` it is created `proposed`.
- If `--source-key` is given the call is an upsert on `(kind, key)`:
  - no match: create, `created: true`;
  - match, `proposed`, different text: update the text, `created: false`;
  - match, `open` or `done`: change nothing, `created: false`;
  - match, `dismissed`: change nothing, `created: false`, `dismissed: true`.
- `origin` is `agent` when a source kind other than `remarkable` is given or the call comes from
  `jotted mcp`; otherwise `typed`.

```json
{"v":1,"ok":true,"data":{"created":true,"dismissed":false,"item":{ "...": "..." }}}
```

Limits: `TEXT` 1 to 500 chars. Violations return `invalid` with the field named in `error.message`.

### 4.2 `jotted items add-batch`

```
jotted items add-batch --stdin [--propose]
```

stdin is a JSON array of up to 100 objects with the same fields as `items add`
(`text`, `owner`, `owner_name`, `source`, `propose`). Items are independent: a bad item does not
stop the others.

```json
{"v":1,"ok":true,"data":{"results":[
  {"index":0,"outcome":"created","id":"itm_..."},
  {"index":1,"outcome":"existing","id":"itm_..."},
  {"index":2,"outcome":"dismissed","id":"itm_..."},
  {"index":3,"outcome":"invalid","error":{"code":"invalid","message":"text is empty"}}
]}}
```

More than 100 items, or malformed JSON overall, fails the whole call with `invalid`.

### 4.3 `jotted items` (list, extended)

New filters: `--source-kind K`, `--source-key KEY`, `--query TEXT` (case-insensitive substring over
text and source title), `--limit N` (default 50, max 200), `--cursor C`. `--status` accepts
`open|done|all|proposed|dismissed|any`.

The result gains `next_cursor` (null when finished). An agent checks for duplicates by listing
with `--source-key` before it adds anything it is unsure about.

### 4.4 `jotted items get ID`

Returns one item with every field in 3.1 to 3.3. `not_found` if absent.

### 4.5 `jotted items accept`

```
jotted items accept ID [ID ...]
jotted items accept --all [--source-kind K]
```

Moves `proposed` items to `open`. Returns `accepted: [ids]` and `skipped: [{id, reason}]`. An item
that is not `proposed` is skipped with reason `not_proposed`; an unknown id is skipped with
`not_found`. The call succeeds if at least one id was valid. If every id was invalid it fails with
`not_found` or `conflict`.

### 4.6 `jotted items edit` (extended)

Adds `--owner` and `--owner-name`. Text edits are allowed in every status except `dismissed`.

### 4.7 `jotted image page` (extended)

```
jotted image page DOC PAGE [--highlight ANCHOR] [--width PX]
```

`--highlight` marks one line on the page (the item's `anchor`). Output stays SVG. **Verify:** under
`--json`, `data` is `{"mime":"image/svg+xml","svg":"<svg...>"}`; match whatever `image line` uses today.

### 4.8 `jotted claude connect | status | disconnect`

Wires `jotted mcp` into Claude Desktop. Both audiences use it: technical users run it in a terminal,
and the desktop app calls it from its "Connect to Claude Desktop" button.

```
jotted claude connect [--command PATH] [--admin] [--dry-run]
jotted claude status
jotted claude disconnect
```

`connect`:

1. Locates Claude Desktop's config file. macOS:
   `~/Library/Application Support/Claude/claude_desktop_config.json`. Windows (later):
   `%APPDATA%\Claude\claude_desktop_config.json`. **Verify** both paths.
2. Reads it. If it is not valid JSON, fails with `config` and writes nothing.
3. Copies it to `claude_desktop_config.json.bak-<timestamp>`.
4. Sets `mcpServers.jotted = { "command": <absolute path>, "args": ["mcp"] }` (adds `"--admin"` with
   `--admin`), leaving every other key and server untouched.
5. Writes atomically (temp file, then rename).

`--command PATH` is for the desktop app, which passes its bundled binary. Without it the command
uses the absolute path of the running `jotted`. The path must exist and be executable, else `invalid`.

```json
{"v":1,"ok":true,"data":{"changed":true,"config_path":"...","backup_path":"...","restart_required":true}}
```

Running it again with the same inputs returns `changed: false` and writes no backup.

`status` returns `configured`, `config_path`, `command`, `command_exists` (false when the path is
stale after a move or reinstall) and `matches_current` (the entry points at this binary). The app
uses it to show Connected, Needs repair or Not connected.

`disconnect` removes only `mcpServers.jotted`, with a backup. It never touches other servers.

This code lives in `jotted/integrations/claude_desktop.py`. It imports no plugin or adapter, and
`core/` knows nothing about it, so a later `jotted codex connect` can follow the same shape.

## 5. Events

`jotted events` gains:

| Event | When |
| --- | --- |
| `item.proposed` | A proposed item is created or its text is updated. |
| `item.accepted` | `accept` moves it to `open`. |

Existing item events carry `origin` and `status` in their payload. Cursors work as before.

## 6. Tablet publishing

- `proposed` and `dismissed` items are never printed on the To-do document.
- Accepting does not publish by itself. The next `todo` run (background check or `jotted todo`)
  places the item in the next free slot.
- The app should warn when open items exceed the document's capacity (two pages). This is a hint
  only; it does not block `accept`.

## 7. Settings

| Key | Values | Default | Meaning |
| --- | --- | --- | --- |
| `mcp_add_mode` | `auto`, `propose_all` | `auto` | `auto`: items an agent adds on a direct request go straight to `open`; items it inferred go to `proposed`. `propose_all`: every agent-added item is `proposed`. |
| `proposed_limit` | integer 10 to 500 | 200 | Max pending proposed items. At the limit, adds fail with `conflict` ("too many proposed items; accept or dismiss some"). |

## 8. MCP surface (`jotted mcp`)

Names are the target semantics; keep existing tool names where they already exist.

Default tool set:

| Tool | Maps to | Hints |
| --- | --- | --- |
| `list_items` | `items` | read-only |
| `get_item` | `items get` | read-only |
| `add_item` | `items add` | Use only when the person asked for this item. Creates `open` (or `proposed` when `mcp_add_mode = propose_all`). |
| `propose_items` | `items add-batch --propose` | Use for anything inferred from another document or message. Always creates `proposed`. Requires `source.kind` and `source.key` on every item. |
| `update_item` | `items edit` | idempotent |
| `complete_item` / `reopen_item` | `items done` / `reopen` | idempotent |
| `dismiss_item` | `items dismiss` | destructive hint set |
| `accept_items` | `items accept` | Describe it as "only after the person has reviewed the proposals". |
| `get_status` | `status` | read-only |
| `show_list` | opens the widget (see the widget spec) | read-only. Visible to the model. Returns a short text summary plus the first page of items; has `_meta.ui.resourceUri = ui://jotted/list`. |
| `get_changes` | `events --since CURSOR` (one-shot, no `--follow`) | read-only, app-only. Returns changes and a new cursor so the widget can poll cheaply. |

Admin set, only with `jotted mcp --admin`: settings, watch, library, collect, check, setup. Off by
default, so a prompt injection in a shared document cannot change what Jotted watches.

Tool descriptions must make the add/propose split obvious, because that is what makes the model
pick the right tool. Tool results sent to the model contain text fields only: `id`, `text`,
`status`, `owner`, `owner_name`, `origin`, `source` (with `excerpt`) and `page` references. They
never contain images.

`get_page_image` (the `image page` operation) is for the app UI only. **Verify:** MCP Apps tool
visibility (`_meta.ui.visibility`, app-only) works the way the current spec says in Claude Desktop;
if not, the image tool must still be hidden from the model's context by another route.

The inline widget (`ui://jotted/list`) is specified in `specs/Claude-widget-spec.md`. It is plain
HTML, CSS and JavaScript shipped as static files inside the Python package and served by
`jotted mcp`; there is no Node toolchain in this repo. It depends on 3.1 to 3.5, 4.3, 4.5, 4.7 and
the tools above.

## 9. Errors

No new error codes or exit statuses.

| Situation | Code |
| --- | --- |
| Bad field, over a length limit, non-https URL, batch over 100 | `invalid` |
| Unknown id | `not_found` |
| `accept` on an item that is not proposed (single id), proposed limit reached | `conflict` |
| Claude Desktop config is not valid JSON, config path not found | `config` |
| A tool is called before setup is complete | `not_set_up`, with `error.step`, as today |
| Another job holds the device | `busy`, as today |

For `not_set_up`, the MCP layer should word the message for a person ("Open Jotted and finish
connecting your reMarkable"), since the agent will relay it.

## 10. Security and privacy

- Local only. `jotted mcp` stays stdio. No listener is added.
- Agent-supplied text (`text`, `excerpt`, `title`, `owner_name`) is length-limited, stripped of
  control characters, stored as plain text and rendered as text everywhere, including the tablet PDF.
- Source URLs must be `https` and are never fetched or followed by Jotted.
- Only `propose_items` can be called on inferred content, and it cannot reach the tablet without
  `accept`.
- Dismissed source keys stay remembered (3.5), so a repeated injection cannot keep re-adding an
  item the person already rejected.
- `claude connect` only edits the `jotted` key in Claude Desktop's config, with a backup first.
- Handwriting images never travel in tool results to the model.

## 11. Compatibility and versioning

- Envelope `v` stays 1. The contract version increments (minor); `jotted version` reports it.
- `docs/cli-contract.md` and `docs/schema.json` are regenerated with `scripts/contract_docs.py`.
- Existing scripts that call `items`, `items add TEXT`, `edit`, `done`, `reopen` and `dismiss`
  without the new flags behave as before. `--status all` is unchanged.

## 12. Implementation notes

- Add the operations to `api.Jotted` first, then expose them in `cli.py` and `mcp.py`. The existing
  test that every operation is a CLI command should cover them.
- `server.py` and the web app should show `proposed` items with accept and dismiss controls. That
  can follow the CLI work; the CLI contract does not depend on it.
- `core/` gains `Item.origin`, `Item.source` and the status transitions. It imports no plugin.
- `fastpath.py` needs no change: the new commands are handed to a running `jotted serve` like the rest.
- The widget files in `src/jotted/mcp_ui/` must be listed as package data in `pyproject.toml` so
  `uv tool install git+...` includes them. **Verify** against the current build backend config.

## 13. Acceptance tests

1. `items add --source-key K` twice returns `created: true` then `created: false`, with one item.
2. After `dismiss`, `items add --source-key K` returns `dismissed: true` and no new item.
3. `add-batch` with a mix of new, duplicate, dismissed and invalid entries returns a per-item outcome
   and creates only the valid new ones.
4. A `proposed` item does not appear in `items` (default), `--status all`, or the tablet document,
   and appears with `--status proposed` and `any`.
5. `accept` makes it appear on the next `todo` publish.
6. `propose_items` over `mcp` creates `proposed` items even when `mcp_add_mode = auto`;
   `add_item` over `mcp` creates `open`, or `proposed` under `propose_all`.
7. `source.url` of `javascript:...` or `http://...` is rejected with `invalid`.
8. `events --follow` emits `item.proposed` and `item.accepted`, and a restart with `--since` misses none.
9. `claude connect` on a config with other servers keeps them, writes a backup, is idempotent, and
   leaves an unparseable config untouched with a `config` error.
10. `claude status` reports `command_exists: false` after the binary is moved.
11. Migration on a database from the previous release preserves all items, sets `origin`, and is
    safe to run twice.

## 14. Open questions

1. Should every dismissal leave a tombstone, or only source-keyed items (as drafted)?
2. Is 100 per batch and 200 pending proposed the right size, given the two-page tablet document?
3. Contract version number: `1.1`, or tie it to the release?
4. Does Claude Desktop honour app-only tool visibility for the image tool? (**Verify** before the widget.)
5. Should `claude connect` also accept `--name` so power users can register a second profile?

## 15. Out of scope

The inline widget, a hosted or remote MCP server, Codex and other app connectors (the `connect`
pattern is meant to extend to them), and any change to how handwriting is read or judged.
