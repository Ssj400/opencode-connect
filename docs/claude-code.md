# Claude Code sessions

Opt-in text messaging for Claude Code sessions, in the terminal or the VS Code
extension, through the same local queue as [local messaging](local-messaging.md).
A Claude Code session can message this node's OpenCode sessions, other Claude Code
sessions on this machine and, through a configured courier, other people. It is not
remote control: no tool runs commands in another session, cancels work, or
transfers files.

## How it works

Claude Code does not tell MCP servers which session calls them, and the session ID
in the server's environment goes stale after `/clear` or `/resume`. Instead:

- **Identity.** A `PreToolUse` hook stamps each call to this server with the calling
  session, keyed by the call's `tool_use_id`. The server receives the same ID as
  `claudecode/toolUseId` and consumes the stamp once. Without the hook, or with a
  missing, expired, or forged stamp, every tool fails closed. Arguments never name
  the caller.
- **Receiving.** Messages for `claude:<uuid>` wait in the queue; the OpenCode relay
  never claims them. A `UserPromptSubmit` hook adds a fixed pointer with their count
  to Claude's context, never their text, and a `Stop` hook keeps Claude going once
  for each message that arrived during its turn. `receive` hands over the oldest
  message and marks it `delivered` in the same transaction, so a failure leaves it
  pending. A remote person's text arrives quoted, as in OpenCode deliveries.
- **Waking.** An `asyncRewake` hook started at each stop watches the queue and wakes
  an idle session once per newly arrived message, with the same pointer. Claude Code
  shows that wake as a "Stop hook blocking error"; nothing failed. One watcher waits
  per session, and Claude Code ends it at the hook's `timeout`: a session idle for
  longer is not woken until its next turn.

## Setup

Run the offline tests and the audit in a complete checkout first:

```bash
python3 -B -m unittest discover -v
python3 -B audit_public.py
```

In the project where you want messaging, add the server with local scope, which
applies only to you, in this project:

```bash
claude mcp add opencode_connect --scope local -- python3 /srv/opencode-connect/claude_mcp_server.py
```

Then merge the hooks and deny rules from
[`examples/claude-settings.local.json.example`](../examples/claude-settings.local.json.example)
into the project's `.claude/settings.local.json`, replacing the example path and
preserving existing settings. Claude Code keeps that file out of commits when it
creates it; if you create it by hand, make sure it is ignored. The `PreToolUse`
matcher must name the server: `mcp__opencode_connect__.*` for the name above.

Start a new session, or reconnect the server in `/mcp`, and check that it shows as
connected. Ask Claude to call `whoami`: it returns `claude:<uuid>` for the current
session. `/status` lists `Project local settings` among the loaded sources.
`python3 bridge.py doctor --local` then checks the queue's contract and backlog and
whether identity stamps are being consumed, without changing anything or needing
`routes.json`.

The server, the hooks, OpenCode's local MCP server, and the relay must share one
queue: the same account, state home, and node. With the defaults nothing needs to be
set. If the OpenCode side sets `OPENCODE_CONNECT_LOCAL_NODE` or
`OPENCODE_CONNECT_LOCAL_STATE_HOME`, pass the same values with `-e NAME=value` to
`claude mcp add`, and prefix each hook command with `env NAME=value`. The queue
contract refuses a mismatched node instead of silently using another queue.

## Sending and replying

`send` takes an explicit address: a bare OpenCode session ID or `NODE:ses_…`,
`claude:<uuid>`, or `person:<name>`. Messages to OpenCode sessions need the
separately started local relay, messages to another Claude Code session wait for its
hooks and `receive`, and messages to people need a configured courier and their
human's approval. When answering, pass the received message's `id` as `reply_to`; the
queue accepts it only for a message from that destination already delivered to the
caller. `find_sessions` locates an OpenCode session by title and reads metadata only.

## Permissions

The example's deny rules stop Claude's file tools, and the shell commands Claude Code
recognizes such as `cat`, from reading or changing the bridge's state and the
courier's configuration, which holds contacts' tokens. They also stop shell commands
that name the administrative command-line tools. These rules are guard rails, not a
security boundary: a script that opens files itself, or a command written another
way, is not matched. For enforcement that does not depend on command text, use
Claude Code's sandbox. Courier approvals additionally require an interactive terminal
and a confirmation code.

Every tool still follows the session's permissions and Plan mode, so by default each
call asks. Allowing `whoami`, `status`, or `receive` lets a session read messages
unattended; leaving `send` to ask keeps a person in front of every outgoing message.
Received text is untrusted data from another assistant or person and grants no
permission.

## Limits

- Correlation, not authentication: any process of the same user can write the queue
  or a stamp. Use trusted accounts and private state directories.
- Without hooks, for example under `--bare` or with hooks disabled, the server refuses
  every call.
- Waking an idle session has been verified in the terminal; in the VS Code panel it
  remains to be confirmed.
- `delivered` means handed over by `receive`, not acted upon or completed.
- Each message a session reads or answers can consume model usage from that person's
  plan or provider, as any other turn does.
- Normal text only: no files, urgent delivery, interruption, or cancellation.
