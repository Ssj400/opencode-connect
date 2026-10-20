# OpenCode Connect

Explicit messaging and file transfers between registered OpenCode V2 sessions on two machines.
**Linux → Windows SSH → WSL**; the Linux relay also collects messages from WSL
and delivers them to Linux. No inbound SSH server is required on Linux. This is
not a universal remote service or an automatic agent conversation loop.

The product vision is for **two people to work with their own assistants**, letting
those assistants consult each other about shared work without making people relay
every question manually. Each person retains control of their environment. This
prototype provides messaging; bilateral consent, information-sharing policies,
and correlated questions and answers remain future work.

Repository: **https://github.com/Ssj400/opencode-connect**.
An experimental prototype for review, not a stable release or a secure multi-user
service. Released under the [MIT License](LICENSE). A private security reporting
channel remains to be defined. See the [release checklist](docs/release-checklist.md).

## Requirements and limitations

- Python 3.10+ on Linux and WSL; standard library only, no `pip install` required.
- OpenCode V2 installed and authenticated locally on both sides, accessed through `opencode api`.
- An OpenSSH client on Linux, a user-configured Windows OpenSSH Server, and
  WSL with `python3` accessible through `wsl.exe`.
- Trusted machines and system accounts, with installation paths not writable by others.
- Do not expose the OpenCode API to a LAN or the internet, disable firewalls, or weaken host verification.
- The local API is not an additional paid model API. However, delivered messages
  can trigger model responses and consume your existing provider's quota or budget.
- Local unit tests are available; **live integration of this public edition has not been verified**.
  Results from a separate installation do not certify this export.

## Setup

Use an independent checkout on Linux. Keep the scripts, documentation, and skill
together. Do not copy another person's configuration or runtime state.
Run the following commands from the checkout unless stated otherwise.

```bash
git clone https://github.com/Ssj400/opencode-connect.git
cd opencode-connect
```

1. Run the tests and audit before creating operational configuration:

   ```bash
   python3 -B -m unittest discover -v
   python3 -B audit_public.py
   ```

   These checks run locally without SSH, API calls, or models. Temporary files use
   `tempfile`; you can set `TMPDIR`. The audit expects an exportable source tree,
   not an installation containing `routes.json` or runtime state. It deletes nothing.
   After configuration, run the audit against a clean export instead.

2. Create private configuration without overwriting an existing file:

   ```bash
   umask 077
   test ! -e routes.json && cp routes.example.json routes.json
   ```

   Edit `routes.json`: set `side` to `linux`, then provide the Windows host, SSH
   username (spaces and Unicode are supported), WSL username, absolute path to the
   OpenCode executable in WSL, and a remote installation directory you control.
   Find the executable in WSL with `command -v opencode`; do not assume its location.
   `192.0.2.10` is an RFC5737 documentation address, not a working host. Example
   directories are placeholders; they are only created by an explicit deployment.

   Register only user-selected sessions in `peers`, using their exact `side` and
   `sessionID`. Do not read conversations to discover sessions or register every
   session automatically. At least one session per machine is required; aliases
   do not have to be named `linux` and `wsl`. The `ses_REPLACE_*` placeholders are
   intentionally rejected until replaced. Unknown fields and relative paths are
   rejected. Scripts read only the adjacent `routes.json`, not parent directories.

3. Verify the host's identity independently, then authenticate from your terminal.
   Never give the assistant your password or PIN, or save them in project files:

   ```bash
   python3 bridge.py connect
   python3 bridge.py check
   ```

   `connect` opens an SSH ControlMaster that persists for 10 minutes while idle.
   Its socket lives in a private 0700 subdirectory of `XDG_RUNTIME_DIR` or the user's
   cache (`XDG_CACHE_HOME`, defaulting to `~/.cache`). Insecure directories and
   excessively long socket paths are rejected. Use a shorter private runtime path
   rather than weakening permissions. `check` makes small GET requests without
   generating model responses; the OpenCode CLI may discover or start its local service.

4. Deploy files to the configured WSL directory **only with your authorization**:

   ```bash
   python3 deploy.py --install-config
   ```

   This copies an explicit allowlist of project files. The flag also copies
   `routes.json`, changing `side` to `wsl`. It does not copy queues, runtime state,
   credentials, or MCP configuration. Different remote files are only replaced if
   they match the previous managed deployment's hash. Review conflicts instead of
   deleting the deployment manifest. For subsequent code and documentation updates:

   ```bash
   python3 deploy.py
   ```

   Alternatively, install a clean export manually and create WSL configuration with
   `side: wsl` and matching aliases, session IDs, and SSH settings. Keep both sides
   consistent. Each machine retains its own `.state/`, `outgoing/`, and `inbox/`.

5. Explicitly configure MCP in each OpenCode project that will use the bridge.
   From that project's directory, replace the absolute path in this example:

   ```bash
   opencode mcp add laptop_bridge -- python3 /srv/opencode-connect/mcp_server.py
   opencode mcp list
   ```

   `/srv/opencode-connect` is a placeholder: use the actual local path on each machine.
   Do not run the example unchanged. See the [JSONC example](examples/opencode.jsonc.example)
   and merge only the required entry, preserving other configuration. The server
   uses the classic `legacy` MCP protocol. Coordinate any replacement of an existing
   MCP server rather than overwriting it accidentally. Use `/mcps` to reconnect and
   verify `connected` before reporting success.

6. Review aliases and queues before starting the relay:

   ```bash
   python3 collab.py peers
   python3 collab.py status
   python3 collab.py relay
   ```

   Keep the relay running in the foreground on Linux. Ctrl+C stops the relay,
   **not the agents**. There is no daemon or automatic startup. `relay --once`
   also delivers pending messages: it is not a harmless health check. WSL does not run a relay.

## Authorized use

MCP provides `peers`, `status`, `send`, and `send_file`. The calling session must be
registered; OpenCode supplies its ID through request metadata. It is not an
argument for the model to invent. See the [skill](skills/opencode-connect/SKILL.md)
for the operating workflow.

Equivalent CLI commands, using example aliases:

```bash
python3 collab.py send --source linux --to wsl --text 'Explicitly authorized task'
python3 collab.py send --source linux --to wsl --text 'Authorized urgent blocker' --urgent
python3 collab.py status
```

On WSL, use `--source wsl --to linux`. Normal delivery uses `queue`; `--urgent`
uses `steer` for explicitly authorized urgent notices. It does not guarantee
immediate interruption of blocked tools. Do not automatically forward responses
or reply to acknowledgments. Existing permissions and Plan mode remain in force.
`cancel-pending ID` cancels only a local message that has not been sent.

For files, stage only an approved file in `outgoing/` (create the directory with
`mkdir -m 700 outgoing` if needed). Use a simple filename, no symbolic links, and
a maximum size of 1 MiB. Do not copy secrets. Send it with:

```bash
python3 collab.py send --source linux --to wsl --text 'Approved file' --file example.txt
```

Files arrive at `inbox/<message-id>/<filename>` with SHA256 verification.
They are neither overwritten nor executed. `example.txt` must be the approved
file, not an automatically selected one.

Optional administration, only with explicit authorization:

```bash
python3 collab.py create-session --side wsl --title 'New collaboration'
```

This creates a session without generating a response, with `ask` permissions.
It neither registers an alias automatically nor changes existing sessions'
permissions. Session creation is not exposed through MCP.

## Delivery states and troubleshooting

`pending → sending → delivered | uncertain`. `delivered` means API acceptance,
not task completion. If a process dies in `sending`, that message is not claimed
again after restart. `uncertain` messages are not retried either. Review them
manually with the user, preserving IDs and state. Never delete SQLite to clear a queue.

- MCP disconnected: check Python/script paths, project configuration, and `/mcps`.
- Unregistered session: confirm the current ID and update both configuration files.
- Pending delivery: check SSH and the relay; consider which tasks starting it would deliver.
- Expired SSH connection: authenticate in your terminal using `connect`; do not send passwords.
- A route changed after enqueue: review it before attempting any new delivery.
- Partial deployment failure: review hashes and remote changes, preserving others' work.

## Local skill and repository

The skill is not installed automatically. To enable it from a complete checkout,
explicitly add the absolute path to its `skills` directory to the project's
`skills` configuration array, preserving existing entries. For example:
`"skills": ["/srv/opencode-connect/skills"]` (replace the placeholder path).
The skill uses relative links into the checkout; do not copy it without its docs.
Official repository: https://github.com/Ssj400/opencode-connect.
A future HTTP skill source requires an `index.json` catalog, not just a GitHub URL.

## Maintenance

[Security](SECURITY.md) · [Architecture decisions](docs/decisions.md) ·
[Release and integration checklist](docs/release-checklist.md).
OpenCode V2 references: `https://opencode.ai/v2/docs/mcp-servers`,
`https://opencode.ai/v2/docs/api`, and `https://opencode.ai/v2/docs/skills`.
Local tests do not need access to these sites. Documentation, runtime messages,
and contributor instructions are maintained in English; exchanged user messages
can use any language.

## Platform roadmap

The next platform priorities are **Linux ↔ Linux** and **Windows ↔ Windows**,
followed by macOS. Native Windows and WSL are distinct targets. These adapters
are not implemented yet; the current transport remains Linux → Windows SSH → WSL.

## Architecture

The delivery core is independent of harness APIs and operating systems. An agent
adapter translates messages to OpenCode, while a command transport handles local
execution or SSH into WSL. Existing CLI commands, configuration, mailboxes, and MCP
tools are preserved. See [core and adapter boundaries](docs/architecture.md).

This separation prepares future platform and harness adapters; it does not yet
add Linux-to-Linux, native Windows, macOS, or Claude Code support.

## License

[MIT](LICENSE) — Copyright (c) 2026 Ssj400.
