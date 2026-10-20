# Core and adapter boundaries

The public edition separates delivery policy from harness APIs and command transport.
This refactoring does not add another operating system or harness adapter.

## Layers

```text
MCP / CLI
    |
collab.py: application wiring, configured peers, mailbox storage, Linux relay process
    |
core.py: route checks, message envelope, capability checks, one-shot delivery outcomes
    |                                  |
AgentAdapter                       MailboxAdapter / file receiver
    |                                  |
opencode_adapter.py                local storage or mailbox/file RPC
    |
CommandTransport
    |
transports.py: local processes or Windows SSH -> WSL
```

- `core.py` uses only `dataclasses` and `typing`. It does not read configuration,
  access files, execute commands, know API routes, or recognize a harness session-ID
  prefix. A peer has an opaque session identifier and a node identifier.
- `OpenCodeAdapter` maps delivery and session creation to OpenCode V2 endpoints.
  `OpenCodeCli` maps API requests to the existing `opencode api` command. API error
  interpretation belongs here, not in generic JSON transport.
- `CommandTransport` describes JSON command execution. Local and Windows/WSL
  implementations retain the existing stdin payload and SSH verification behavior.
- `bridge.Endpoint` remains a compatibility facade for application wiring and
  deployment; `connect` and `check` keep their existing command-line interface.
- `collab.py` owns existing configuration, SQLite storage, file staging, and the
  Linux relay lifecycle. It translates configured `sessionID`/`side` values into
  generic peers and supplies the appropriate adapters. These infrastructure pieces
  are not claimed to be cross-platform yet.

## Contracts

An agent adapter exposes `supported_deliveries` and
`send_message(session, message_id=..., text=..., delivery=...)`. It returns an
acceptance ID, not completion. The core rejects unsupported delivery modes before
transferring files or sending a message: there is no silent steer-to-queue fallback.

A mailbox adapter exposes `claim()` and `mark(id, state, result)`. The core claims
at most one message per call, sends at most once, and records acceptance or an
uncertain outcome. If saving a receipt fails, that error propagates; the claimed
message remains retained for manual reconciliation. Stopping the relay never
calls an agent cancellation API.

A file receiver accepts a message ID and the existing attachment payload and
returns a destination path and hash. It must enforce file limits, verify integrity,
avoid overwriting, and never execute content. Transfer happens before notification.

## Preserved behavior

- Same routes configuration, MCP tools, SQLite schema, IDs, and CLI commands.
- Existing Linux -> Windows SSH -> WSL topology; no migration of personal state.
- Same OpenCode message envelope, queue/steer values, and ask permissions for new sessions.
- Ambiguous sends are not retried. Acceptance does not imply task completion.
- No daemon, implicit installation, automatic responses, or permission escalation.

## Next adapters

Linux-to-Linux and Windows-to-Windows are the next platform milestones; macOS
comes later. Platform work belongs in transport and infrastructure adapters.
Future harness adapters must implement the agent contract and advertise only the
delivery semantics they actually support. None of those future adapters, bilateral
consent agreements, or interaction-mode policies are implemented by this refactor.

Core tests use arbitrary node names and opaque session IDs without processes,
SSH, OpenCode, model calls, or network access. Adapter and compatibility tests
verify the existing OpenCode request and transport shapes separately. Live
integration of this public edition remains a separate, explicitly authorized step.
