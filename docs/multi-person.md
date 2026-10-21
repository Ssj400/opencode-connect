# Federated person-to-person messaging

This opt-in courier exchanges **text only** between independently owned machines.
There is no central mailbox, shared SSH shell, remote execution endpoint, or
required VPN vendor. Each recipient holds inbound messages on their own machine
until an operator approves them for a local session, unless that operator chose
direct delivery for the contact. Receipt by a courier is not receipt by an
assistant, and approval is not task completion.

## Connecting two people

Each person runs these on their own machine, in a terminal, from a complete
checkout. Nothing is installed or opened without a confirmation code.

| When | Command | What it does |
| --- | --- | --- |
| Once per machine | `python3 bin/courier.py setup` | Checks the machine, asks a name and address, writes the configuration, prints the firewall and MCP commands, offers the service |
| Once per contact | `python3 bin/courier.py pair`, then `pair 'LINK'` on the other side | Connects both couriers in both directions |
| Optionally, per contact | `python3 bin/courier.py contact bob --to SESSION` | Proposes, or with `--direct` delivers, bob's messages to one session |
| Daily | nothing; `status` shows the state | The service runs the courier from login |
| Per held message | `python3 bin/courier.py approve` | Shows what is held and approves it with one code |
| To stop | `python3 bin/courier.py unpair bob` | Revokes both directions on this side |

### 1. `setup`, once per machine

`setup` first checks the machine and reports each finding:

- the local queue, which must not belong to another node (`OPENCODE_CONNECT_LOCAL_NODE`);
- whether systemd runs a manager for this user, and whether *linger* is on. Without
  linger the service runs from login to logout; `loginctl enable-linger` keeps it
  running, and `setup` only suggests it;
- whether `opencode` is on the `PATH`, which the service keeps to deliver to
  OpenCode sessions.

Then it asks for a name, as contacts will see it, and offers the IPv4 addresses
of this machine on a VPN (100.64/10, as mesh VPNs use) and then on a LAN (RFC 1918).
It leaves out loopback, container and virtual machine bridges, links that are
down, and public addresses. It checks that the chosen port can be bound, and
writes `courier.json` after a confirmation code. An existing configuration is
never overwritten: `setup` then only repeats the checks and the commands below,
and `address` changes the address.

Finally it prints, and never runs:

- **firewall commands** that admit only the chosen network to the listener: for
  `ufw` or `firewalld` when either is installed, and on WSL the Windows Hyper-V
  rule. A LAN rule names its subnet; a mesh VPN that gives each machine a `/32`
  gets the VPN's range. Review them and run them yourself;
- **MCP commands** for OpenCode (`opencode mcp add local_bridge …`) and Claude Code
  (`claude mcp add opencode_connect …`), with this terminal's queue variables.
  Claude Code also needs the hooks from
  [`examples/claude-settings.local.json.example`](../examples/claude-settings.local.json.example);
  see [Claude Code sessions](claude-code.md);
- an offer to install the service, which takes another code. Pressing Enter skips it.

### 2. `pair`, once per contact

One person runs `python3 bin/courier.py pair`. It prints a link such as
`http://192.0.2.10:8765#ABCD-EFGH-JKMN`, open for ten minutes. They send it
privately; the other person runs `python3 bin/courier.py pair 'LINK'`. Both
couriers then hold each other's card, and `pair` reports when both ways work.
Whoever uses the link first becomes the contact; `pair --for bob` accepts only
bob's courier. See [Pairing](#pairing) for how the code is used.

### 3. Every day

With the service, nothing: it starts the courier at login and restarts it after
a failure. `status` shows who this courier is, whether the service and listener
run, and per contact the directions, the route, the last contact or failure, and
what waits on either side. Held messages raise a desktop notification and wait
for `approve`, unless the contact is `direct`.

`unpair bob` revokes both directions on this side after a code, cancels unsent
messages, and removes bob's route. What bob's courier sends afterwards fails at
once; ask bob to run `unpair` with your name too.

## Implementation status

Implemented: the HTTP courier, contacts and approval, the real local queue v2
adapter and MCP person targets, read-only diagnostics (B1–B6), and the simplified
flow: `setup`, `pair`, the optional service, per-contact routes, `status`, and
`unpair`. A LAN acceptance test (B7) of the manual flow passed. The simplified
flow needs its own separately authorized live test. Offline tests cover both an
injected `FakeQueue` and real `LocalMailbox` instances, including independent
CLI/worker connections, crash recovery, and racing revocation. Publishing the
source does not activate a listener or modify a personal installation.

Run offline tests with Python 3.12 or newer:

```sh
python3 -B -m unittest discover -v -s tests/courier -t .
python3 -B -m unittest discover -v
python3 -B tools/audit_public.py
```

## Trust and network boundaries

- Run only on an explicitly authorized private LAN or VPN interface. Bind the
  interface's specific IP, not all interfaces. Wildcard binds are rejected unless
  `--allow-wildcard` is explicitly supplied; that flag is not firewall permission.
- The firewall must admit only the chosen interface, port, and network. `setup`
  prints such rules and never changes a firewall, SSH setting, route, or VPN
  configuration. Do not expose this listener on the public Internet.
- Plain HTTP depends on the trusted network path. On an untrusted LAN it exposes
  bearer credentials and message content; use HTTPS with an independently managed
  authenticated TLS endpoint or a trusted encrypted VPN. `setup` labels LAN
  addresses as unencrypted, and `pair` warns when the listener is outside VPN
  ranges. The courier does not implement TLS certificates, peer discovery, NAT
  traversal, or VPN setup.
- A contact's inbound token authorizes holding messages, reading statuses for
  **that contact's messages only**, and querying the courier identity. It never
  authorizes local delivery, conversation reads, file transfers, or shell access.
- Consent is bilateral: each side stores the other's card only after its own
  operator ran `pair` or `trust`.
- A card contains a bearer token. This side keeps only the SHA-256 digest of the
  token it issued. Never publish a card, log it, or paste it in an agent chat.
- Local processes with access to the courier state or an unrestricted shell are
  within the operator's trust boundary. TTY plus a fresh confirmation code guards
  against accidental/noninteractive approval, **not a hostile local process**.
  Permissions and deny rules are operational guidance, not an enforced sandbox.

## WSL: mirrored networking only

Under WSL's default NAT networking, other machines cannot reach a listener inside
WSL. The courier supports WSL in **mirrored** mode, which needs Windows 11 22H2 or
later. On WSL, `setup` leaves out 172.16/12 addresses, which are WSL's NAT or, in
mirrored mode, Windows' virtual switches. When nothing else remains, it explains
the switch:

1. In Windows, add `networkingMode=mirrored` under `[wsl2]` in
   `%UserProfile%\.wslconfig`.
2. In Windows, run `wsl --shutdown`. It closes every open WSL window and session.
3. Open WSL again and run `setup` again.

If 172.16/12 is the range of your actual LAN, `setup --allow-172` offers those
addresses. In mirrored mode, Windows' Hyper-V firewall filters inbound
connections to WSL: `setup` prints a `New-NetFirewallHyperVRule` command, for an
administrator PowerShell, that admits only the chosen network to the port.

A WSL courier listens only while a WSL session is open. Senders' couriers keep
retrying with backoff until then, and `status` says so.

## The service

```sh
python3 bin/courier.py service install
python3 bin/courier.py service status
python3 bin/courier.py service logs
python3 bin/courier.py service stop
python3 bin/courier.py service uninstall
```

The optional systemd user unit runs `courier.py run --relay`: the courier, and
this machine's local relay, which delivers approved messages to its OpenCode
sessions. `install`, `uninstall`, `start`, and `stop` take a confirmation code;
`status` and `logs` only read. `install` writes the unit once, with absolute paths,
and never overwrites a different one. The unit keeps the installing terminal's
queue and XDG variables, and a `PATH` with the folder of its `opencode`, so run
`install` where `opencode` works. It restarts after a failure, and waits for a
listen address that is not assigned yet, such as a VPN that is still connecting.
Stopping it lets a delivery in progress finish. Delivering approved messages can
start agents and use model quota.

Only one relay delivers a queue. If a separate `local_bridge.py relay` runs, the
service's relay waits for it to stop; `status` shows that relay's PID.

Without the service, run the same in a terminal and keep it open:

```sh
python3 -B bin/courier.py run --relay
```

`run` without `--relay` serves only the courier; the local relay then runs
separately as `python3 -B bin/local_bridge.py relay`. A `claude:UUID` destination
is delivered by that session's hooks and `receive`, not by the relay; an approval
alone does not establish that it is running.

## Pairing

`pair` connects two couriers in both directions with one command on each side.
On the first machine it opens a one-time offer for ten minutes and prints a link;
the other person runs `pair 'LINK'` on theirs. To pair again with an existing
contact, such as after `address`, add `--replace` on both sides.

The 60-bit code after `#` never leaves either machine. Both sides derive a key
from it with PBKDF2, and each proves it knows the code with an HMAC over its own
card, so neither stores a card from a courier that does not know the code. A
wrong code writes nothing. Each card's URL must name the IP its request came
from, which also keeps a joiner from pointing this courier at another host. The
inviter's trust stays provisional until the joiner confirms with its new token;
an offer that lapses or is interrupted without that confirmation is undone, and
a replaced contact is restored. Revoking a contact ends a pairing in progress
with it. Finally the inviter checks the way back by asking the joiner's courier
who it is. `POST /v1/pair` is the only route that needs no token: it answers 404
while no offer is open, accepts at most 1 KiB, and is throttled in memory per
address and in total.

When no courier runs, as when the service is not installed, `pair` serves in
the foreground while it waits.

## Routes and approval

```sh
python3 -B bin/courier.py inbox
python3 -B bin/courier.py inbox --watch
python3 -B bin/courier.py contact bob --to wsl:ses_EXAMPLE
python3 -B bin/courier.py approve
python3 -B bin/courier.py approve MESSAGE_ID --to wsl:ses_EXAMPLE
python3 -B bin/courier.py reject MESSAGE_ID
```

`setup`, `pair`, `unpair`, `contact`, `address`, `approve`, `reject`, the
`service` commands that change something, and the manual commands below require
interactive stdin **and** stdout. Each change they make is confirmed by retyping
a newly displayed five-letter code; `pair` is confirmed instead by the code it
prints, which only the other person receives. No flag skips confirmation; the
only standing approval is a `direct` route, which is itself set with a code.

Each held message has a destination: the session that wrote the message it
answers, else the contact's route, set with `contact`. `approve` without an ID
shows every held message with its sanitized text and destination, then asks for
one code. It approves only the messages it showed, each to the destination shown;
messages without a destination stay held. `approve MESSAGE_ID` approves one
message, and `--to` overrides its destination. Supported destinations are
`NODE:ses_ID` and `claude:UUID`; person targets cannot be used for approval or
automatic forwarding. The real adapter additionally validates the local
namespace; the local relay checks session availability before delivering
anything to OpenCode.

A route is `ask` by default. `contact bob --to SESSION --direct` approves bob's
messages on arrival, up to 20 an hour; the rest are held, because each delivery
can start an agent and use model quota. A direct approval that fails, such as for
a route to a session this queue does not serve, also leaves the message held.
`contact bob --to SESSION --ask` revokes direct delivery, and `unpair` removes
the route.

Inbox output removes escape sequences, control characters, and invisible Unicode
formatting controls from remote text. Original text is retained for the explicitly
approved recipient, not executed. Treat all remote content as untrusted data.
Held messages expire after seven days, including while the listener is idle;
expiration is checked before approval and status/inbox reads. Expiration changes
eligibility, not data erasure: deduplication/status tombstones persist. When a
message is held, the courier shows a desktop notification with `notify-send` if it
is available. The notification names the contact, never the text. `--watch` never
approves a message.

## Configuration, address, and revocation

The default configuration is
`$XDG_CONFIG_HOME/opencode-connect/courier.json`, falling back to
`~/.config/opencode-connect/courier.json`. The file must be owned by the operator,
regular (not a symlink), and mode **0600**, in a private directory. It is separate
from `routes.json` and does not alter local or cross-machine bridge configuration.
It contains `person`, `listen`, `url`, `tokens` (name to token digest), and
`contacts` (name to received card). Names are lowercase ASCII identifiers.

The shared SQLite database is authoritative for token revocations/contact changes;
`courier.json` is an atomically replaced private snapshot. After a crash between
the database commit and snapshot replacement, startup repairs the snapshot from
SQLite, never restoring an old revoked token from JSON. Do not edit contact maps
by hand. Change the listener or URL with `address --listen IP:PORT --url URL`: it
updates the database, then the snapshot, and restarts the service if it runs. If
it is interrupted, run it again. Contacts keep the old URL until they receive a
new card, as from `pair --replace`. The person name never changes; any other
mismatch between the snapshot and the database requires operator review.

`unpair bob`, like `untrust bob`, removes the received card, revokes the issued
token, removes the route, and cancels held/approved-but-unsent inbound and pending
outbound messages from/to that person in the **same SQLite transaction**. It
cannot retract delivered messages or an already in-flight HTTP request.
Reissuing an invitation rotates the inbound token; the previous token stops
authenticating. Contact lists never display tokens.

## Status and diagnostics

```sh
python3 -B bin/courier.py status
python3 -B bin/courier.py doctor
python3 -B bin/courier.py doctor --offline
```

`status` is for people. It reads the queue through a read-only connection, asks
`systemctl --user is-active` only when the unit is installed, connects to its own
listener, and reads `/proc` for a separate relay of this user. It never prints
tokens or message text, and changes nothing.

`doctor` is for diagnosis. It validates private configuration, database and
existing SQLite sidecar permissions, held messages/expiry due, incomplete sending
claims, and retry-due uploads. It also reports all local queue namespace counts so
unclaimed outbound work is visible. Those aggregate counts may include unrelated
local deliveries.

By default it probes the configured TCP listener and each outbound contact's
authenticated `/v1/whoami`. TCP reachability alone cannot prove the listener is
this courier; contact probes independently compare the expected person/protocol.
`--offline` makes no connections. Identity probes consume rate-limit budget but
do not submit text or invoke a model. Findings never include tokens, cards, or
remote response content. A stale JSON contact snapshot is not used to probe a
revoked contact: SQLite remains authoritative.

Doctor opens SQLite read-only. It does not create directories or queues, migrate,
claim, release, expire, approve, send, repair snapshots, start services, or acquire
worker locks. `FAIL` exits with status 1; `WARN` is advisory. Missing state is a
warning, not permission to initialize it automatically.

## Advanced: the manual path

`setup` and `pair` replace these steps, which remain available:

```sh
python3 -B bin/courier.py init --person alice --listen 192.0.2.10:8765 --url http://192.0.2.10:8765
python3 -B bin/courier.py invite bob
python3 -B bin/courier.py trust bob --card -
python3 -B bin/courier.py contacts
```

`invite` displays a fresh token once, in a card for the other person. `trust
--card -` pastes the card they sent through a hidden terminal prompt. Avoid an
inline `--card '{...}'`: command arguments can be visible to other local processes
and shell history. Cards have exactly `person`, `url`, and `token`. Exchange them
over a separate private channel, and verify the intended person and endpoint
independently. Displaying a card does not transmit it.

The courier, local MCP, and local delivery worker must use the **same**
`OPENCODE_CONNECT_LOCAL_NODE` and `OPENCODE_CONNECT_LOCAL_STATE_HOME`. The latter
isolates the queue without changing OpenCode service discovery: do not change
`XDG_STATE_HOME` merely to isolate the courier. Use `--config /private/path/courier.json`
for a test identity instead of changing `XDG_CONFIG_HOME`, which OpenCode also uses.
Only one person identity may own a queue. Do not reuse an operational queue for an
acceptance test.

For an existing v1 queue, stop/reload only the affected local MCP/worker processes,
review their state, and explicitly migrate as documented in
[local messaging](local-messaging.md). There is no silent migration. A new isolated
queue binds to v2 without touching existing state.

## Agent deny rules

In each agent's permission policy, deny courier administration and sensitive file
access. Apply rules to alternate entry points too (shell interpreters, Python
imports, direct SQLite access, copies and symlinks). For example, these
[OpenCode V2 rules](https://opencode.ai/v2/docs/permissions) cover the script and
default configuration/state paths (merge them **after** broader allowances):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "permissions": [
    { "action": "shell", "resource": "*courier.py*", "effect": "deny" },
    { "action": "read", "resource": "~/.config/opencode-connect/*", "effect": "deny" },
    { "action": "edit", "resource": "~/.config/opencode-connect/*", "effect": "deny" },
    { "action": "read", "resource": "~/.local/state/opencode-connect/*", "effect": "deny" },
    { "action": "edit", "resource": "~/.local/state/opencode-connect/*", "effect": "deny" }
  ]
}
```

Equivalent [Claude Code rules](https://code.claude.com/docs/en/permissions) in its
settings file use `Read` and `Edit` path rules; current Claude Code does not use
`Write(path)` rules for filesystem checks:

```json
{
  "permissions": {
    "deny": [
      "Bash(*courier.py*)",
      "Read(~/.config/opencode-connect/**)",
      "Edit(~/.config/opencode-connect/**)",
      "Read(~/.local/state/opencode-connect/**)",
      "Edit(~/.local/state/opencode-connect/**)"
    ]
  }
}
```

Substitute **all actual configured** XDG/queue paths, including databases, WAL
files, configuration snapshots, locks, token-card copies, the systemd unit, and
any alternate CLI entry points. Verify behavior with your installed harness
version. Shell command matching is not filesystem enforcement: a Python import or
renamed copy can bypass a script-name pattern. Never grant an agent access to
pairing links, invitation output, or card tokens. A broad unrestricted shell
allowance defeats these rules; use OS/user separation if isolation from the local
agent is required.

## Protocol and reliability

- `POST /v1/messages` accepts exactly `{text, idempotency_key, reply_to?}`. The
  recipient derives the sender from its issued token, never from submitted text.
  It returns an opaque message ID and `held`, `approved` for a direct contact,
  or the existing lifecycle state for an exact retry. Deduplication and held queue
  insertion share one transaction. Reusing a key for different content returns a
  conflict, not a second message.
- `GET /v1/messages/ID` reports only messages owned by the authenticated contact.
  `GET /v1/whoami` reports the configured person and protocol version, not sessions.
- `POST /v1/pair` and `POST /v1/pair/ack` carry pairing, described above.
- Authentication occurs before body reads, except on `POST /v1/pair`, which has
  its own limits. Socket timeout is three seconds;
  bodies and responses are bounded at 64 KiB; message text at 12,000 characters.
  Transfer encoding and duplicate content lengths are rejected. Each authenticated
  contact is limited to 60 requests/minute (including malformed requests after
  authentication) and 100 held messages.
  The serial listener limits resource use but is not Internet-grade DoS protection.
- Outbound workers claim only exact `person:` targets. An accepted POST records
  `delivered` with `remote_id` and remote state (usually `held`); it does not mean
  approved or delivered to a session. Remote approval/rejection/expiration is
  polled separately, at most once per minute for held receipts.
- Connection failures, lost responses, rate limits, and server failures release
  the local person message for retry with the **same idempotency key**, using
  persisted exponential backoff (2–300 seconds). No automatic transport-level
  retries or redirects occur. Other HTTP 4xx errors fail without retry.
  Backoff does not block ready jobs behind a deferred message. On worker restart,
  only courier-owned, idempotent HTTP claims are released using their original
  keys; unrelated local session deliveries and ambiguous bridge jobs are untouched.
  This idempotent person protocol does not change the existing bridge's rule
  against automatically retrying ambiguous session API deliveries.
- `reply_to`, if supplied by the sending adapter, identifies the original remote
  receipt ID. Suggestions are scoped to that contact and map back to the recorded
  source session. They approve a reply only for a `direct` contact; otherwise
  they only propose its destination. The local MCP accepts `reply_to` only for a
  message from that person already delivered to the caller; plain text is not
  parsed for routing instructions.

## Queue integration contract

Courier accepts a queue with a shared `sqlite3.Connection` as `db`, and a
`transaction()` context that starts `BEGIN IMMEDIATE`, checks the local queue
contract, and atomically commits/rolls back **both queue and courier tables**.
Its methods must not independently commit while in that transaction:

- `claim("person:")`: one pending outbound dictionary with `id`, `source`,
  `target`, and `payload` containing `text` and optional `reply_to`; never claim
  local session targets.
- `mark(id, state, result)` finalizes only sending messages. `release(id)` is only
  for sending person targets and returns them to pending.
- `insert_inbound(source, text, remote_key=..., reply_to=None)`: held receipt with
  newly allocated `id`, `state`, and `duplicate`. Only this API accepts a `person:`
  source. Courier metadata must exist for a duplicate; missing metadata requires
  operator review, not an automatic replay into a session.
- `get(id)`: dictionary in the same shape as a claimed row.
- `approve_inbound(id, target)`: validate the authorized local target and change
  **only held inbound** to pending for local delivery. Never deliver inline.
- `reject_inbound(id, reason=...)`: reject held rows. Expiration uses this same
  queue state with a retention reason, while the courier's receipt reports
  `expired` distinctly. There is no `expired` state in the local queue.
- `cancel_person("person:NAME", outbound=True)`: cancel held and approved/pending inbound and
  pending outbound for exactly that contact, without touching other contacts,
  sending/uncertain jobs, or already-delivered messages.

The operational factory uses `LocalMailbox(state_root())`, with the inherited node
and bridge state environment. All courier writes occur inside its non-nesting
transaction; queue methods join that transaction. Courier never writes queue
tables directly. All its own tables use the `courier_` prefix. A fake queue is used
only in tests, never as an operational fallback. Real-queue tests cover the MCP
send path, local relay delivery with mocked API effects, correlated replies,
independent CLI/worker connections, crash recovery, queue binding, and revocation
racing an upload.

## Live acceptance (not yet performed for the simplified flow)

Proceed only with separate authorization: first Linux and WSL in mirrored mode on
the private LAN, starting from no configuration; then over the selected VPN; then
with another consenting person. On each, run `setup` on both machines, `pair`,
send one message to a `direct` contact and one to an `ask` contact, check reply,
offline retry, and service restart, and end with `unpair` on both sides. Count the
steps that were actually needed. Do not publish operational names, network
addresses, pairing links, invitation cards, or acceptance receipts.
