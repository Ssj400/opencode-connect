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

In each participating OpenCode project, explicitly add a separate MCP server.
For interoperability with an existing bridge that names this same session
namespace `wsl`, configure that canonical node explicitly:

```bash
opencode mcp add local_bridge -- env OPENCODE_CONNECT_LOCAL_NODE=wsl python3 /srv/opencode-connect/local_mcp_server.py
```

Replace the example absolute path with the actual checkout path. Preserve other
MCP configuration; do not replace `laptop_bridge`. The server uses classic MCP,
as does the existing cross-machine server. Reconnect the new server in `/mcps`
and verify `connected` and `whoami`. This repository installs nothing automatically.

The node name is identity, not transport selection. Even when it is `wsl` or
`linux`, this server and its worker use only `LocalCommandTransport`, never SSH.
Choose a node name already understood by a mixed-mode recipient only if it names
the same session namespace. Names are explicit, case-sensitive simple identifiers;
the default `local` preserves standalone use but cannot interoperate with a
cross-machine MCP that recognizes only `linux` and `wsl`. No OS-based inference
or fallback to another transport is performed.

MCP and worker must use the same `OPENCODE_CONNECT_LOCAL_NODE` and bridge state home.
For an isolated test queue, supply a dedicated private absolute
`OPENCODE_CONNECT_LOCAL_STATE_HOME` to both processes rather than using an operational
queue. Do not change `XDG_STATE_HOME` just to isolate the bridge: OpenCode also uses
it for service discovery and storage, so changing it can select or attempt to start
a different service. The bridge-specific variable leaves the CLI environment intact.
Both sessions can use
one project configuration if its scope covers both; do not install duplicate
servers or overwrite existing configuration without checking that scope.

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
`wsl:ses_EXAMPLE` address when configured as `wsl`, never a title. Bare IDs and
`local:ses_EXAMPLE` are input shorthand for the configured node. Foreign node
prefixes are rejected before insertion. All new messages persist and announce
canonical node addresses; abbreviations are never reinterpreted at delivery.
Renaming a session does not redirect
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
OPENCODE_CONNECT_LOCAL_NODE=wsl python3 -B local_bridge.py diagnose
OPENCODE_CONNECT_LOCAL_NODE=wsl python3 -B local_bridge.py status
OPENCODE_CONNECT_LOCAL_NODE=wsl python3 -B local_bridge.py relay
```

Review pending messages before starting the worker. **Delivery can trigger model
responses and consume quota or budget.** `relay --once` also delivers a message;
it is not a harmless health check. One local relay can run at a time. Ctrl+C stops
the worker, not an agent. No daemon or automatic startup is installed.

Local state uses `$OPENCODE_CONNECT_LOCAL_STATE_HOME/opencode-connect/local/.state/`
when explicitly configured, otherwise the corresponding path under the existing
`XDG_STATE_HOME` or `~/.local/state/`. MCP and CLI must run with the same account and state-home
setting to share a queue. It is separate from the cross-machine bridge's state.
The source tree remains free of operational configuration and queues.

### Queue configuration contract

A new empty queue is bound to a contract on its first insertion or claim:
`schema_version`, `mode=local`, and `canonical_node`. Each subsequent insertion
or claim checks that contract inside the same SQLite write transaction. A
mismatch blocks the operation without changing pending messages. Different
clients cannot silently adopt the same queue under different nodes. The worker
uses its configured node, not a node inferred from queued text or an address.

`diagnose` in MCP and CLI reports configured/stored contracts, compatibility, and
the effective database path, without reading messages or consulting an API. An
empty unbound queue reports a null stored contract; diagnosis does not bind it.
These diagnostics contain private runtime paths: do not publish their output.
`whoami` validates node configuration and caller metadata, but does not validate
queue or service identity. Compare diagnostics before starting a worker.

An older queue containing any messages but no contract is rejected for insertion
and claim. This includes historical delivered messages: no automatic adoption,
rewrite, migration, or reenqueue is attempted. Preserve that queue and its
receipts for review; use a separately configured fresh test state location.
`status` and `diagnose` remain available to inspect an incompatible queue.

Stop old local workers and disconnect old local MCP processes before upgrading
or changing configuration. Versions predating contract checks cannot enforce the
new metadata; a new schema cannot retroactively make an old binary reject it.
Do not stop agents, delete state, or reset ambiguous messages to force an upgrade.

### Contract versions and upgrades

Version 2 of the contract adds two address namespaces next to this node's OpenCode
sessions: `claude:<uuid>` for Claude Code sessions and `person:<name>` for remote
people. Rows keep the same schema. Each consumer claims only targets in its own
namespace, so the OpenCode worker never claims `claude:` or `person:` rows.
Remote people can only enter the queue through the courier, never as a source
passed to `send`.

A version 1 queue is refused until `python3 local_bridge.py migrate` upgrades it.
Migration rewrites only the contract version: every row stays untouched, stale
`sending` rows included, and a queue bound to another node or to an unknown
version is refused. Version 1 code then refuses the upgraded queue, so upgrade
every local worker and MCP process together. `diagnose` reports
`migration_needed`.

`python3 local_bridge.py resolve ID --state delivered|failed|cancelled` records a
human-reviewed outcome for a stale `sending` or `uncertain` message. It never resends.

### Service selection limitation

The contract compares configuration, **not service identity or authentication**.
Use one queue per administratively shared local CLI discovery context: same
account, environment, and agreed OpenCode configuration. The CLI executable is
resolved from `PATH` and inherits process context; matching node names, database
paths, binaries, versions, PIDs, or project directories do not prove that two
processes discover the same API service. `/api/info` and session existence checks
do not provide a stable service identity. Multiple independently discovered
services require separate queues and are not technically isolated by this fix.
No endpoint-selection flags, service UUIDs, or identity guarantees are invented.

Canonical addresses allow the existing cross-machine MCP to understand a reply,
but that MCP may still use its Linux relay. A mixed-mode reply is not proof of
SSH-free delivery. Both sessions must use this local server and shared local
worker to test a fully local round trip; replies remain explicit, never automatic.

The worker checks local service availability before claiming a message. If that
check fails, messages remain pending. If access fails after the check but during
the destination GET, the claimed message becomes failed, not uncertain.

Messages follow `pending -> sending -> delivered | failed | uncertain`. `delivered` means
API acceptance, not a completed task. Queue delivery uses OpenCode's normal queue
semantics; it does not promise immediate handling by a busy session. A failed or
timed-out request may already have been accepted. Neither uncertain messages nor
stale sending messages are retried automatically. Proven pre-send failures (for
example, a deleted or archived destination) are recorded as `failed`, also without
automatic retries. Only preflight checks use this distinction: errors during or
after the POST remain `uncertain`. Preserve state for manual review.

The optional `time.archived` numeric field is defined by the published V2
`Session.Info` schema. Offline fixtures cover it; no real session is archived by
the tests, and live behavior with archived sessions remains unverified.

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
