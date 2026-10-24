# Architecture decisions

1. **Personal/public separation.** Export through the `deploy.FILES` allowlist.
   Do not inherit private history, configuration, or runtime state. Use flat scripts
   with local imports; an export must work from an independent directory.
2. **Current platform scope.** Linux coordinator, Windows SSH, and OpenCode in WSL.
   Python 3.12+ and the standard library. Do not claim universal support, native
   Windows, macOS, direct Linux SSH, multiple transports, or arbitrary topologies.
3. **No implicit changes.** Require adjacent configuration with absolute installation
   paths and no personal defaults. Shortcuts are optional for chat; reject unresolved
   placeholders when shortcuts are configured.
4. **MCP compatibility.** Retain the recommended `laptop_bridge` alias and classic
   `legacy` protocol. Do not install or change OpenCode configuration automatically.
5. **Directed messaging.** No automatic conversation command or agent cancellation
   endpoints. Explicit queue/steer delivery; session creation through the administrative CLI only.
6. **Persistence and errors.** Dedicated SQLite database, stable IDs, a single-relay
   lock, and retained ambiguous states. Do not promise exactly-once delivery or retry POSTs.
7. **Deployment.** Copy only allowlisted files. Exclude operational configuration by
   default; `--install-config` explicitly authorizes copying it to WSL with an adjusted
   `side`, not publishing it. Preserve remote changes and manifests; do not migrate state.
8. **Testing.** Deterministic unit tests with mocked endpoints and portable temporary
   directories. Exclude the old integration runner because of cost and incomplete
   isolation. Do not transfer another installation's integration guarantees to this edition.
9. **Skill.** English instructions and metadata, with no automatic installation.
   Checkout-relative references; repository: https://github.com/Ssj400/opencode-connect.
   An HTTP skill catalog is not implemented. A repository URL is not automatically
   an OpenCode V2 HTTP skill catalog.
10. **Authorized experimental publication.** Publish `Ssj400/opencode-connect` only
    at the authenticated owner's explicit request. Use the owner's Git identity
    without agent coauthors. The owner confirmed ownership and approved MIT with
    copyright in their name. A private security reporting channel remains pending.
11. **English project language.** Keep documentation, instructions, metadata, comments,
    help, errors, logs, examples, and commit messages in English. Retain Unicode test
    fixtures and support user messages in any language. See `AGENTS.md`.

## Not implemented in the current edition

A GUI, frameworks, automatic credential-based reconnection or session selection,
agent debates, automatic reconciliation, and additional platform adapters. The
only daemon is the courier's optional service; see the last section.

## Opt-in local messaging

`local_bridge.py` and `local_mcp_server.py` provide normal text queues for sessions
sharing the same local OpenCode service. They do not load cross-machine routes or
SSH settings and keep runtime data outside the checkout. Session search belongs
to the OpenCode adapter, returns metadata-only pages, and never resolves a title
directly into a send. The caller selects an explicit session ID after ambiguity
is resolved. Queue schema and claim/receipt behavior reuse the existing mailbox;
the existing cross-machine installation and state are not migrated. Local mode
does not support files, steer, self delivery or automatic replies. See
[local messaging](local-messaging.md) for setup and remaining live validation.

Local transport does not define node identity. `OPENCODE_CONNECT_LOCAL_NODE`
explicitly selects a canonical session namespace; `local` is the isolated default.
The local queue binds mode, node and schema version transactionally before any
insertion or claim. Existing populated unbound queues are not automatically
adopted. This is a configuration contract, not a claim that CLI discovery exposes
a stable server identity. Cross-machine routing and core contracts are unchanged.

Use `OPENCODE_CONNECT_LOCAL_STATE_HOME` to isolate a bridge queue without changing
the inherited CLI discovery context. Overriding the general `XDG_STATE_HOME` can
also change OpenCode service discovery and storage; it is not a bridge-only
isolation mechanism. The existing XDG location remains the fallback when no
bridge-specific override is supplied.


## Next platform priorities

Implement and test **Linux ↔ Linux** and **Windows ↔ Windows** first. Consider
macOS afterward. Native Windows and WSL are distinct environments; WSL support
does not establish native Windows support. Keep platform adapters separate from
messaging and authorization so that future harness adapters can reuse the core.
These are agreed priorities, not capabilities already supported by this release.

## Relocating an installation

Pause sends and close MCP/relay processes without cancelling sessions. Back up and
move the entire tree, including state and the deployment manifest; do not use deploy
as a state migration tool. Update the local MCP path and reconnect its processes.
Only change `ssh.remote_directory` when WSL was also moved; keep both machines
consistent. Preserve IDs and states, verify files before resuming, and do not use
`relay --once` as a health check: it delivers pending messages. Never overwrite new
runtime state during a rollback.

## Product direction: assistants belonging to different people

The goal is not just controlling one's own machines. Each person works with their
own OpenCode and permissions; their assistants can consult each other about shared
work to reduce manually relayed messages.

The next evolution must distinguish **consulting** from **assigning or modifying**:
asking about an API contract does not authorize changing it or reassigning the
respondent's work. Connectivity and session registration do not replace bilateral
consent or an information-sharing policy.

Future capabilities, NOT implemented yet:
- Collaborator identity, shared projects/topics, and revocation.
- Agreed minimal availability or task summaries, not conversation exports.
- Correlated questions and answers with references and verifiable provenance.
- Answers within previously approved scope, without approving every trivial message;
  escalate sensitive information and scope decisions to people.
- Default queueing and policy-governed urgent notices without indiscriminate interruptions.
- Message/cost limits, expiration, and a distinction between current and stale information.

An existing VPN provides connectivity, not project identity or permission to query
every machine. Never include a company's private configuration or data in the public edition.

## Agreed interaction modes

The following product direction is agreed but not implemented:

| Mode | Allowed within an authorized agreement |
| --- | --- |
| Consultation | Ask and answer questions about shareable information. |
| Collaboration | Coordinate tasks, share progress, and review; each assistant controls its environment. |
| Delegation | Assign a bounded task with permitted actions and an expected result. |
| Control | Direct remote actions with the owner's explicit authorization. |

A mode does not replace granular permissions. Control may permit running tests while
prohibiting secret edits, installations, or pushes. Messages, network trust, and SSH
access never promote a mode automatically. Existing OpenCode permissions remain a
boundary that an agreement cannot bypass.

Each agreement identifies participants, project/scope, actions, duration, and revocation.
Collaboration is the proposed starting point for coworkers, but requires acceptance;
Control over one's own machines must also be enabled explicitly.

Future implementations must check authorization both at enqueue and execution time,
rejecting expired or revoked tasks. Revocation blocks new actions when received;
it does not undo completed effects or cancel running work without specific
authorization. This prototype does NOT implement these agreements yet.

## Implemented core/adapter separation

The first abstraction step extracts a pure delivery core (`core.py`), an OpenCode
CLI/session adapter (`opencode_adapter.py`), and command transports (`transports.py`).
Application wiring remains in `collab.py`; `bridge.Endpoint` preserves its facade
for CLI and deployment callers. Existing routes, tools, and SQLite schema remain
unchanged. No operational personal state is migrated by this change.

The core accepts opaque peer/session identifiers through injected contracts and
never reads configuration, runs commands, or calls a harness API. Adapters declare
delivery capabilities; unsupported modes fail rather than silently becoming queue.
Receipt persistence failure propagates without a resend, preserving manual recovery.

Tests cover the pure core with fictitious nodes/harnesses, OpenCode API mappings,
command transport shapes, and the existing application behavior. The public edition
still needs explicitly authorized live integration; interface extraction alone does
not verify a new platform or harness. See [architecture](architecture.md).

## Any-session chat on paired nodes

Text chat no longer requires session registration. The source remains tied to
mandatory MCP metadata. A destination may be a configured shortcut or an explicit
`linux:ses_ID` / `wsl:ses_ID` address, including another session on the same node.
`whoami` returns the caller's reply address; received messages include that address.
This does not discover arbitrary hosts or expose conversation histories.

Canonical addresses are pinned in queued payloads and revalidated against current
routes and mailbox origin at delivery. Older SQLite records keep their original
cross-node route checks; no schema migration or automatic retry is introduced.
Both endpoints must still be explicitly registered for files, checked at enqueue
and delivery. Chat payloads cannot include hidden file operations.

The trust boundary remains the owner's paired accounts and existing harness
permissions. Chat permission does not authorize executing received instructions,
controlling environments, or sharing files. It does not create a harness-enforced
text-only sandbox or implement cross-person consent agreements.

## Package layout with launchers

Every module was a script in the checkout root, and `courier.py` had passed
1,100 lines. The code is now the `opencode_connect` package, with one subpackage
per area: `bridge`, `local`, `courier`, and `claude`. The commands are thin
launchers in `bin/` that keep their earlier names and arguments, so nothing needs
`pip install` or `PYTHONPATH`. Tests mirror the package under `tests/`, and the
export audit moved to `tools/`. No file may exceed 600 lines. The courier is split
by responsibility: settings, application logic, HTTP server, worker, systemd
service, doctor, and command line.

Configurations that name a script by path must add `bin/`: MCP servers, Claude
Code hooks, and the courier's systemd unit. The relay recognizes `bin/collab.py`
as before. A WSL installation needs a new deployment, which adds the new paths and
leaves the old root files unused. Runtime state and stored message formats do not
change. Earlier sections name modules by their flat file names: `core.py` is now
`opencode_connect/core.py`, and `local_bridge.py` is now `opencode_connect/local/mailbox.py`.

## Connecting two people in a few steps

The first live test of the courier connected two machines over a LAN, revocation
included, but took about twenty manual steps: variables and wrappers, a port proxy
and firewall rules, two cards and four confirmation codes, and two foreground
processes that had to stay open. The owner judged that not worth it. The simplified
flow keeps every consent boundary and removes the steps that were not decisions:

- **`setup`** checks the machine before asking anything, offers only VPN and LAN
  IPv4 addresses, and writes the configuration after one code. Firewall and MCP
  commands are printed for the owner to review and run, never run: changing a
  firewall or another tool's configuration stays the owner's decision.
- **`pair`** replaces copying cards. A 60-bit code, shown once and never sent, proves
  to each side that the other knows it; the first courier to use it becomes the
  contact, and trust stays provisional until the joiner confirms.
- **An optional systemd user service** runs the courier and this machine's local
  relay from login. It is the project's only daemon, installed only after a code,
  from `setup` or `service install`; the cross-machine relay still has none.
- **Routes per contact**: messages are held for approval by default. `direct`
  delivery is a standing approval for one session, set with a code, revocable,
  and capped at 20 messages an hour, because each delivery can start an agent.
  `approve` without an ID approves, with one code, exactly the messages it showed.
- **`status`** describes the courier for people and only reads; `doctor` remains the
  diagnosis. **`unpair`** revokes both directions on one side.
- **WSL is supported in mirrored mode only**: NAT addresses are not offered, and
  `setup` explains the switch. Native Windows and macOS are separate milestones.

## Announcing an address change

A courier's address is an IP, and it changes: switching VPNs, a new LAN lease, or
re-registering the device. Contacts then kept the old URL until both sides paired again.
Now the courier says when its IP is no longer on the machine, the person changes it
with the menu or `address`, and the courier tells each contact's courier the new URL.

- **The person decides the address.** Rebinding automatically was rejected: it would
  choose, without the person, which network the courier listens on.
- **An announcement is authenticated and tied to its source.** It needs the token the
  contact issued, and its URL must be the IP it comes from, as with pairing cards. A
  stolen token could already send messages as the contact; now it can also redirect
  that contact's messages to the thief's own IP, which is why tokens stay private and
  the network trusted.
- **Older couriers keep working.** One that does not know the route answers 404; the
  announcement is marked so, and `status` asks for pairing again with `--replace`.
