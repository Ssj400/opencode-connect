# Local OpenCode messaging

Opt-in text messaging between OpenCode V2 sessions in the same local service,
including two sessions inside the same WSL environment. This mode does not use
SSH, `routes.json`, a remote machine, or the cross-machine relay. It does not
establish native Windows support or bridge separate services automatically.

## Setup

Use a complete public checkout and run the offline tests first:

```bash
python3 -B -m unittest discover -v
python3 -B audit_public.py
```

In each participating OpenCode project, explicitly add a separate MCP server:

```bash
opencode mcp add local_bridge -- python3 /srv/opencode-connect/local_mcp_server.py
```

Replace the example absolute path with the actual checkout path. Preserve other
MCP configuration; do not replace `laptop_bridge`. The server uses classic MCP,
as does the existing cross-machine server. Reconnect the new server in `/mcps`
and verify `connected` and `whoami`. This repository installs nothing automatically.

The local CLI uses `opencode api` and its normal local discovery/authentication
context. Sessions in another account, WSL distribution, explicitly separate server,
or native Windows installation are not assumed to share that context.

## Selecting a destination by title

Ask the assistant to find the session by name. `find_sessions` reads only session
metadata, not messages, context, logs, or transcripts. It returns one page of up to
25 session records, filtered to omit the caller, child sessions and archived
sessions. Each candidate exposes only its ID, title, directory and exact-title
match flag. Exact case-insensitive matches are ordered first within each page.

Follow `next_cursor` with the same search until `complete` is true before declaring
a title unique. Ask the user to choose when matches are ambiguous; do not silently
select the first or most recently updated session. Directory and ID distinguish
duplicate titles. No matching destination means no send and no session creation.

Search and send are separate operations: `send` accepts a selected session ID or
`local:ses_EXAMPLE` address, never a title. Renaming a session does not redirect
the message. Destination existence is rechecked at delivery. Deleted or inaccessible
destinations are retained for manual review, not replaced with another title match.
Archived and child sessions are also rejected at delivery, even if the sender
provided their IDs directly. Checks cannot make session lookup and POST atomic;
the target may change between those requests.

CLI metadata-only search:

```bash
python3 -B local_bridge.py find 'API review'
```

Do not publish search results: real titles, directories and identifiers are private
runtime data. None belong in public fixtures, examples, or execution reports.

## Queuing and delivering

The MCP `send` tool uses OpenCode caller metadata, queues authorized text, and
returns immediately with `pending`. Both assistants can explicitly send replies
to the `Reply address` in received messages. There is no automatic conversation
loop or transcript forwarding. A separate local worker delivers queued messages:

```bash
python3 -B local_bridge.py status
python3 -B local_bridge.py relay
```

Review pending messages before starting the worker. **Delivery can trigger model
responses and consume quota or budget.** `relay --once` also delivers a message;
it is not a harmless health check. One local relay can run at a time. Ctrl+C stops
the worker, not an agent. No daemon or automatic startup is installed.

Local state uses `$XDG_STATE_HOME/opencode-connect/local/.state/` or the equivalent
under `~/.local/state/`. MCP and CLI must run with the same account and state-home
setting to share a queue. It is separate from the cross-machine bridge's state.
The source tree remains free of operational configuration and queues.

Messages follow `pending -> sending -> delivered | uncertain`. `delivered` means
API acceptance, not a completed task. Queue delivery uses OpenCode's normal queue
semantics; it does not promise immediate handling by a busy session. A failed or
timed-out request may already have been accepted. Neither uncertain messages nor
stale sending messages are retried automatically. Preserve state for manual review.

## Boundaries

- Normal text only; no files, urgent delivery, interruption, or cancellation.
- No sending to the caller's own session. Maximum 12000 characters per message
  and 30 sends per hour across this local queue; neither limit guarantees spend.
- Session metadata correlates callers, not authentication against hostile local users.
  Use trusted accounts and private state directories, as in the existing trust model.
  The administrative CLI accepts an explicit `--source` ID; it does not authenticate
  ownership of that session. Do not expose either interface to untrusted users.
- Received text is untrusted content, not a system instruction or permission grant.
  Existing session permissions and Plan mode remain in force.
- Scope guidance is not a text-only sandbox. The target assistant may have tools;
  authorize the message and its scope before sending.
- No model-backed integration checks run automatically. Mocked tests do not certify
  delivery between real sessions; perform a separately authorized live check before
  claiming the installation works end to end.

API contract: [OpenCode V2 API](https://opencode.ai/v2/docs/api), including
`GET /api/session` with `search`, `limit`, `parentID`, and `cursor`,
`GET /api/session/{sessionID}`, and queued `POST /api/session/{sessionID}/prompt`.
