# Federated person-to-person messaging (B1–B4 preview)

This opt-in courier exchanges **text only** between independently owned machines.
There is no central mailbox, shared SSH shell, remote execution endpoint, or
required VPN vendor. Each recipient holds inbound messages on their own machine
until an operator explicitly approves one for a chosen local session. Receipt by
a courier is not receipt by an assistant, and approval is not task completion.

## Implementation status

B1–B4 are implemented against an injected SQLite `FakeQueue` and tested with two
short-lived couriers on loopback. **The operational queue adapter and MCP person
targets are not integrated yet (B5).** `courier.py run` fails closed before binding
a socket without that adapter. Doctor (B6), real LAN/VPN acceptance (B7), and
autostart are not included. No personal installation is changed by this preview.

Run offline tests with Python 3.12 or newer:

```sh
python3 -B -m unittest test_courier -v
python3 -B -m unittest discover -v
python3 -B audit_public.py
```

## Trust and network boundaries

- Run only on an explicitly authorized private LAN or VPN interface. Bind the
  interface's specific IP, not all interfaces. Wildcard binds are rejected unless
  `--allow-wildcard` is explicitly supplied; that flag is not firewall permission.
- Before a live test, the operator must authorize the listener and configure both
  host firewalls for the selected interface/port and the other participant's IP.
  Do not expose this listener on the public Internet or automatically change a
  firewall, SSH setting, route, or VPN configuration.
- Plain HTTP depends on the trusted network path. On an untrusted LAN it exposes
  bearer credentials and message content; use HTTPS with an independently managed
  authenticated TLS endpoint or a trusted encrypted VPN. The courier does not
  implement TLS certificates, peer discovery, NAT traversal, or VPN setup.
- A contact's inbound token authorizes holding messages, reading statuses for
  **that contact's messages only**, and querying the courier identity. It never
  authorizes local delivery, conversation reads, file transfers, or shell access.
- Consent is bilateral: receiving someone's card does not grant them access to
  your machine. Each participant issues and independently exchanges a card.
- `invite` displays a fresh token once. Only its SHA-256 digest is retained for
  inbound authentication. A received card necessarily contains a bearer token;
  store it privately and never publish it, log it, or paste it in an agent chat.
- Local processes with access to the courier state or an unrestricted shell are
  within the operator's trust boundary. TTY plus a fresh confirmation code guards
  against accidental/noninteractive approval, **not a hostile local process**.
  Permissions and deny rules are operational guidance, not an enforced sandbox.

## Private configuration and contacts

The default configuration is
`$XDG_CONFIG_HOME/opencode-connect/courier.json`, falling back to
`~/.config/opencode-connect/courier.json`. The file must be owned by the operator,
regular (not a symlink), and mode **0600**, in a private directory. It is separate
from `routes.json` and does not alter local or cross-machine bridge configuration.

The following are **setup examples for after B5**, not instructions to activate a
preview listener. Substitute your authorized VPN interface IP and reachable URL:

```sh
python3 -B courier.py init --person alice --listen 192.0.2.10:8765 --url http://192.0.2.10:8765
python3 -B courier.py invite bob
python3 -B courier.py trust bob --card -
python3 -B courier.py contacts
```

`--card -` pastes the independently obtained JSON card through a hidden terminal
prompt. Avoid an inline `--card '{...}'`: command arguments can be visible to other
local processes and shell history. Cards have exactly `person`, `url`, and `token`.
Use a separate out-of-band channel to exchange them; verify the intended person
and endpoint independently. Displaying a card does not transmit it.

Configuration contains `person`, `listen`, `url`, `tokens` (name to token digest),
and `contacts` (name to received card). Names are lowercase ASCII identifiers.
The shared SQLite database is authoritative for token revocations/contact changes;
`courier.json` is an atomically replaced private snapshot. After a crash between
the database commit and snapshot replacement, startup repairs the snapshot from
SQLite, never restoring an old revoked token from JSON. Do not edit contact maps
by hand. A mismatch in identity/listener/URL requires explicit operator review.

`untrust bob` removes the received card, revokes the issued token, and cancels
held/approved-but-unsent inbound and pending outbound messages from/to that person
in the **same SQLite transaction**. It cannot retract delivered messages or an
already in-flight HTTP request. Reissuing an invitation rotates the inbound token;
the previous token stops authenticating. Contact lists never display tokens.

## Inbox and approval

```sh
python3 -B courier.py inbox
python3 -B courier.py inbox --watch
python3 -B courier.py approve MESSAGE_ID --to wsl:ses_EXAMPLE
python3 -B courier.py reject MESSAGE_ID
python3 -B courier.py untrust bob
```

`init`, `invite`, `trust`, `untrust`, `approve`, and `reject` require interactive
stdin **and** stdout and retyping a newly displayed five-letter code. There is no
`--yes` bypass. Approval always requires an explicit `--to` session address, even
if the inbox suggests one. Supported shapes are `NODE:ses_ID` and `claude:UUID`;
person targets cannot be used for approval or automatic forwarding. The real
adapter must additionally validate the local destination before queueing delivery.

Inbox output removes escape sequences, control characters, and invisible Unicode
formatting controls from remote text. Original text is retained for the explicitly
approved recipient, not executed. Treat all remote content as untrusted data.
Held messages expire after seven days, including while the listener is idle;
expiration is checked before approval and status/inbox reads. Expiration changes
eligibility, not data erasure: deduplication/status tombstones persist. No desktop
notification is enabled in this preview, and `--watch` never approves a message.

### Agent deny rules

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
files, configuration snapshots, locks, token-card copies, and any alternate CLI
entry points. Verify behavior with your installed harness version. Shell command
matching is not filesystem enforcement: a Python import or renamed copy can
bypass a script-name pattern. Never grant an agent access to invitation output or
card tokens. A broad unrestricted shell allowance defeats these rules; use
OS/user separation if isolation from the local agent is required.

## Protocol and reliability

- `POST /v1/messages` accepts exactly `{text, idempotency_key, reply_to?}`. The
  recipient derives the sender from its issued token, never from submitted text.
  It returns an opaque message ID and `held` (or the existing lifecycle state for
  an exact retry). Deduplication and held queue insertion share one transaction.
  Reusing a key for different content returns a conflict, not a second message.
- `GET /v1/messages/ID` reports only messages owned by the authenticated contact.
  `GET /v1/whoami` reports the configured person and protocol version, not sessions.
- Authentication occurs before body reads. Socket timeout is three seconds;
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
  source session. They never approve or route a reply automatically. B5 must wire
  this metadata explicitly; plain text is not parsed for routing instructions.

## Queue integration contract (B5 dependency)

Courier accepts a queue with a shared `sqlite3.Connection` as `db`, and a
`transaction()` context that starts `BEGIN IMMEDIATE`, checks the local queue
contract, and atomically commits/rolls back **both queue and courier tables**.
Its methods must not independently commit while in that transaction:

- `claim("person:")`: one pending outbound dictionary with `id`, `source`,
  `target`, `text`, and optional `reply_to`; never claim local session targets.
- `mark(id, state, result)` and `release(id)`; release is only for person targets.
- `insert_inbound(source, target=None, text, remote_key)`: held dictionary with
  the newly allocated `id`. Only this API accepts a `person:` source.
- `get(id)`: dictionary used to render held text.
- `approve_inbound(id, target)`: validate the authorized local target and change
  **only held inbound** to pending for local delivery. Never deliver inline.
- `cancel_person("person:NAME")`: cancel held and approved/pending inbound and
  pending outbound for exactly that contact, without touching other contacts,
  sending/uncertain jobs, or already-delivered messages.

The last three methods and `db` are explicit proposed adapter requirements beyond
the initial C0 sketch. They must be reconciled with C0 before B5; courier does not
reach into the local queue's tables or assume a private message schema. All its
own tables use the `courier_` prefix. No fake queue is shipped as an operational
fallback. After C0.8/C0.9 approval, the real adapter must be tested for concurrent
CLI/worker access, crash recovery, queue binding, and revoked-contact races before
any live acceptance test.

## WSL and live acceptance (not performed)

WSL NAT may not accept inbound connections from the other machine. The operator
can choose mirrored networking in `.wslconfig` or an explicitly scoped Windows
port proxy. Both Windows and Linux firewalls must restrict the listener to the
other authorized participant/interface. Do not set either up automatically.

Proceed only with separate authorization: first on the private LAN, then on the
selected VPN once both machines join it, then with another consenting person.
Check remote authentication, hold, explicit terminal approval, session reception,
reply, revocation, offline retry, and shutdown independently. Do not publish
operational names, network addresses, invitation cards, or acceptance receipts.
