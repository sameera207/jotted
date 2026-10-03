# Claude widget: the Jotted list inside Claude Desktop

Status: draft v0.1
Target repo: `jotted-cli` (this file belongs in `specs/`)
Depends on: `specs/Claude-connector-spec.md` (new item fields, proposed status, accept, page image,
`get_changes`)

> Drafted without access to the existing web app's front-end code or to the MCP Apps spec text.
> Items marked **Verify** are assumptions the spike (section 11) must settle.

## 1. Goal

Show the Jotted to-do list as an interactive widget inside the chat in Claude Desktop (and any other
host that supports MCP Apps), so a person can:

- see the list, including the handwriting each item came from;
- review items Claude proposed, and accept or dismiss them;
- tick, edit, dismiss and add items without writing a prompt.

The same tools stay usable as plain text when a host has no UI support.

## 2. Decision: plain static files, no Node toolchain

The widget is HTML, CSS and JavaScript written by hand and shipped as static files inside the Python
package. `jotted mcp` serves them as the `ui://` resource.

- No `package.json`, no bundler, no TypeScript, no framework, no committed build output.
- The repo stays Python-only. A contributor needs `uv` and nothing else.
- `uv tool install git+https://github.com/sameera207/jotted-cli` ships the widget because the files
  are package data.

Why: the widget is one list view with a popup and an input line, and the earlier mockup at that
scope was a few hundred lines of plain JS. A build chain would add a second ecosystem to a repo that
has one, for little gain.

What we give up: TypeScript's checking, component tooling, and an easy path to share components with
a desktop UI. Section 13 lists the signs that it is time to move the source elsewhere.

## 3. Files

```
src/jotted/mcp_ui/
  __init__.py      load_html(): reads the files below and returns one HTML string (cached)
  list.html        page skeleton with placeholders for the CSS and JS
  list.css         styles, using host theme variables with fallbacks
  bridge.js        the only file that talks to the host (section 5)
  list.js          state, rendering, actions
  vendor/          only if a single-file SDK is vendored (section 5); includes VERSION and LICENSE
  dev/mock.js      dev-only fake host and fixture data; never served to a real host
```

- `load_html()` inlines `list.css`, `bridge.js` and `list.js` into `list.html` and returns a single
  document. The host loads the widget in a sandboxed iframe, so relative file requests are not safe
  to rely on. **Verify** in the spike.
- `src/jotted/mcp.py` registers the resource and calls `load_html()` once, then on every read in
  development mode.
- `pyproject.toml` lists `mcp_ui/*.html|css|js` as package data. **Verify** against the build backend.

Size budget: the served HTML stays under 100 KB, and `list.js` under 1,500 lines. Going past either
is a trigger in section 13.

## 4. MCP resource and tools

Resource:

| Property | Value |
| --- | --- |
| URI | `ui://jotted/list` |
| MIME type | `text/html;profile=mcp-app` |
| Network access (CSP) | None. The widget loads nothing from the network. |
| Border | Host default |

`jotted mcp` advertises the MCP Apps UI capability in the handshake. **Verify:** whether the Python
MCP SDK version in use supports this directly. If not, the server adds the extension fields itself;
that is a small, contained change in `mcp.py`.

Tools (names match the connector spec):

| Tool | Visible to | Purpose |
| --- | --- | --- |
| `show_list` | model and widget | Opens the widget. Has `_meta.ui.resourceUri = ui://jotted/list`. Returns a short text summary for the model and the first page of items for the widget. |
| `list_items` | widget only | Paginated list with filters, used to refresh and page. |
| `get_changes` | widget only | Changes since a cursor, used for polling. |
| `get_page_image` | widget only | One page or line as SVG. Never sent to the model. |
| `add_item`, `update_item`, `complete_item`, `reopen_item`, `dismiss_item`, `accept_items` | model and widget | Mutations, validated server-side in the same way for both callers. |

**Verify:** that Claude Desktop honours widget-only visibility (`_meta.ui.visibility`). If it does
not, the image tool must be kept out of the model's context another way, because it carries
handwriting.

`show_list` result:

```json
{
  "content": [{"type": "text", "text": "4 open for you, 3 proposed. Showing the list."}],
  "structuredContent": {
    "as_of": "2026-10-03T09:41:00Z",
    "cursor": "c_184",
    "counts": {"open_mine": 4, "open_others": 1, "proposed": 3, "done": 12},
    "items": [ "...first 50 items, fields as in the connector spec..." ]
  }
}
```

The text part is what a host without UI support, and the model, see. It must stay useful on its own.

## 5. The bridge

All host-specific code lives in `bridge.js`, behind this small interface, so a change in the MCP Apps
spec touches one file:

```js
Bridge.init()                      // handshake; resolves with initial tool result and theme
Bridge.callTool(name, args)        // resolves with the tool's structured result, or rejects
Bridge.updateModelContext(text)    // tell the model what the person did (short text)
Bridge.openLink(url)               // https only, asks the host to open it
Bridge.onToolResult(fn)            // host pushes a new result into the widget
Bridge.theme()                     // 'light' | 'dark'
```

Two ways to build it; the spike picks one:

- **A. Hand-written JSON-RPC client** (around 100 lines). No dependency. We own the work of keeping up
  with the spec.
- **B. Vendor the official SDK** as a single browser file in `vendor/`, if one exists that needs no
  bundler. Pin the version and keep its licence. **Verify** that such a file exists.

If neither works without a build step, stop and take the decision in section 13 before going on.

## 6. Screens and states

1. **List.** Header with counts and an All / Mine / Others filter. A "Proposed by Claude" section
   (hidden when empty) with Accept all, then "On your list". The input line sits under the list with
   an owner picker.
2. **Item row.** Checkbox, text, source chips (notebook and page, document, mail), owner. Handwritten
   items show a small handwriting tile that opens the page popup. Proposed items show a quoted
   excerpt and Accept and Dismiss buttons.
3. **Page popup.** The page with the item's line highlighted, "page N of M", the text that was read,
   Mark done or Reopen, and Close. It is drawn inside the widget. The first version does not
   request a larger display mode. Escape and a click outside close it.
4. **Empty.** "Nothing on your list yet", with a hint about adding from chat or writing on the tablet.
5. **Setup needed.** From `not_set_up`: "Open Jotted and finish connecting your reMarkable", showing
   the step named in `error.step`.
6. **Error.** One line and a Retry button. A `busy` error says Jotted is checking the tablet and
   retries on its own once.
7. **Stale.** Every widget is a snapshot, so each shows "as of 9:41" and a refresh button. Actions on
   items that changed elsewhere return `conflict` or `not_found`; the widget refreshes and shows a
   quiet note rather than an error.

## 7. Behaviour

- **Optimistic updates.** Tick, accept and dismiss update the row at once, call the tool, and roll
  back with a short message if the call fails.
- **Adding.** The input line calls `add_item` directly. Empty input shows "Enter an item first" and
  does nothing. Enter submits. Owner defaults to Me.
- **Refresh and polling.** A refresh button always works. While the widget is visible it polls
  `get_changes` every 30 seconds, and stops when hidden. Hidden state comes from the page visibility
  API, with the host's own signal if it offers one. **Verify** that polling works after the chat has
  been idle.
- **Telling the model.** After accept-all, a dismiss, or a batch of changes, call
  `updateModelContext` with one short line such as "The person accepted 3 of 5 proposals." Do not
  send item text back unless it is needed.
- **Links.** Source links go through `Bridge.openLink`. Only `https` URLs are passed on.
- **Images.** Load lazily as rows scroll into view, and on opening the popup. Fetch with
  `get_page_image` and show with `<img src="data:image/svg+xml;base64,...">`. Show a skeleton while
  loading and a plain "Couldn't load this page" if it fails.
- **Theme.** Use the host's theme variables when provided, else follow `prefers-color-scheme`.
  Handwriting tiles keep a white paper background and dark ink in both themes.
- **Accessibility.** Real buttons and labels, keyboard use for the list and popup, a visible focus
  ring, sensible tab order, and text alternatives for handwriting tiles (the read text).

## 8. Security

- All item text, excerpts, titles and owner names are inserted with `textContent` or DOM methods. Never
  `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write` with data.
- SVG from handwriting is shown only through `<img>` with a `data:` URL, never inlined, so it cannot
  run script.
- No `eval`, `new Function`, or string timers.
- No network requests of any kind. The CSP is empty and the widget relies on `Bridge` for everything.
- Links open only through the host and only if they start with `https://`.
- The widget makes no decisions based on the content of items. An item that reads like an instruction
  is shown as text, nothing more.

## 9. Code conventions

Plain, small and readable, since there is no tooling to catch mistakes:

- No modules and no globals beyond three names: `Bridge`, `State`, `View`. Files are concatenated in
  order: `bridge.js`, then `list.js`.
- A tiny `h(tag, attrs, children)` helper builds DOM nodes with `textContent`. It is the only way
  nodes are made from data.
- State lives in one object. A single `render()` redraws from it; actions change state and call
  `render()`. No framework needed at this size.
- No minification and no comments that restate the code. Comments explain why.
- `dev/mock.js` replaces `Bridge` with a fake host and fixture data so the page can be opened in a
  normal browser during development. `jotted mcp --ui-dir PATH` serves the files from a folder and
  reloads them on each request, so changes show up without reinstalling.

## 10. Tests

Python only:

1. The resource loads, has the right MIME type, and `load_html()` returns one document with the CSS
   and JS inlined.
2. The HTML contains no `http://` or `https://` URLs other than inside comments or the vendored licence,
   and no `<script src>` or `<link href>`.
3. A source check fails the build if `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval` or
   `document.write` appear in `list.js` or `bridge.js`.
4. The served HTML stays under the size budget.
5. `show_list` returns a text summary and `structuredContent` with the documented keys, and the
   widget-only tools are marked widget-only.
6. The new files are present in the built wheel.

Manual, per release: open the widget in Claude Desktop in light and dark, tick and accept an item,
open a page popup, and check the stale and setup states. Later, an optional Playwright run against
`dev/mock.js` can cover the same flow; it is a dev-only tool and not part of the install.

## 11. Phases

**Phase 0: spike (about a day).** A hello-world widget served from `jotted mcp`. Answer:

1. Does the Python MCP SDK in use serve a `ui://` resource and advertise the UI capability, or do we
   add the fields ourselves?
2. Does Claude Desktop honour widget-only tool visibility?
3. Do `data:` images render inside the sandbox?
4. How is the widget's height decided, and does the host resize it when content changes?
5. Can the widget call tools after the chat has been idle?
6. Are old widgets in the chat history still interactive?
7. Does the host pass theme variables, or must we detect light and dark ourselves?
8. Is there a single-file SDK we can vendor, or do we hand-write the bridge?
9. Does the inlined single-document approach load correctly in the sandbox?

Exit: all nine answered in this file, and the decision in section 5 made. Questions 2 and 3 decide the
privacy plan for images; if either fails, stop and replan before Phase 1.

**Spike status (2026-10-03).** Built ahead of the manual checks, so they are quick to run. "Local" means
checked in Chrome with a fake host (a sandboxed `srcdoc` iframe speaking the MCP Apps messages), not in
Claude Desktop.

1. No Python MCP SDK is used: `jotted mcp` is hand-written JSON-RPC. It serves `ui://jotted/list`
   (`resources/list`, `resources/read`) and names the `io.modelcontextprotocol/ui` extension in its
   capabilities. **Answered.**
2. Widget-only visibility: tools carry `_meta.ui.visibility: ["app"]`, and they are listed and answered
   only for a host that advertises the UI extension in `initialize`, so a host without the widget can never
   hand handwriting to the model. Claude Desktop hides them from the model: **answered, works** (Claude Desktop, 2026-10-03).
3. `data:` images: work locally through `<img>`; in Claude Desktop's sandbox: **answered, works.**
4. Height: the widget reports `ui/notifications/size-changed` on every change (local: 120 → 211 px).
   Claude Desktop resizes the widget to fit: **answered, works.**
5. Calls after the chat has been idle: **answered, works** (ticked after 10+ minutes idle).
6. Old widgets in the history: a widget that gets no tool result within 1.5 s calls `show_list` itself
   (local: works). Old widgets stay interactive in Claude Desktop:
   **answered, works.**
7. Theme: the bridge applies `hostContext.theme` and `styles.variables`, else `prefers-color-scheme`.
   In Claude Desktop the widget follows its light and dark themes: **answered, works.**
8. Bridge: hand-written (option A), about 170 lines in `bridge.js`. **Decided**; revisit if an official
   single-file SDK appears.
9. The inlined single document loads and completes the handshake in a sandboxed iframe locally;
   in Claude Desktop: **answered, works.**

Exit criteria met: all nine answered, section 5 decided. Phases 1 to 3 and most of 4 are built (`src/jotted/mcp_ui/`); `dev/index.html` runs them against
`dev/mock.js`. To try it in Claude Desktop: `jotted claude connect`, restart it, ask "show my Jotted list".

**Phase 1: read-only.** `show_list`, the list, the proposed section, filters, empty, setup, error and
stale states. Needs the connector spec's new item fields.

**Phase 2: actions.** Tick, accept, dismiss, edit, and the input line.

**Phase 3: images.** Lazy handwriting tiles and the page popup with the highlighted line.

**Phase 4: polish.** Polling, model-context notes, accessibility pass, theme pass.

**Phase 5: packaging.** The Mac app ships the same files through its bundled CLI, and its "Connect to
Claude Desktop" button calls `jotted claude connect`.

## 12. Open decisions

1. Display modes: stay inline-only in the first version (as drafted), or try a larger view for the
   page popup after the spike?
2. Polling: 30 seconds while visible (as drafted), or manual refresh only?
3. Bridge: hand-written or vendored SDK (spike decides).
4. Size budget numbers in section 3: right for the first version?
5. Whether `jotted mcp --ui-dir` should ship in releases or stay a dev-only flag.

## 13. When to revisit this decision

Move the widget source out of this repo, or add a build step, if any of these happens:

- `list.js` passes the size budget and feels hard to keep correct without types;
- the desktop app wants to share the same components with its own UI;
- the host SDK can only be used through a bundler;
- more than one person is working on the widget and needs a component workflow.

If that happens, the options are: put the source in `jotted-app` and vendor its built output into this
repo with a CI freshness check, or download a pinned, checksummed asset at first run. The CLI contract
and the tools in this spec do not change either way.

## 14. Out of scope

A hosted or remote widget, changes to handwriting reading or judging, a larger display mode in the
first version, and any front-end build step.
