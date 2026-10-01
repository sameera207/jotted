# rM Tasks — Common to-do list spec

Oct 1, 2026 · @Sam · draft

## Purpose

One list of everything that needs doing, collected from wherever you write. A watcher reads the documents you choose (meeting notes and so on), asks Jev which lines are actions, and keeps each action in one to-do list. Each item links back to the line it came from. The list lives in the web app and as a To-do document on the tablet, and ticking an item in either place marks it done.

Reading is incremental. Only new or changed lines are sent to Claude and Jev, never a whole document again.

## Decisions

| Question | Decision |
| --- | --- |
| Sources | The reMarkable library first, built as an adapter behind a port (hexagonal), so other sources (local Markdown, Google Docs) can be added later |
| Where the list lives | The web app, and a generated To-do PDF on the tablet |
| Which actions | Every action, yours or someone else's. Jev judges the owner, and the list can filter by it |
| Watched folders and thresholds | Set in the web UI, stored in the database. `config.toml` keeps only paths, secrets and defaults |
| On the tablet | A separate To-do document with printed checkboxes; you tick by hand |
| Source documents | Read-only. Ticking an action done never writes to the meeting notes it came from |

## Architecture (ports and adapters)

```text
                 driving adapters                                     driven adapters
        ┌──────────────────────────┐                        ┌───────────────────────────────┐
        │ web (Flask)   cli        │                        │ remarkable.Library (rmapi,     │
        │ background scheduler     │                        │   rmscene, lines, Claude)      │
        └────────────┬─────────────┘                        │ jev.ActionJudge (TypeSafe)     │
                     │ calls                                │ sqlite.Repository              │
        ┌────────────▼─────────────────────────────┐        │ remarkable.TodoDocument (PDF)  │
        │ core: services                           │──────▶ │ (later: markdown.Folder, …)    │
        │   collect · publish · read_ticks · edit  │  ports └───────────────────────────────┘
        │ core: model  (Source, Page, Line,        │
        │               ActionItem, Judgment)      │
        └──────────────────────────────────────────┘
```

The core imports no adapter, no Flask, no rmapi and no SDK. It sees only the ports:

| Port | Responsibility | First adapter |
| --- | --- | --- |
| `DocumentSource` | List documents in watched locations with a change marker; list a document's pages with a content hash; read a page as `SourceLine`s (text, stable anchor, location, page heading) | `remarkable.Library`: rmapi listing and download, rmscene, line clustering, Claude transcription (all existing code, wrapped) |
| `ActionJudge` | For a page's new lines plus context: P(action), owner (`me`, `someone else`, `unclear`) | `jev.ActionJudge` |
| `Repository` | Sources, page hashes, lines seen, action items, settings | `sqlite.Repository` (extends the current store) |
| `TodoPublisher` | Render the list where you can tick it, and read ticks back | `remarkable.TodoDocument` |

Existing modules stay where they are and are wrapped by the reMarkable adapter. The Tasks notebook flow moves onto the same core in the last stage.

## Incremental reading

Three levels, each skipping what hasn't changed:

1. **Document.** The library listing gives each document's modified time. Unchanged documents are not downloaded.
2. **Page.** Each page is its own `.rm` file. Its SHA-256 is stored, and an unchanged page is not parsed.
3. **Line.** A changed page is clustered into lines. A line whose stroke IDs were all seen before keeps its stored text and judgment. Only new or changed lines go to Claude, and only their pages go to Jev. The existing AI cache, keyed by stroke IDs, backs this.

Adding two lines to a 30-line page sends two line images to Claude and one small request to Jev.

The whole document is still downloaded when it changes: rmapi has no per-page download. That costs bandwidth only, not AI calls.

## Judging actions

One Jev request per changed page. The state holds the page's lines plus context: document name, folder path, page heading or date, and neighbouring lines. Each new line gets two questions, asked together:

- **Noul:** is this line an action: something someone should do, follow up or decide?
- **Choice:** who owns it: `me` (the writer), `someone else` (a named person or team), or `unclear`.

Policy stays in code: a line is an action when P(action) ≥ `action_threshold`, which is set in the UI and defaults higher than for the Tasks notebook, because a shared list full of false positives stops being trusted. The owner is stored as a label and filter, not a gate. Wrapped lines and drawings reuse the existing merge logic.

## The list

Each action item stores:
- its source: document id, name and folder path, page index and id
- its anchor stroke, line box and text
- the P(action) and owner judgments
- its status, with change times, so the most recent change wins (as in the current store)
- its slot on the To-do document

Items that disappear from their source are flagged, not deleted.

### Web app

- **One combined list.** Filter by open or done, owner (mine, others, all) and source folder or document. The Tasks notebook appears as one source among others.
- **Each item shows** an image of its handwritten line and a link to its source page with the line highlighted. reMarkable has no stable deep link into its own apps, so the link goes to our page view, labelled with folder, document and page number.
- **Settings panel:** pick watched folders from your library's folder tree; set the action threshold, the poll interval, and whether to include others' actions on the tablet.

### To-do document on the tablet

A generated PDF, `To-do`, in a folder you choose:

- **Pre-allocated pages:** 20 pages of 25 slots. The page count can't change after creation without losing ticks (`--content-only` keeps the page list).
- **Fixed slots.** An item keeps its slot (page, row) from the first time it's printed. A new item takes the next free slot, so your ticks never drift onto another item.
- **Each slot shows** a checkbox, the text, and a small source line ("Meeting notes › Weekly sync · p3"). Done items are struck through.
- **Ticks.** A hand-drawn tick or cross inside a slot's checkbox marks the item done on the next pull. It is read with the same calibrated mapping as the Tasks template (`template.scale`). As in the current store, a paper tick only counts when the ink changed, so re-opening an item on the web isn't undone by the old tick.
- **When the slots run out:** create `To-do (2)` and carry over the open items. A later step; 500 slots is months of use.

## Scheduling

One background scheduler replaces today's auto-pull and auto-push:

- **Every `poll_interval_s`:**
  1. list the watched folders
  2. collect the changed documents
  3. read ticks from the To-do document
  4. if anything changed, republish it
- **After a web edit:** republish after a few seconds (the existing debounce).
- **All rmapi calls share one lock.** Running two at once blocks one of them.

## Privacy and cost

- Only watched folders are read. The default is none, so nothing beyond Tasks is sent until you choose folders in the UI.
- New lines are sent to Anthropic (as images) and TypeSafe (as text). The web UI shows each watched folder's document count before you enable it.
- Cost follows how much you write, not how often documents sync.

## Stages

| # | Stage | Done when |
| --- | --- | --- |
| 1 | Core model and ports; `remarkable.Library` adapter with folder tree, document and page change detection; repository tables; `rmtasks collect --dry-run` | A run over one folder lists new lines per changed page; a second run with no tablet changes reads nothing |
| 2 | `jev.ActionJudge` (action and owner); action items in the store; the combined list and source links in the web app | Actions from a real meeting-notes page appear with owners and link to a highlighted source page |
| 3 | Settings in the web UI (folder picker, thresholds); the background scheduler | Enabling a folder in the UI starts collecting it without a restart |
| 4 | `remarkable.TodoDocument`: create, publish to fixed slots, read ticks | Ticking a box on the tablet marks the item done in the web app within one poll |
| 5 | The Tasks notebook flow moved onto the core (source plus template publisher) | The current tests pass through the new core; one scheduler for everything |

Every stage keeps the current app working and adds tests that use fake adapters for the core.

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Meeting notes yield many false actions | A noisy list | Context-rich Jev questions; a threshold set in the UI; a "not an action" dismiss in the web app, remembered per line |
| Typed text (Type Folio) in notes | Missed actions | rmscene reads typed text; the adapter emits typed paragraphs as lines (stage 2, if your notes use it) |
| A whole document is downloaded for a small change | Slow polls on big notebooks | Bandwidth only; page and line deltas keep AI calls small |
| A tick lands between slots | Missed or wrong tick | Generous checkbox hit areas; ticks outside any box are ignored and shown in the overlay |
| Many watched folders | Long first run | The first collection runs in the background with progress in the UI; later runs are incremental |

## Open questions

- [ ] Should dismissed actions ("not an action") be fed back as examples in the Jev question? (A later tuning step.)
- [ ] Should due dates in a line ("by Friday") be extracted and shown? Jev can select the date phrase from candidates found in code.
