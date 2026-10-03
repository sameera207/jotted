// Development only, never served to a host: a fake Bridge with fixture data, so list.js can be
// worked on in a normal browser (dev/index.html). Every call takes a moment, like the real one.
"use strict";

const Bridge = (() => {
  const params = new URLSearchParams(location.search);
  const mode = params.get("state") || "ready";
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const err = (code, message, extra) => Object.assign(new Error(message), { code }, extra || {});
  let cursor = 184;
  let nextId = 100;
  let busyOnce = mode === "busy";

  const page = (doc, n, anchor) => ({ doc_id: doc, doc_name: "Retro prep", page: n, page_count: 5, anchor });
  let items = mode === "empty" ? [] : [
    { id: 7, text: "Send Dana the revised estimate", status: "proposed", owner: "me", owner_name: null,
      origin: "agent", source: { kind: "gdoc", key: "1AbC#h.x7q", title: "Platform sync, 29 Sep",
      url: "https://docs.google.com/document/d/1AbC", excerpt: "…Sam to send the revised estimate to Dana by Friday." } },
    { id: 8, text: "Book the venue for the offsite", status: "proposed", owner: "someone_else", owner_name: "Priya",
      origin: "agent", source: { kind: "gmail", key: "m-88", title: "Re: offsite dates", excerpt: "Priya will book." } },
    { id: 1, text: "Call the plumber", status: "open", owner: "me", owner_name: null, origin: "remarkable",
      source: { kind: "remarkable", key: "doc-a:1:14", title: "Retro prep" }, page: page("doc-a", 2, "1:14") },
    { id: 2, text: "Review Sam's deck <script>alert(1)</script>", status: "open", owner: "someone_else",
      owner_name: "Sam", origin: "remarkable", source: { kind: "remarkable", key: "doc-a:1:20" },
      page: page("doc-a", 3, "1:20") },
    { id: 3, text: "Renew passport", status: "open", owner: "me", owner_name: null, origin: "web", source: {} },
    { id: 4, text: "Order printer ink", status: "done", owner: "me", owner_name: null, origin: "web", source: {} },
  ];

  const svg = (w, h, label) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}">` +
    `<path d="M10 ${h / 2} q 20 -18 40 0 t 40 0 t 40 0 t 40 0" fill="none" stroke="#1f1f1d" stroke-width="3"/>` +
    `<text x="10" y="${h - 6}" font-size="10" fill="#777">${label}</text></svg>`;

  function counts() {
    const c = { open_mine: 0, open_others: 0, proposed: 0, done: 0 };
    for (const i of items) {
      if (i.status === "proposed" || i.status === "done") c[i.status] += 1;
      else if (i.status === "open") c[i.owner === "someone_else" ? "open_others" : "open_mine"] += 1;
    }
    return c;
  }

  function item(id) {
    const found = items.find((i) => i.id === id);
    if (!found) throw err("not_found", `no item ${id}`);
    return found;
  }

  const tools = {
    show_list() {
      if (mode === "setup") throw err("not_set_up", "reMarkable account: not set up yet", { step: "remarkable.connect" });
      if (mode === "error") throw err("internal", "Unexpected error: the database is locked");
      if (busyOnce) { busyOnce = false; throw err("busy", "Another job holds the device", { retry: true }); }
      const visible = items.filter((i) => i.status !== "dismissed");
      return { as_of: new Date().toISOString(), cursor, counts: counts(), items: visible, next_cursor: null };
    },
    items_list() { return { items: [], next_cursor: null }; },
    items_changes({ since }) { return { cursor: Math.max(since, cursor), events: [] }; },
    items_done({ id }) { item(id).status = "done"; cursor += 1; return item(id); },
    items_reopen({ id }) { item(id).status = "open"; cursor += 1; return item(id); },
    items_dismiss({ id }) { item(id).status = "dismissed"; cursor += 1; return {}; },
    items_edit({ id, text }) { item(id).text = text; cursor += 1; return item(id); },
    items_accept({ ids }) {
      const accepted = [];
      const skipped = [];
      for (const id of ids) {
        const found = items.find((i) => i.id === id);
        if (!found) skipped.push({ id, reason: "not_found" });
        else if (found.status !== "proposed") skipped.push({ id, reason: "not_proposed" });
        else { found.status = "open"; accepted.push(id); }
      }
      if (!accepted.length) throw err("conflict", "Not proposed");
      cursor += 1;
      return { accepted, skipped };
    },
    items_add_typed({ text, owner }) {
      const added = { id: nextId++, text, status: "open", owner: owner === "others" ? "someone_else" : "me",
                      owner_name: null, origin: "web", source: {}, created: true };
      items.push(added);
      cursor += 1;
      return added;
    },
    line_image({ anchor }) {
      if (anchor === "1:20") throw err("not_connected", "The tablet's cloud can't be reached");
      return { mime: "image/svg+xml", svg: svg(180, 40, anchor) };
    },
    page_image({ page: n }) { return { mime: "image/svg+xml", svg: svg(600, 800, `page ${n}`) }; },
  };

  return {
    async init() {
      await delay(150);
      const theme = params.get("theme");
      if (theme) document.documentElement.dataset.theme = theme;
      return { theme: theme || undefined };
    },
    async callTool(name, args) {
      await delay(250);
      console.debug("tools/call", name, args);
      if (!tools[name]) throw err("usage", `no tool ${name}`);
      return JSON.parse(JSON.stringify(tools[name](args || {})));
    },
    updateModelContext(text) { console.info("model context:", text); return Promise.resolve(); },
    openLink(url) { if (url.startsWith("https://")) console.info("open link:", url); return Promise.resolve(); },
    onToolResult() {}, // no initial result: the widget fetches show_list itself, as from the chat history
    onContextChange() {},
    resultData(result) { return result.structuredContent; },
    theme() { return document.documentElement.dataset.theme || "light"; },
  };
})();
