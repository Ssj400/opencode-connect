# Security and limitations

## Trust model

The opt-in [local messaging mode](docs/local-messaging.md) uses only the local
OpenCode service and separate private runtime state. Session discovery exposes
titles, IDs and directories, never conversation content. Treat these metadata as
private; do not publish discovery output. Selecting a title is not authorization
to control that session. Existing permissions remain in force.

A personal tool for two trusted machines and system accounts: Linux coordinator
→ Windows SSH → WSL. MCP uses stdio and does not open an additional network port.
The OpenCode API remains local and is accessed through `opencode api`.
The `ai.opencode/sessionID` metadata provides correlation, not authentication.
The allowlist reduces routing mistakes; it does not isolate hostile local users.
Anyone who can edit scripts, configuration, runtime state, or SSH commands already
controls the bridge. Do not expose this RPC or MCP server as a public or multi-user service.

## Implemented protections and limitations

- Chat accepts shortcuts and exact session addresses on paired nodes. Paths, usernames,
  and OpenCode session IDs are validated; callers cannot name arbitrary remote hosts.
- Both file endpoints require explicit registration at enqueue and delivery. A newly
  permitted chat session does not inherit permission to transfer files or execute actions.
- JSON travels over stdin; messages and paths are not interpolated into Windows shell commands.
- Host verification stays enabled; internal OpenCode credentials are never copied.
- The control socket lives in a private user directory, not a shared temporary directory.
- SQLite and mailboxes are private data. Installation paths and the environment must
  belong to the user and must not be writable by others.
- Files are limited to 1 MiB, checked with SHA256, and created without overwriting.
  A matching hash confirms integrity, not content trustworthiness or authenticity.
- Symlink checks do not fully defend against races caused by malicious processes
  running as the same user. Do not share this installation between untrusted users.
- Blocking some credential filenames is only an additional precaution; it does not
  detect secrets inside harmless-looking files. Review file contents manually.
- Each machine is limited to 30 sends per hour; this is not a spending guarantee.
- Ambiguous POSTs are not retried. Exactly-once delivery is not guaranteed.
  A received file can remain without a notification if the subsequent POST fails.
- There is no automatic reconciliation of `sending`/`uncertain` states or agent cancellation.
- Existing session permissions remain in force. `create-session` requests `ask`
  permissions; it neither enables tools automatically nor starts model generation.
- OpenCode's prompt endpoint can activate the receiving session with its existing tools.
  Chat scope instructions are not an enforced text-only sandbox. Use trusted paired
  accounts and do not present this prototype as cross-user permission isolation.
- Deployment checks remote changes against a manifest and hashes. Files are replaced
  atomically one at a time, not as a transaction across the entire tree. Partial
  failures can require manual review. Never delete the manifest to force an update.

## Data excluded from publication

`routes.json`, `.state/`, mailboxes, sockets, logs, backups, tokens, private identities,
operational IPv4/IPv6 addresses, VPN host names, SSH fingerprints, real session/message
IDs, Claude Code session UUIDs, and execution reports.
`audit_public.py` uses an allowlist and heuristics; it does not replace human review
or a secret scanner. Do not treat it as certification that no private data exists.
Names it cannot recognize belong in its private deny-list, `OPENCODE_CONNECT_AUDIT_TERMS`.

## Incident handling

Stop the relay and disconnect the affected MCP server without cancelling agents.
Preserve state and review destinations and messages before any resend. Do not attach
SQLite databases or unsanitized logs. A private vulnerability reporting channel
must be agreed with the owner before a stable release; never post secrets in public issues.
The initial repository is an experimental prototype, not a hosted service offering.
