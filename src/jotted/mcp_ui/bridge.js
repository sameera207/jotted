// The only code that talks to the host: MCP Apps (SEP-1865), JSON-RPC 2.0 over postMessage
// with the parent window. Hand-written (spec section 5, option A), so a change in that spec
// touches this file only. The rest of the widget sees the small interface returned below.
"use strict";

const Bridge = (() => {
  const PROTOCOL = "2025-06-18";
  const TIMEOUT_MS = 30000;
  const pending = new Map();
  const toolResultListeners = [];
  const contextListeners = [];
  let nextId = 1;
  let lastResult = null;
  let hostContext = {};
  let hostCapabilities = {};

  function send(message) {
    window.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
  }

  function request(method, params) {
    const id = nextId++;
    send({ id, method, params });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (pending.delete(id)) reject(failure("timeout", "Claude didn't answer in time."));
      }, TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
    });
  }

  function notify(method, params) {
    send({ method, params: params || {} });
  }

  function failure(code, message, extra) {
    const err = new Error(message);
    err.code = code;
    Object.assign(err, extra || {});
    return err;
  }

  // A tool's error is Jotted's {code, message, step?}, as structuredContent or as JSON text.
  function toolError(result) {
    let error = result.structuredContent && result.structuredContent.error;
    if (!error) {
      try {
        error = JSON.parse((result.content || [])[0].text);
      } catch (e) {
        error = { code: "internal", message: "Something went wrong." };
      }
    }
    return failure(error.code || "internal", error.message || "Something went wrong.", { step: error.step });
  }

  function dataOf(result) {
    if (result.structuredContent) return result.structuredContent;
    const text = (result.content || []).find((c) => c.type === "text");
    return text ? JSON.parse(text.text) : null;
  }

  function applyContext(context) {
    hostContext = { ...hostContext, ...context };
    const root = document.documentElement;
    const vars = (hostContext.styles && hostContext.styles.variables) || {};
    for (const [name, value] of Object.entries(vars)) {
      if (name.startsWith("--") && typeof value === "string") root.style.setProperty(name, value);
    }
    if (hostContext.theme === "light" || hostContext.theme === "dark") root.dataset.theme = hostContext.theme;
    contextListeners.forEach((fn) => fn(hostContext));
  }

  function onMessage(event) {
    if (event.source !== window.parent) return;
    const msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") return;
    if (msg.id !== undefined && !msg.method) {
      const waiting = pending.get(msg.id);
      if (!waiting) return;
      pending.delete(msg.id);
      clearTimeout(waiting.timer);
      if (msg.error) waiting.reject(failure("host", msg.error.message || "Claude refused the request."));
      else waiting.resolve(msg.result);
      return;
    }
    switch (msg.method) {
      case "ui/notifications/tool-result":
        lastResult = msg.params;
        toolResultListeners.forEach((fn) => fn(msg.params));
        break;
      case "ui/notifications/host-context-changed":
        applyContext(msg.params || {});
        break;
      case "ui/resource-teardown":
        send({ id: msg.id, result: {} });
        break;
      default:
        // Requests we don't know get an error; notifications (tool-input, initialized...) need nothing.
        if (msg.id !== undefined) send({ id: msg.id, error: { code: -32601, message: "Not supported" } });
    }
  }

  function watchSize() {
    let last = 0;
    const report = () => {
      const height = Math.ceil(document.documentElement.scrollHeight);
      if (height !== last) {
        last = height;
        notify("ui/notifications/size-changed", { width: document.documentElement.scrollWidth, height });
      }
    };
    new ResizeObserver(report).observe(document.body);
    report();
  }

  window.addEventListener("message", onMessage);

  return {
    async init() {
      const result = await request("ui/initialize", {
        protocolVersion: PROTOCOL,
        appInfo: { name: "Jotted", version: "1" },
        appCapabilities: { availableDisplayModes: ["inline"] },
      });
      hostCapabilities = result.hostCapabilities || {};
      applyContext(result.hostContext || {});
      notify("ui/notifications/initialized");
      watchSize();
      return hostContext;
    },

    async callTool(name, args) {
      const result = await request("tools/call", { name, arguments: args || {} });
      if (result.isError) throw toolError(result);
      return dataOf(result);
    },

    updateModelContext(text) {
      if (!hostCapabilities.updateModelContext) return Promise.resolve();
      return request("ui/update-model-context", { content: [{ type: "text", text }] }).catch(() => {});
    },

    openLink(url) {
      if (typeof url !== "string" || !url.startsWith("https://")) return Promise.resolve();
      return request("ui/open-link", { url }).catch(() => {});
    },

    // The host pushes the tool result that opened the widget (and maybe later ones).
    onToolResult(fn) {
      toolResultListeners.push(fn);
      if (lastResult) fn(lastResult);
    },

    onContextChange(fn) {
      contextListeners.push(fn);
    },

    resultData(result) {
      if (result.isError) throw toolError(result);
      return dataOf(result);
    },

    theme() {
      if (hostContext.theme === "light" || hostContext.theme === "dark") return hostContext.theme;
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    },
  };
})();
