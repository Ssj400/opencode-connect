#!/usr/bin/env python3
"""Classic MCP over stdio: local tools without an additional network port."""

import json
import os
import sys

from bridge import BridgeError
from collab import Mailbox, ROOT, settings


INSTRUCTIONS = """Directed communication between user-authorized OpenCode sessions.
Use send only for a necessary task or notice within the authorized scope.
Normal: queue; urgent=true: steer, only for a real urgent blocker or change.
Do not cancel agents, expand tasks, or change Plan mode through this channel.
Do not forward responses or reply to acknowledgments automatically: avoid loops.
Do not wait for replies in a polling loop; continue your independent task.
send_file only reads files explicitly staged in the bridge's outgoing/ directory.
Delivery requires the Linux relay and SSH to be running; pending is NOT delivered.
Check status; stale sending or uncertain requires review, never blind retries.
Read AGENTS.md in this server's directory for context and boundaries.
"""


def tool_schema(name, description, properties, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": list(required),
        "additionalProperties": False,
    }}


SEND_PROPERTIES = {
    "to": {"type": "string", "description": "Registered alias on the other machine; check peers."},
    "text": {"type": "string", "minLength": 1, "maxLength": 12000},
    "urgent": {"type": "boolean", "default": False, "description": "Explicit urgent notices only: steer instead of queue."},
}
TOOLS = [
    tool_schema("peers", "List registered sessions without reading their conversations.", {}),
    tool_schema("status", "Status of this machine's last 30 sends; do not retry uncertain messages.", {}),
    tool_schema("send", "Send an explicit message to the other OpenCode; does not wait for a reply.", SEND_PROPERTIES, ("to", "text")),
    tool_schema("send_file", "Transfer a staged outgoing/ file (maximum 1 MiB), without overwriting or executing it.",
                {**SEND_PROPERTIES, "filename": {"type": "string", "description": "Simple filename without a path."}},
                ("to", "text", "filename")),
]


def call_tool(params, root=ROOT):
    name = params["name"]
    args = params.get("arguments", {})
    if name == "peers":
        return settings(root)["peers"]
    box = Mailbox(root)
    try:
        if name == "status":
            return box.status()
        if name not in ("send", "send_file"):
            raise BridgeError("Unknown tool.")
        allowed = {"to", "text", "urgent"} | ({"filename"} if name == "send_file" else set())
        if set(args) - allowed:
            raise BridgeError("Unknown arguments.")
        session_id = params.get("_meta", {}).get("ai.opencode/sessionID")
        if not session_id:
            raise BridgeError("Missing ai.opencode/sessionID metadata; the source session will not be guessed.")
        return box.enqueue(session_id, args["to"], args["text"], args.get("urgent", False),
                           args["filename"] if name == "send_file" else None)
    finally:
        box.db.close()


def handle(request, root=ROOT):
    method = request.get("method")
    if "id" not in request:
        return None
    reply = {"jsonrpc": "2.0", "id": request["id"]}
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion")
        version = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-11-25"
        reply["result"] = {"protocolVersion": version,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "opencode-connect", "version": "0.1.0"},
                           "instructions": INSTRUCTIONS}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            result = call_tool(request.get("params", {}), root)
            reply["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except (BridgeError, KeyError, TypeError, ValueError, OSError) as exc:
            reply["result"] = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        reply["error"] = {"code": -32601, "message": "Method not found"}
    return reply


def main():
    os.umask(0o077)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle(request)
        except (ValueError, AttributeError, TypeError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
