#!/usr/bin/env python3
"""Linux -> Windows SSH -> WSL transport: connect, check, and a read-only doctor.

No automatic conversations.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from config import BridgeError, load_config, validate_ssh
from opencode_adapter import OpenCodeCli, check, probe_api
from transports import LocalCommandTransport, WindowsWslSshTransport, tcp_probe, without_ssh_advisories

ROOT = Path(__file__).resolve().parent
OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
STATE_ORDER = ("pending", "sending", "uncertain", "failed", "cancelled", "delivered")
CONNECT_HINT = "Run `python3 bridge.py connect` in your own terminal; never give the password to an assistant."
# Specific to wsl.exe failures; a path such as /opt/wsl-tools/opencode must not match.
WSL_MARKERS = ("wsl.exe", "wsl/", "wsl_e_", "wsl (", "no distribution", "getpwnam")
NETWORK_HINTS = {
    "unreachable": "The Windows machine is not on this network: it may be asleep, off Wi-Fi, or on "
                   "another network. Wake it, keep it plugged in, and check its address.",
    "timeout": "Nothing answered: the machine may be asleep, a firewall may drop the traffic, or it "
               "is on another network or VPN.",
    "refused": "The machine is up but nothing listens on that port: check the Windows OpenSSH Server service.",
    "unresolved": "The host name does not resolve: check `host` in routes.json.",
    "error": "Check the address in routes.json and the network on both machines.",
}


class Endpoint:
    """Compatibility facade composing one CLI client with a selected transport."""

    def __init__(self, args, remote=False):
        options = {key: getattr(args, key, None) for key in (
            "host", "ssh_user", "wsl_user", "remote_opencode", "remote_directory")}
        validate_ssh(options)
        self.args = args
        self.remote = remote
        self.transport = WindowsWslSshTransport(options) if remote else LocalCommandTransport()
        self.api = OpenCodeCli(self.transport, args.remote_opencode if remote else args.local_opencode)

    def ssh(self, batch=True):
        if not self.remote:
            raise BridgeError("The local transport does not use SSH.")
        return self.transport.ssh(batch)

    def connect(self):
        if not self.remote:
            raise BridgeError("The local transport does not need an SSH connection.")
        return self.transport.connect()

    def call(self, method, path, body=None, timeout=30):
        return self.api.call(method, path, body, timeout)

    def remote_exec(self, argv, timeout=30, cwd=None, data=None):
        if not self.remote:
            raise BridgeError("remote_exec requires a remote transport.")
        return self.transport.run(argv, timeout=timeout, cwd=cwd, data=data)


def ssh_problem(error):
    """Drop OpenSSH advisories (such as post-quantum warnings) so the actual failure shows."""
    return without_ssh_advisories(error)[:300] or "unknown error"


def failure_kind(problem):
    if "Permission denied" in problem:
        return "auth"
    if "Host key verification failed" in problem or "IDENTIFICATION HAS CHANGED" in problem:
        return "hostkey"
    if any(text in problem for text in ("Connection timed out", "No route to host", "Connection refused",
                                        "Could not resolve", "Connection closed", "Connection reset",
                                        "Network is unreachable")):
        return "network"
    if "timed out" in problem:
        return "timeout"
    return "remote"


def listing(names, limit=5):
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def describe(counts):
    if not counts:
        return "empty"
    order = sorted(counts, key=lambda state: STATE_ORDER.index(state) if state in STATE_ORDER else len(STATE_ORDER))
    return ", ".join(f"{counts[state]} {state}" for state in order)


def diagnose(root=None):
    """Layered, read-only diagnosis of this installation and the paired machine.

    Sends no messages, claims nothing, starts no relay, and deploys or reconnects nothing.
    Like `check`, the OpenCode CLI may discover or start its local service.
    Returns (status, area, summary, hint) tuples in checking order.
    """
    import collab  # Loaded on demand: both modules import this one.
    import deploy

    root = Path(root) if root is not None else ROOT
    findings = []

    def report(status, area, summary, hint=None):
        findings.append((status, area, summary, hint))

    try:
        cfg = load_config(root)
        if cfg["side"] != "linux":
            raise BridgeError("doctor runs on the Linux coordinator (side=linux).")
    except BridgeError as exc:
        report(FAIL, "Configuration", str(exc), "Create or fix routes.json as described in the README setup.")
        return findings
    report(OK, "Configuration", f"routes.json is valid; {len(cfg['peers'])} optional shortcut(s)")
    options = SimpleNamespace(**cfg["ssh"], local_opencode=shutil.which("opencode") or "opencode")
    local, remote = Endpoint(options), Endpoint(options, remote=True)
    try:
        report(OK, "Local OpenCode", f"OpenCode {probe_api(local)}")
    except BridgeError as exc:
        report(FAIL, "Local OpenCode", str(exc)[:300],
               "Check that OpenCode V2 is installed and authenticated: `opencode api get /api/info`.")
    inspection = diagnose_remote(remote, cfg, root, deploy.FILES, deploy.inspection_script(), report)
    diagnose_delivery(collab, root, inspection, report)
    return findings


def diagnose_remote(remote, cfg, root, files, script, report):
    """Network, SSH, remote API, and remote installation; returns the inspection or None."""
    try:
        host, port = remote.transport.destination()
        probe = tcp_probe(host, port)
    except BridgeError as exc:
        report(FAIL, "Network", ssh_problem(exc), "Check that the OpenSSH client works: `ssh -V`.")
        probe = None
    if probe is not None:
        if probe.ok:
            report(OK, "Network", f"{host}:{port} answers" + (f" ({probe.detail})" if probe.detail else ""))
        else:
            report(FAIL, "Network", f"{host}:{port}: {probe.detail}", NETWORK_HINTS[probe.reason])
    if probe is None or not probe.ok:
        for area in ("SSH connection", "Remote OpenCode", "Remote installation"):
            report(SKIP, area, "skipped: the Windows machine is not reachable")
        return None

    try:
        master = remote.transport.master_alive()
    except BridgeError as exc:
        report(FAIL, "SSH connection", str(exc),
               "Use a private 0700 runtime directory for the SSH socket (README setup, step 3).")
        for area in ("Remote OpenCode", "Remote installation"):
            report(SKIP, area, "skipped: SSH is not available")
        return None
    try:
        remote_version, problem, kind = probe_api(remote), None, None
    except BridgeError as exc:
        remote_version, problem = None, ssh_problem(exc)
        kind = failure_kind(problem)
    ssh_works = master or remote_version is not None or kind == "remote"
    if master:
        report(OK, "SSH connection", "reusable connection is open")
    elif ssh_works:
        report(OK, "SSH connection", "no reusable connection, but non-interactive authentication works")
    elif kind == "hostkey":
        report(FAIL, "SSH connection", problem, "Verify the host key fingerprint independently before "
               "trusting it; never disable host key checking.")
    elif kind == "auth":
        report(FAIL, "SSH connection", "the reusable connection is closed and non-interactive "
               "authentication failed", CONNECT_HINT)
    else:
        report(FAIL, "SSH connection", problem, CONNECT_HINT if kind == "timeout" else NETWORK_HINTS["error"])
    if not ssh_works:
        for area in ("Remote OpenCode", "Remote installation"):
            report(SKIP, area, "skipped: SSH is not available")
        return None

    wsl_user = cfg["ssh"]["wsl_user"]
    if remote_version is not None:
        report(OK, "Remote OpenCode", f"OpenCode {remote_version} in WSL")
    elif kind == "timeout":
        report(FAIL, "Remote OpenCode", "WSL or OpenCode did not answer in time",
               "WSL may still be starting: retry. If it persists, check OpenCode inside WSL.")
    elif any(marker in problem.lower() for marker in WSL_MARKERS):
        report(FAIL, "Remote OpenCode", problem, f"Check WSL on Windows: `wsl.exe -u {wsl_user} -- true`.")
    elif "no such file" in problem.lower() or "not found" in problem.lower():
        report(FAIL, "Remote OpenCode", problem,
               "Check `remote_opencode` in routes.json with `command -v opencode` inside WSL.")
    else:
        report(FAIL, "Remote OpenCode", problem, "Run `python3 bridge.py check` to see the complete error.")
    try:
        inspection = remote.remote_exec(["python3", "-I", "-c", script], timeout=60, data=json.dumps(
            {"root": cfg["ssh"]["remote_directory"], "files": list(files)}))
        if not isinstance(inspection, dict) or not isinstance(inspection.get("python"), list):
            raise BridgeError("unexpected inspection result.")
    except BridgeError as exc:
        report(FAIL, "Remote installation", ssh_problem(exc),
               f"Check that Python works inside WSL: `wsl.exe -u {wsl_user} -- python3 -V`.")
        return None
    diagnose_installation(inspection, cfg, root, files, report)
    return inspection


def diagnose_installation(inspection, cfg, root, files, report):
    area, issues = "Remote installation", []

    def issue(status, summary, hint):
        issues.append(summary)
        report(status, area, summary, hint)

    python = ".".join(map(str, inspection["python"]))
    if tuple(inspection["python"]) < (3, 12):
        issue(FAIL, f"WSL has Python {python}; 3.12+ is required", "Install Python 3.12 or newer in WSL as `python3`.")
    if not inspection.get("installed"):
        issue(FAIL, f"{cfg['ssh']['remote_directory']} does not exist in WSL",
              "Deploy it with `python3 deploy.py --install-config` (README setup, step 4).")
        return
    manifest = inspection.get("manifest") if isinstance(inspection.get("manifest"), dict) else {}
    remote_files = inspection.get("files") or {}
    stale, edited = [], []
    for name in files:
        path = Path(root) / name
        local = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        current = remote_files.get(name)
        if current == local:
            continue
        (edited if current is not None and manifest.get(name) != current else stale).append(name)
    if edited:
        issue(WARN, f"edited in WSL since the last deployment: {listing(edited)}",
              "Review those changes on the WSL side first; deploy.py refuses to overwrite them.")
    if stale:
        issue(WARN, f"not up to date with this checkout: {listing(stale)}",
              "Run `python3 deploy.py`, then reconnect `laptop_bridge` with /mcps on the WSL side.")
    if inspection.get("state_private") is False:
        issue(FAIL, ".state in WSL is not private (0700, owned by the WSL user)",
              "The mailbox refuses to run like this: check its owner, then restrict it with chmod 700.")
    remote_cfg = inspection.get("config")
    if not remote_cfg:
        issue(FAIL, "routes.json is missing in WSL", "Install it with `python3 deploy.py --install-config`.")
    elif remote_cfg.get("invalid") or remote_cfg.get("side") != "wsl":
        issue(FAIL, "routes.json in WSL is invalid or does not declare side=wsl",
              "Reinstall it with `python3 deploy.py --install-config`.")
    elif remote_cfg.get("peers") != cfg["peers"]:
        issue(WARN, "shortcuts differ between the two machines",
              "Reinstall the configuration with `python3 deploy.py --install-config`; "
              "file transfers need matching registrations.")
    if not issues:
        report(OK, area, f"{len(files)} files match this checkout; Python {python}")


def diagnose_delivery(collab, root, inspection, report):
    """Relay process and both queues, without opening a writable mailbox or the relay lock."""
    relay, queues, problems = collab.relay_status(root), {}, []
    try:
        queues["linux"] = collab.queue_summary(root)
    except BridgeError as exc:
        problems.append(f"linux: {exc}")
    if inspection is None:
        remote_state = "not checked"
    elif inspection.get("queue_error"):
        problems.append(f"wsl: {inspection['queue_error']}")
        remote_state = None
    elif inspection.get("queue") is None:
        remote_state = "no mailbox yet"
    else:
        queues["wsl"], remote_state = inspection["queue"], None
    pending = sum(counts.get("pending", 0) for counts in queues.values())
    if relay["running"]:
        report(OK, "Relay", f"running (pid {relay['pid']})")
    else:
        stale = " (stale pid file)" if relay["stale"] else ""
        waiting = f"; {pending} message(s) waiting" if pending else ""
        report(WARN, "Relay", f"not running{stale}{waiting}",
               "Nothing is delivered until you start `python3 collab.py relay` on Linux; "
               "it can deliver pending messages and use model quota.")
    summary = " | ".join([f"{node}: {describe(counts)}" for node, counts in queues.items()]
                         + ([f"wsl: {remote_state}"] if remote_state else []))
    attention = sum(counts.get(state, 0) for counts in queues.values() for state in ("sending", "uncertain"))
    failed = sum(counts.get("failed", 0) for counts in queues.values())
    if problems:
        report(FAIL, "Queues", "; ".join(problems), "Do not delete the database; review it before using the bridge.")
    elif attention:
        report(WARN, "Queues", summary, "Review sending/uncertain messages with `python3 collab.py status` "
               "on each machine; never resend them blindly.")
    elif failed:
        report(WARN, "Queues", summary, "Failed messages were never sent; send one again yourself "
               "only if it is still needed.")
    else:
        report(OK, "Queues", summary)


def print_findings(findings):
    print("OpenCode Connect doctor (read-only: sends no messages and starts nothing)")
    width = max(len(area) for _, area, _, _ in findings)
    for status, area, summary, hint in findings:
        print(f"  {status:<4}  {area:<{width}}  {summary}")
        if hint:
            print(f"  {'':<4}  {'':<{width}}  -> {hint}")
    print("MCP setup is per OpenCode project and is not checked here: use /mcps and whoami.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["connect", "check", "doctor"])
    args = parser.parse_args()
    if args.command == "doctor":
        try:
            findings = diagnose()
        except (BridgeError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nDoctor stopped; nothing was changed.", file=sys.stderr)
            return 130
        print_findings(findings)
        return 1 if any(status == FAIL for status, *_ in findings) else 0
    try:
        cfg = load_config()
        if cfg["side"] != "linux":
            raise BridgeError("Run connect/check from the Linux coordinator.")
        options = SimpleNamespace(**cfg["ssh"], local_opencode=shutil.which("opencode") or "opencode")
        local, remote = Endpoint(options), Endpoint(options, remote=True)
        if args.command == "connect":
            remote.connect()
            print("Reusable connection opened (expires after 10 idle minutes).")
        else:
            check([("Linux", local), ("Windows/WSL", remote)])
    except (BridgeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nClient stopped; no agents were cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
