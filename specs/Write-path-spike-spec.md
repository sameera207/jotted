# rM Tasks — Write-path spike spec

Sep 29, 2026 · @Sam · draft 2: web changes are printed into a PDF template under your ink

## Purpose and goals

The read-path spike turns handwriting into tasks with stable anchors. This spike proves the reverse: a change made in the web UI shows up on the tablet, without ever writing to your ink.

The notebook becomes a PDF we generate. Each page has three zones:

```text
┌────────────────────────────────┐
│ Date: ________                 │  header: you write the date
├────────────────────────────────┤
│                                │
│  body: your handwritten tasks  │
│                                │
├────────────────────────────────┤
│ From web:                      │  footer: printed by us
│  [ ] …                         │
└────────────────────────────────┘
```

Your handwriting is stored as annotations on top of the PDF. Web changes are printed into the PDF underneath, and the PDF is swapped with `rmapi put --content-only`, which replaces the PDF and keeps the annotations.

Goals:

1. Generate the template PDF, with zones at known coordinates.
2. Map annotation coordinates to PDF coordinates exactly, so a printed mark lands on a handwritten line.
3. Mark a task done from the web: a printed line through its handwriting, in place.
4. Add a task from the web: printed in the footer of the page.
5. Replace the PDF without losing or shifting any handwriting.
6. Read the header as the page's date, the body as tasks, and ignore the footer, which we printed.

## Decisions

| Question | Decision |
| --- | --- |
| Where web changes appear | Printed into the PDF under the ink; never written as strokes |
| Done from the web | A printed line through the handwritten line, in place |
| Web-created tasks | Printed in the page's footer zone |
| Web edit of a task's text | The old line is struck, the full new text goes in the footer |
| Conflicts | The most recent change wins, per task and per field |
| Handwriting | Never modified. The `.rm` files are only ever read |
| Edge cases (moved ink, overflow, un-done, extra pages) | Deferred |

## Scope

In scope:

- `template.py`: build the PDF (zones, labels, printed strikes, footer tasks) with reportlab.
- Coordinate mapping between reMarkable page units and PDF points, verified on the tablet.
- Zones in the read path: header, body, footer by y coordinate.
- `jotted template build`: write the PDF locally.
- `jotted push`: rebuild the PDF from the latest scan plus a local `webstate.json` (standing in for the task database), then upload with `--content-only`. `--dry-run` only writes the PDF.
- A sandbox notebook, `Tasks-sandbox`, used for every test in this spike.

Out of scope: the web UI, the task database, polling, and the deferred edge cases.

## Page geometry

The rM2 screen is 1404 × 1872 pixels, 3:4. The template page uses the same aspect ratio, so the tablet shows it full-screen with no letterboxing:

- PDF page: 468 × 624 pt (1 pt = 3 reMarkable units).
- Annotations on a PDF page use the same units as a notebook: x from −702 to 702, centred on the page; y from 0 at the top.
- Mapping: `pdf_x = (x + 702) / 3`, `pdf_y_from_top = y / 3`. reportlab counts y from the bottom, so `pdf_y = 624 − y / 3`.

This mapping is the assumption W2 tests. If the tablet scales or offsets PDF pages, the correction goes in config.

Zones are fractions of page height, in `[template]`:

```toml
[template]
pages         = 20      # template pages in a new notebook
header_height = 0.08    # top 8%: date
footer_height = 0.22    # bottom 22%: printed web content
line_spacing  = 0.045   # faint ruling in the body, as a share of page height; 0 = none
```

## Write operations

| Web change | Printed into the PDF |
| --- | --- |
| Done | A horizontal line across the task's handwritten line, at the vertical middle of its bbox, from its left to its right edge |
| New task | `[ ]` and the text, one per row in the footer |
| Edit | A done-style line through the old handwriting, and the full new text in the footer |

The push rebuilds every page from state each time, so the PDF always reflects the current state. Nothing accumulates, and un-doing a change later is only a matter of not printing it.

## Read path changes

- Lines are assigned to a zone by the vertical centre of their bbox.
- The header's text becomes the page's date. It is transcribed, not judged by Jev.
- Only body lines become tasks.
- Footer ink is ignored in this spike. Handwriting in the footer, such as a tick on a web task, is a later phase.

## Test plan

Every test uses `Tasks-sandbox`. `Tasks` isn't touched in this spike.

| # | Test | How | Pass when |
| --- | --- | --- | --- |
| W1 | Template | `jotted template build`; upload as `Tasks-sandbox`; open on the tablet | Full-screen pages, zones visible, the pen writes normally |
| W2 | Alignment | Draw a short line along a printed calibration mark on the first page; scan | The ink maps onto the mark within 3 pt |
| W3 | Keep ink | Write tasks on the sandbox; push an unchanged PDF with `--content-only`; sync; scan | Every stroke is present with identical points; nothing moved on the tablet |
| W4 | Done | Mark one task done in `webstate.json`; push | The printed line crosses that line's handwriting on the tablet; the next scan reports it done |
| W5 | Add | Add two tasks in `webstate.json`; push | Both appear in the footer; no handwriting affected |
| W6 | Zones | Write a date in the header and a task in the body | The date is read as the page's date; only the body line is a task |

## Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| `--content-only` doesn't keep annotations as documented | Lost handwriting | Sandbox only; W3 before any real use; `cache/backups/` keeps each downloaded `.rmdoc` |
| The tablet scales or offsets PDF pages | Strikes miss the text | W2 measures it; the offset goes in config |
| The page count changes between versions | Annotations attach to the wrong page | The push keeps the page count; adding pages is deferred |
| Undocumented cloud API | Push breaks | All cloud access stays in `cloud.py`; `--dry-run` works offline |
