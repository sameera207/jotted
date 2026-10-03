"""`jotted mcp`: Jotted's operations as MCP tools, on stdio, for Claude, Codex and other agents.

Each tool is a CLI command: its input schema comes from that command's arguments in
`jotted schema`, and a call runs the command with --json in this process (`cli.invoke`),
returning its `data`, or its `error` as a tool error. So a new command needs only a line
in TOOLS to become a tool.

Nothing that takes a secret is exposed (`ai key`, `connect`): keys don't pass through an
agent. `setup_status` tells the agent which command the person should run.

The protocol is JSON-RPC 2.0, one message per line (MCP's stdio transport). Only tools
are offered.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from . import contract

PROTOCOL = "2025-06-18"

# tool -> (command, description); arguments are the command's own
TOOLS: dict[str, tuple[str, str]] = {
    "items_list": ("items list", "The to-do list: items found in handwritten notes, and ones added by hand."),
    "items_add": ("items add", "Add an item to the to-do list."),
    "items_edit": ("items edit", "Change an item's text."),
    "items_done": ("items done", "Mark an item done."),
    "items_reopen": ("items reopen", "Mark an item open again."),
    "items_dismiss": ("items dismiss", "Take an item off the list: it isn't an action (one added by hand is deleted)."),
    "library": ("library", "The device's folders and documents, and which are watched."),
    "watch": ("watch", "Watch or stop watching a folder or document; from-now skips what is already written."),
    "settings_get": ("settings", "Jotted's settings: watched folders, thresholds, the To-do document."),
    "settings_set": ("settings set", "Change one setting. The value is JSON (true, 0.8, [\"/A\"]) or plain text."),
    "check": ("check", "Read what changed in watched folders and update the To-do document, now."),
    "status": ("status", "What Jotted reads, judges and publishes, and when it last did."),
    "setup_status": ("setup status", "Setup steps, done or not. A step that isn't done names the command the "
                                     "person should run in a terminal (keys and codes never go through an agent)."),
}
NEVER = {"ai key", "connect", "auth"}  # take a secret


def _commands() -> dict[str, dict]:
    from . import cli, schema

    return {c["command"]: c for c in schema.commands(cli.build_parser())}


def tools() -> list[dict]:
    commands = _commands()
    out = []
    for name, (command, description) in TOOLS.items():
        assert command not in NEVER
        props, required = {}, []
        for a in commands[command]["arguments"]:
            prop = {k: v for k, v in a.items() if k in ("type", "enum", "default")}
            if a.get("help"):
                prop["description"] = a["help"]
            props[a["name"]] = prop
            if a["required"]:
                required.append(a["name"])
        out.append({"name": name, "description": description,
                    "inputSchema": {"type": "object", "properties": props, "required": required,
                                    "additionalProperties": False}})
    return out


def argv_for(name: str, arguments: dict) -> list[str]:
    """The command line for a tool call."""
    command, _ = TOOLS[name]
    spec = _commands()[command]["arguments"]
    known = {a["name"] for a in spec}
    unknown = set(arguments) - known
    if unknown:
        raise contract.UsageError(f"{name}: unknown argument(s) {', '.join(sorted(unknown))}")
    argv = command.split()
    options: list[str] = []
    for a in spec:
        if a["name"] not in arguments or arguments[a["name"]] is None:
            continue
        value = arguments[a["name"]]
        if a.get("positional"):
            argv.append(value if isinstance(value, str) else json.dumps(value))
        elif a.get("type") == "boolean":
            options += [a["flags"][-1]] if value else []
        else:
            options += [a["flags"][-1], str(value)]
    return argv + options


def call(name: str, arguments: dict) -> dict:
    """A tools/call result."""
    from . import cli

    if name not in TOOLS:
        envelope = contract.fail("usage", f"no tool {name!r}")
    else:
        try:
            envelope = cli.invoke(argv_for(name, arguments or {}))
        except contract.UsageError as e:
            envelope = contract.error_of(e)
    if envelope["ok"]:
        return {"content": [{"type": "text", "text": json.dumps(envelope["data"], ensure_ascii=False, default=str)}],
                "isError": False}
    return {"content": [{"type": "text", "text": json.dumps(envelope["error"], ensure_ascii=False)}], "isError": True}


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    method, msg_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if msg_id is None:  # notifications/initialized, cancellations: nothing to answer
        return None
    try:
        if method == "initialize":
            result: Any = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "jotted", "version": contract.release()},
                           "instructions": "Jotted turns handwritten notes into a to-do list. Use items_list to "
                                           "read it; setup_status says what the person still has to set up."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools()}
        elif method == "tools/call":
            result = call(params.get("name", ""), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"no method {method!r}"}}
    except Exception as e:  # noqa: BLE001 - the server keeps answering
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            response = handle(message) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches aren't supported"}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
            stdout.flush()
    return 0
