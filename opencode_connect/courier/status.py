"""`status`: who this courier is, whether it runs, and what waits, for people to read.

Like `doctor`, it only reads: a read-only connection to the queue, `systemctl
--user is-active`, a TCP connection to its own listener, and /proc.
"""

import json
import os
from pathlib import Path
import sqlite3
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier import host
from opencode_connect.courier.doctor import listener_answers, local_queue_root, open_state
from opencode_connect.courier.service import SERVICE, unit_path
from opencode_connect.courier.settings import clean_display, read_config, validate_config

COURIER = "python3 bin/courier.py"


def ago(seconds):
    if seconds < 60:
        return "under a minute ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def processes(matches, proc=Path("/proc")):
    """PIDs of this user's processes whose argument list `matches`."""
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            if not entry.name.isdigit() or entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # It ended while it was read.
        if matches(argv):
            found.append(int(entry.name))
    return sorted(found)


def separate_relays(proc=Path("/proc")):
    """PIDs of this user's `local_bridge.py relay` processes. While one runs, the relay
    of a courier started with --relay, such as the service's, waits for it to stop."""
    return processes(lambda argv: b"relay" in argv and any(arg.endswith(b"local_bridge.py") for arg in argv), proc)


def courier_relays(proc=Path("/proc")):
    """PIDs of this user's `courier.py run --relay` processes, such as the service's."""
    return processes(lambda argv: b"run" in argv and b"--relay" in argv
                     and any(arg.endswith(b"courier.py") for arg in argv), proc)


def read_state(db, now):
    """The courier's tables, read in one transaction; None before the courier first ran."""
    db.execute("BEGIN")
    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def rows(table, query, *parameters):
        # Tables from a later version are missing until the courier runs again.
        return db.execute(query, parameters).fetchall() if table in tables else []

    saved = rows("courier_settings", "SELECT data FROM courier_settings WHERE id=1")
    if not saved:
        return None
    return {
        "config": validate_config(json.loads(saved[0][0])),
        "routes": {name: (target, mode) for name, target, mode in
                   rows("courier_routes", "SELECT contact, target, mode FROM courier_routes")},
        "peers": {name: (seen, error, failed) for name, seen, error, failed in
                  rows("courier_peers", "SELECT contact, last_seen, last_error, last_error_at FROM courier_peers")},
        "held": dict(rows("courier_inbound", "SELECT contact, count(*) FROM courier_inbound "
                                             "WHERE state='held' AND expires>? GROUP BY contact", now)),
        "theirs": dict(rows("courier_sent", "SELECT contact, count(*) FROM courier_sent WHERE state='held' "
                                            "GROUP BY contact")),
        "unsent": dict(rows("messages", "SELECT substr(target, 8), count(*) FROM messages "
                                        "WHERE target LIKE 'person:%' AND state IN ('pending', 'sending') GROUP BY target")),
        "offer": next(iter(rows("courier_offer", "SELECT state, name, contact, expires FROM courier_offer "
                                                 "WHERE id=1 AND expires>?", now)), None),
    }


def describe_contact(name, config, state, now):
    receiving, sending = name in config["tokens"], name in config["contacts"]
    parts = ["both ways" if receiving and sending else
             f"{name} can write to you, but you cannot write back: pair again" if receiving else
             f"you can write to {name}, but {name} cannot write to you: pair again"]
    if receiving:
        target, mode = state["routes"].get(name, (None, None))
        parts.append("messages are held with no destination" if target is None else
                     f"messages go to {target} without approval" if mode == "direct" else
                     f"messages are held for approval, then go to {target}")
    seen, error, failed = state["peers"].get(name, (None, None, None))
    parts.append(f"the last attempt failed {ago(now - failed)}: {error.rstrip('.')}" if error and (seen is None or failed > seen)
                 else f"last contact {ago(now - seen)}" if seen is not None else "no contact yet")
    lines = [f"  {name}: " + "; ".join(parts) + "."]
    for count, text in ((state["unsent"].get(name), f"to {name} wait to be sent"),
                        (state["theirs"].get(name), f"to {name} wait for {name}'s approval"),
                        (state["held"].get(name), f"from {name} wait for your approval")):
        if count:
            lines.append(f"    {count} message(s) {text}.")
    return lines


def status(config_path, *, root=None, clock=time.time, proc=Path("/proc"), wsl=None):
    """(exit code, lines) describing this courier."""
    try:
        config = read_config(config_path)
    except BridgeError:
        return 1, [f"No readable courier configuration: run `{COURIER} setup`, or `{COURIER} doctor --offline` "
                   "to see why."]
    now, state = clock(), None
    try:
        db = open_state(local_queue_root() if root is None else root)
        if db is not None:
            try:
                state = read_state(db, now)
            finally:
                db.close()
    except (BridgeError, OSError, ValueError, sqlite3.Error):
        return 1, [f"{config['person']}: the courier's state cannot be read; nothing was changed. "
                   f"See `{COURIER} doctor --offline`."]
    if state is not None:
        config = state["config"]  # The queue's copy is authoritative.
    lines = [f"{config['person']}: listening on {config['listen']}, reachable at {config['url']}."]
    if unit_path().is_symlink() or not unit_path().is_file():
        lines.append(f"Service: not installed; `{COURIER} service install` starts the courier at each login.")
    else:
        lines.append(f"Service: {host.command_output(['systemctl', '--user', 'is-active', SERVICE]) or 'unknown'}.")
    answers = listener_answers(config["listen"])
    if answers is not None:
        lines.append(f"Listener: answering on {config['listen']}." if answers else
                     f"Listener: nothing answers on {config['listen']}; contacts' messages wait on their side.")
    relays = separate_relays(proc)
    if relays:
        lines.append(f"A separate local relay runs (PID {', '.join(map(str, relays))}): it delivers this machine's "
                     "queue, and a courier started with --relay, such as the service, waits until it stops.")
    if state is None:
        lines.append("The courier has not run yet.")
    else:
        offer = state["offer"]
        if offer is not None and offer[0] == "open":
            lines.append(f"Pairing: an offer{' for ' + offer[1] if offer[1] else ''} is open until "
                         f"{time.strftime('%H:%M', time.localtime(offer[3]))}.")
        elif offer is not None and offer[0] == "provisional":
            lines.append(f"Pairing: {offer[2]} answered the offer; waiting for their confirmation.")
        names = sorted(set(config["tokens"]) | set(config["contacts"]))
        lines.append("Contacts:" if names else f"No contacts yet: `{COURIER} pair` prints a link for one.")
        for name in names:
            lines.extend(describe_contact(name, config, state, now))
        held = sum(state["held"].values())
        if held:
            lines.append(f"{held} message(s) wait for your approval: `{COURIER} approve` shows them first.")
    if host.on_wsl() if wsl is None else wsl:
        lines.append(host.WSL_LISTENS)
    return 0, [clean_display(line) for line in lines]
