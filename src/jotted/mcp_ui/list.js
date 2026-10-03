// The Jotted list: state, rendering and actions. Talks to the host only through Bridge.
// One State object; render() redraws everything from it. Every node made from data goes
// through h(), which only ever sets text, never HTML.
"use strict";

const State = {
  phase: "loading", // loading | ready | setup | error
  error: null, // {code, message, step}
  items: [], // proposed and listed items, in the server's order
  counts: { open_mine: 0, open_others: 0, proposed: 0, done: 0 },
  asOf: null,
  cursor: null, // events cursor, for polling
  nextCursor: null, // more of the list after this item id
  filter: "all", // all | mine | others
  note: "",
  pending: new Set(),
  editing: null,
  editText: "",
  draft: "",
  draftOwner: "mine",
  popup: null,
  images: new Map(),
  busyRetried: false,
};

const View = (() => {
  const POLL_MS = 30000;
  const SOURCE_NAMES = { gdoc: "Doc", gmail: "Mail", confluence: "Confluence", jira: "Jira", gcal: "Calendar",
                         chat: "Chat", other: "Elsewhere" };
  const app = document.getElementById("app");
  let pollTimer = null;
  let observer = null;
  let popupOpener = null;
  let rendering = false; // removing a focused input can fire blur: that isn't the person leaving it

  // ------------------------------------------------------------ building nodes

  function h(tag, attrs, children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (typeof value === "function") el.addEventListener(name.replace(/^on/, ""), value);
      else if (name === "dataset") Object.assign(el.dataset, value);
      else if (name === "className") el.className = value;
      else el.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of [].concat(children === undefined ? [] : children)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // ------------------------------------------------------------ state helpers

  const find = (id) => State.items.find((i) => i.id === id);
  const isMine = (item) => item.owner !== "someone_else";
  const shown = (item) => State.filter === "all" || (State.filter === "mine") === isMine(item);

  function countKey(item) {
    if (!item || item.status === "dismissed") return null;
    if (item.status === "proposed" || item.status === "done") return item.status;
    return isMine(item) ? "open_mine" : "open_others";
  }

  // Counts come from the server; local changes move them, so they stay right past the first page.
  function recount(before, after) {
    const a = countKey(before);
    const b = countKey(after);
    if (a) State.counts[a] -= 1;
    if (b) State.counts[b] += 1;
  }

  function put(item) {
    const at = State.items.findIndex((i) => i.id === item.id);
    if (at === -1) {
      State.items.push(item);
      recount(null, item);
    } else {
      recount(State.items[at], item);
      State.items[at] = item;
    }
  }

  function remove(id) {
    const item = find(id);
    if (!item) return;
    recount(item, null);
    State.items = State.items.filter((i) => i.id !== id);
    if (State.popup === id) State.popup = null;
  }

  // ------------------------------------------------------------ loading

  function load(data) {
    if (!data || !Array.isArray(data.items)) throw Object.assign(new Error("The list came back empty."), { code: "internal" });
    State.items = data.items;
    State.counts = { ...data.counts };
    State.asOf = data.as_of;
    State.cursor = data.cursor;
    State.nextCursor = data.next_cursor === undefined ? null : data.next_cursor;
    State.phase = "ready";
    State.error = null;
    if (State.busyRetried) State.note = "";
    State.busyRetried = false;
    if (State.popup !== null && !find(State.popup)) State.popup = null;
    render();
  }

  async function refresh(quietly) {
    if (!quietly && State.phase !== "ready") {
      State.phase = "loading";
      render();
    }
    try {
      load(await Bridge.callTool("show_list", {}));
    } catch (err) {
      failed(err);
    }
  }

  function failed(err) {
    if (err.code === "not_set_up") {
      State.phase = "setup";
      State.error = err;
    } else if (err.code === "busy" && !State.busyRetried) {
      State.busyRetried = true;
      State.note = "Jotted is checking your tablet. Trying again in a moment…";
      setTimeout(() => refresh(true), 4000);
    } else if (State.phase === "ready") {
      State.note = `Couldn't refresh: ${err.message}`;
    } else {
      State.phase = "error";
      State.error = err;
    }
    render();
  }

  // An action that failed: undo it, and refresh quietly if the item changed elsewhere.
  function actionFailed(err, undo) {
    undo();
    if (err.code === "conflict" || err.code === "not_found") {
      State.note = "That item changed somewhere else, so the list was refreshed.";
      refresh(true);
    } else if (err.code === "not_set_up") {
      failed(err);
    } else {
      State.note = `Couldn't do that: ${err.message}`;
    }
  }

  async function loadMore() {
    try {
      const page = await Bridge.callTool("items_list", { status: "all", limit: 50, cursor: State.nextCursor });
      page.items.forEach((item) => { if (!find(item.id)) State.items.push(item); });
      State.nextCursor = page.next_cursor;
    } catch (err) {
      State.note = `Couldn't load more: ${err.message}`;
    }
    render();
  }

  // ------------------------------------------------------------ actions

  async function toggle(id) {
    const before = find(id);
    if (!before || State.pending.has(id)) return;
    const done = before.status !== "done";
    State.pending.add(id);
    put({ ...before, status: done ? "done" : "open" });
    render();
    try {
      const result = await Bridge.callTool(done ? "items_done" : "items_reopen", { id });
      put({ ...(find(id) || before), ...result });
    } catch (err) {
      actionFailed(err, () => put(before));
    } finally {
      State.pending.delete(id);
      render();
    }
  }

  async function accept(ids, everything) {
    ids = ids.filter((id) => !State.pending.has(id) && find(id));
    if (!ids.length) return;
    const waiting = State.counts.proposed;
    const befores = ids.map(find);
    ids.forEach((id) => State.pending.add(id));
    befores.forEach((item) => put({ ...item, status: "open" }));
    render();
    try {
      const result = await Bridge.callTool("items_accept", { ids });
      if (result.skipped.length) {
        State.note = "Some of those had changed somewhere else, so the list was refreshed.";
        refresh(true);
      }
      Bridge.updateModelContext(everything
        ? `In the Jotted widget, the person accepted ${result.accepted.length} of ${waiting} proposals.`
        : `In the Jotted widget, the person accepted proposal #${ids[0]}.`);
    } catch (err) {
      actionFailed(err, () => befores.forEach(put));
    } finally {
      ids.forEach((id) => State.pending.delete(id));
      render();
    }
  }

  async function dismiss(id) {
    const before = find(id);
    if (!before || State.pending.has(id)) return;
    const at = State.items.indexOf(before);
    State.pending.add(id);
    remove(id);
    render();
    try {
      await Bridge.callTool("items_dismiss", { id });
      Bridge.updateModelContext(before.status === "proposed"
        ? `In the Jotted widget, the person dismissed proposal #${id}.`
        : `In the Jotted widget, the person dismissed item #${id}.`);
    } catch (err) {
      actionFailed(err, () => {
        State.items.splice(at, 0, before);
        recount(null, before);
      });
    } finally {
      State.pending.delete(id);
      render();
    }
  }

  function startEdit(id) {
    const item = find(id);
    if (!item || State.pending.has(id)) return;
    State.editing = id;
    State.editText = item.text;
    render();
    focusKey(`edit-${id}`);
  }

  async function saveEdit() {
    const id = State.editing;
    const text = State.editText.trim();
    State.editing = null;
    const before = find(id);
    if (!before || !text || text === before.text) {
      render();
      return;
    }
    State.pending.add(id);
    put({ ...before, text });
    render();
    try {
      put({ ...(find(id) || before), ...(await Bridge.callTool("items_edit", { id, text })) });
    } catch (err) {
      actionFailed(err, () => put(before));
    } finally {
      State.pending.delete(id);
      render();
    }
  }

  async function add(event) {
    event.preventDefault();
    const text = State.draft.trim();
    if (!text) {
      State.note = "Enter an item first.";
      render();
      return;
    }
    State.draft = "";
    State.note = "";
    render();
    try {
      const item = await Bridge.callTool("items_add_typed", { text, owner: State.draftOwner });
      if (!find(item.id)) put(item);
    } catch (err) {
      State.draft = text;
      State.note = err.code === "not_set_up" ? err.message : `Couldn't add it: ${err.message}`;
    }
    render();
  }

  // ------------------------------------------------------------ polling

  async function poll() {
    if (document.hidden || State.phase !== "ready" || State.editing !== null || State.pending.size) return;
    try {
      const changes = await Bridge.callTool("items_changes", { since: State.cursor });
      if (changes.events.some((e) => e.type.startsWith("item."))) {
        await refresh(true);
      } else {
        State.cursor = changes.cursor;
        State.asOf = new Date().toISOString();
        render();
      }
    } catch (err) {
      // Quiet: polling is a convenience, and the refresh button still works.
    }
  }

  function startPolling() {
    clearInterval(pollTimer);
    pollTimer = document.hidden ? null : setInterval(poll, POLL_MS);
  }

  // ------------------------------------------------------------ handwriting

  function imageKey(kind, item) {
    return [kind, item.page.doc_id, item.page.anchor, kind === "page" ? item.page.page : ""].join(":");
  }

  async function loadImage(kind, item) {
    const key = imageKey(kind, item);
    if (State.images.has(key)) return;
    State.images.set(key, { status: "loading" });
    try {
      const image = kind === "line"
        ? await Bridge.callTool("line_image", { doc_id: item.page.doc_id, anchor: item.page.anchor })
        : await Bridge.callTool("page_image", { doc_id: item.page.doc_id, page: item.page.page,
                                                anchor: item.page.anchor, width: 600 });
      // Through <img> only: an SVG shown as an image can't run script.
      State.images.set(key, { status: "ready", url: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(image.svg) });
    } catch (err) {
      State.images.set(key, { status: "failed" });
    }
    render();
  }

  function openPopup(id) {
    const item = find(id);
    if (!item || !item.page) return;
    popupOpener = `tile-${id}`;
    State.popup = id;
    loadImage("page", item);
    render();
    focusKey("popup-close");
  }

  function closePopup() {
    State.popup = null;
    render();
    focusKey(popupOpener);
  }

  // ------------------------------------------------------------ rendering

  function fmtTime(iso) {
    const when = new Date(iso);
    return isNaN(when) ? "" : when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function owner(item) {
    if (isMine(item)) return null;
    return h("span", { className: "owner others" }, item.owner_name || "Someone else");
  }

  function sourceChip(item) {
    const src = item.source || {};
    if (item.page) {
      const of = item.page.page_count ? ` of ${item.page.page_count}` : "";
      return h("span", { className: "chip" }, `${item.page.doc_name} · p${item.page.page}${of}`);
    }
    if (item.origin === "web") return h("span", { className: "chip" }, "added here");
    const label = [SOURCE_NAMES[src.kind] || src.kind, src.title].filter(Boolean).join(" · ") || "added by Claude";
    if (typeof src.url === "string" && src.url.startsWith("https://")) {
      return h("span", { className: "chip" }, h("button", {
        type: "button", title: src.url, onclick: () => Bridge.openLink(src.url),
      }, label));
    }
    return h("span", { className: "chip" }, label);
  }

  function itemText(item) {
    if (State.editing === item.id) {
      return h("input", {
        className: "edit-input", value: State.editText, "aria-label": "Edit the item", maxlength: 500,
        dataset: { key: `edit-${item.id}` },
        oninput: (e) => { State.editText = e.target.value; },
        onkeydown: (e) => {
          if (e.key === "Enter") saveEdit();
          if (e.key === "Escape") { e.stopPropagation(); State.editing = null; render(); focusKey(`text-${item.id}`); }
        },
        onblur: () => { if (!rendering && State.editing === item.id) saveEdit(); },
      });
    }
    return h("div", { className: "text" }, h("button", {
      type: "button", className: "edit", title: "Edit", dataset: { key: `text-${item.id}` },
      onclick: () => startEdit(item.id),
    }, item.text));
  }

  function tile(item) {
    if (!item.page) return null;
    const image = State.images.get(imageKey("line", item));
    const inner = image && image.status === "ready"
      ? h("img", { src: image.url, alt: item.text })
      : image && image.status === "failed"
        ? h("span", { className: "quiet" }, "…")
        : null;
    return h("button", {
      type: "button", className: "tile" + (inner ? "" : " skeleton"),
      "aria-label": `Handwriting: ${item.text}. Open the page.`, dataset: { key: `tile-${item.id}`, tile: item.id },
      onclick: () => openPopup(item.id),
    }, inner);
  }

  function listRow(item) {
    const done = item.status === "done";
    const busy = State.pending.has(item.id);
    return h("li", { className: `row${done ? " done" : ""}${busy ? " pending" : ""}` }, [
      h("input", {
        type: "checkbox", className: "check", checked: done, disabled: busy,
        "aria-label": `${done ? "Reopen" : "Mark done"}: ${item.text}`, dataset: { key: `check-${item.id}` },
        onchange: () => toggle(item.id),
      }),
      h("div", {}, [itemText(item), h("div", { className: "meta" }, [owner(item), sourceChip(item)])]),
      h("div", { className: "actions" }, [
        tile(item),
        h("button", {
          type: "button", className: "btn link", title: "Dismiss", "aria-label": `Dismiss: ${item.text}`,
          disabled: busy, dataset: { key: `dismiss-${item.id}` }, onclick: () => dismiss(item.id),
        }, "✕"),
      ]),
    ]);
  }

  function proposedRow(item) {
    const busy = State.pending.has(item.id);
    const excerpt = item.source && item.source.excerpt;
    return h("li", { className: `row proposed${busy ? " pending" : ""}` }, [
      h("div", {}, [
        itemText(item),
        h("div", { className: "meta" }, [owner(item), sourceChip(item)]),
        excerpt ? h("blockquote", { className: "excerpt" }, `“${excerpt}”`) : null,
      ]),
      h("div", { className: "actions" }, [
        h("button", {
          type: "button", className: "btn primary", disabled: busy, "aria-label": `Accept: ${item.text}`,
          dataset: { key: `accept-${item.id}` }, onclick: () => accept([item.id], false),
        }, "Accept"),
        h("button", {
          type: "button", className: "btn", disabled: busy, "aria-label": `Dismiss: ${item.text}`,
          dataset: { key: `dismiss-${item.id}` }, onclick: () => dismiss(item.id),
        }, "Dismiss"),
      ]),
    ]);
  }

  function header() {
    const c = State.counts;
    const filters = [["all", "All"], ["mine", "Mine"], ["others", "Others"]].map(([value, label]) => h("button", {
      type: "button", "aria-pressed": State.filter === value ? "true" : "false", dataset: { key: `filter-${value}` },
      onclick: () => { State.filter = value; render(); },
    }, label));
    return h("div", { className: "top" }, [
      h("div", { className: "counts" }, [`${c.open_mine} open`,
        h("span", {}, ` · ${c.open_others} for others · ${c.proposed} proposed`)]),
      h("div", { className: "filter", role: "group", "aria-label": "Show" }, filters),
      h("div", { className: "asof" }, [
        State.asOf ? `as of ${fmtTime(State.asOf)}` : "",
        h("button", { type: "button", className: "btn link", "aria-label": "Refresh", title: "Refresh",
                      dataset: { key: "refresh" }, onclick: () => { State.note = ""; refresh(true); } }, "↻"),
      ]),
    ]);
  }

  function addForm() {
    return h("form", { className: "add", onsubmit: add }, [
      h("input", {
        type: "text", value: State.draft, placeholder: "Add an item", "aria-label": "Add an item", maxlength: 500,
        dataset: { key: "add" }, oninput: (e) => { State.draft = e.target.value; },
      }),
      h("select", { "aria-label": "Whose item", dataset: { key: "add-owner" },
                    onchange: (e) => { State.draftOwner = e.target.value; } }, [
        h("option", { value: "mine", selected: State.draftOwner === "mine" }, "Me"),
        h("option", { value: "others", selected: State.draftOwner === "others" }, "Others"),
      ]),
      h("button", { type: "submit", className: "btn primary", dataset: { key: "add-submit" } }, "Add"),
    ]);
  }

  function popup() {
    const item = find(State.popup);
    if (!item || !item.page) return null;
    const image = State.images.get(imageKey("page", item));
    const of = item.page.page_count ? ` of ${item.page.page_count}` : "";
    const done = item.status === "done";
    let page = null;
    if (image && image.status === "ready") {
      page = h("img", { src: image.url, alt: `The handwritten page. The highlighted line reads: ${item.text}` });
    } else if (image && image.status === "failed") {
      page = h("p", { className: "quiet pad" }, "Couldn't load this page.");
    }
    return h("div", { className: "shade", onclick: (e) => { if (e.target === e.currentTarget) closePopup(); } },
      h("div", { className: "dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "popup-title",
                 onkeydown: trapFocus }, [
        h("h2", { id: "popup-title" }, `${item.page.doc_name} · page ${item.page.page}${of}`),
        h("div", { className: "page" + (page ? "" : " skeleton") }, page),
        h("p", { className: "read" }, ["Read as: ", h("strong", {}, item.text)]),
        h("div", { className: "actions" }, [
          item.status === "proposed" ? null : h("button", {
            type: "button", className: "btn", dataset: { key: "popup-toggle" }, onclick: () => toggle(item.id),
          }, done ? "Reopen" : "Mark done"),
          h("button", { type: "button", className: "btn primary", dataset: { key: "popup-close" },
                        onclick: closePopup }, "Close"),
        ]),
      ]));
  }

  function trapFocus(event) {
    if (event.key !== "Tab") return;
    const stops = [...event.currentTarget.querySelectorAll("button:not([disabled])")];
    if (!stops.length) return;
    const first = stops[0];
    const last = stops[stops.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function state(title, text, action) {
    return h("div", { className: "state" }, [
      h("h2", {}, title),
      h("p", {}, text),
      action ? h("button", { type: "button", className: "btn", dataset: { key: "retry" }, onclick: action }, "Try again") : null,
    ]);
  }

  function readyView() {
    const proposed = State.items.filter((i) => i.status === "proposed" && shown(i));
    const listed = State.items.filter((i) => (i.status === "open" || i.status === "done") && shown(i));
    const ordered = [...listed.filter((i) => i.status === "open"), ...listed.filter((i) => i.status === "done")];
    const parts = [header()];
    if (State.note) parts.push(h("p", { className: "note", role: "status" }, State.note));
    if (proposed.length) {
      parts.push(h("section", { "aria-labelledby": "proposed-title" }, [
        h("div", { className: "section-head" }, [
          h("h2", { id: "proposed-title" }, `Proposed by Claude (${proposed.length})`),
          proposed.length > 1 ? h("button", {
            type: "button", className: "btn", dataset: { key: "accept-all" },
            onclick: () => accept(proposed.map((i) => i.id), true),
          }, "Accept all") : null,
        ]),
        h("ul", {}, proposed.map(proposedRow)),
      ]));
    }
    if (ordered.length) {
      parts.push(h("section", { "aria-labelledby": "list-title" }, [
        h("div", { className: "section-head" }, h("h2", { id: "list-title" }, "On your list")),
        h("ul", {}, ordered.map(listRow)),
        State.nextCursor !== null ? h("button", {
          type: "button", className: "btn more", dataset: { key: "more" }, onclick: loadMore,
        }, "Show more") : null,
      ]));
    } else if (!proposed.length) {
      parts.push(State.filter === "all"
        ? h("div", { className: "empty" }, [h("p", {}, "Nothing on your list yet."),
            h("p", { className: "quiet" }, "Ask Claude to add something, or write it on your tablet.")])
        : h("p", { className: "empty quiet" }, "Nothing here for this filter."));
    }
    parts.push(addForm());
    const dialog = popup();
    if (dialog) parts.push(dialog);
    return parts;
  }

  function build() {
    if (State.phase === "setup") {
      return [state("Jotted isn't set up yet", `Open Jotted and finish setting it up. ${State.error.message}`,
                    () => refresh())];
    }
    if (State.phase === "error") {
      return [state("Couldn't load your list", State.error ? State.error.message : "", () => refresh())];
    }
    if (State.phase === "loading") return [h("p", { className: "quiet pad" }, "Loading your list…")];
    return readyView();
  }

  function focusKey(key) {
    if (!key) return;
    const el = [...app.querySelectorAll("[data-key]")].find((e) => e.dataset.key === key);
    if (el) el.focus();
  }

  // Redrawing replaces every node, so focus (and the caret, while typing) is put back by key.
  function render() {
    const active = document.activeElement;
    const key = active && active.dataset ? active.dataset.key : null;
    const caret = active && typeof active.selectionStart === "number" ? active.selectionStart : null;
    rendering = true;
    try {
      app.replaceChildren(...build());
    } finally {
      rendering = false;
    }
    if (key) {
      focusKey(key);
      const again = document.activeElement;
      if (caret !== null && again && again.dataset && again.dataset.key === key && again.setSelectionRange) {
        again.setSelectionRange(caret, caret);
      }
    }
    watchTiles();
  }

  // Handwriting loads as rows scroll into view.
  function watchTiles() {
    if (observer) observer.disconnect();
    observer = new IntersectionObserver((entries) => {
      entries.filter((e) => e.isIntersecting).forEach((e) => {
        const item = find(Number(e.target.dataset.tile));
        if (item) loadImage("line", item);
        observer.unobserve(e.target);
      });
    });
    app.querySelectorAll("[data-tile]").forEach((el) => observer.observe(el));
  }

  function onKey(event) {
    if (event.key === "Escape" && State.popup !== null) closePopup();
  }

  async function start() {
    document.addEventListener("keydown", onKey);
    document.addEventListener("visibilitychange", () => {
      startPolling();
      if (!document.hidden) poll();
    });
    let opened = false;
    Bridge.onToolResult((result) => {
      opened = true;
      try {
        load(Bridge.resultData(result));
      } catch (err) {
        failed(err);
      }
    });
    try {
      await Bridge.init();
    } catch (err) {
      State.phase = "error";
      State.error = { message: "Couldn't connect to Claude." };
      render();
      return;
    }
    // A widget reopened from the chat history may not be sent its result again: fetch it.
    setTimeout(() => { if (!opened) refresh(); }, 1500);
    startPolling();
  }

  return { start, render };
})();

View.start();
