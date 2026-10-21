"""Read-only courier diagnosis: configuration, state, and bounded identity probes."""

import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier.settings import (SOCKET_TIMEOUT, check_private_directory, listen_address,
                                               read_config, validate_config)
from opencode_connect.transports import http_json


def local_queue_root():
    """Read-only counterpart of mailbox.state_root; never creates its path."""
    base = Path(os.environ.get("OPENCODE_CONNECT_LOCAL_STATE_HOME",
                               os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))))
    if not base.is_absolute():
        raise BridgeError("The local bridge state home must be absolute.")
    return base / "opencode-connect/local"


def open_state(root):
    """A read-only connection to the queue holding the courier's tables, once its files
    are private; None before the queue exists. Nothing is created or repaired."""
    database = Path(root) / ".state/mail.sqlite3"
    if not database.exists() and not database.is_symlink():
        return None
    check_private_directory(database.parent)
    info = database.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise BridgeError("Insecure courier database.")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            info = sidecar.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise BridgeError("Insecure courier database sidecar.")
    return sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=SOCKET_TIMEOUT)


def listener_answers(listen):
    """Whether something accepts TCP at the listen address, which alone does not
    establish process identity; None when the address names no single endpoint."""
    host, port = listen_address(listen, allow_wildcard=True)
    if ipaddress.ip_address(host).is_unspecified or port == 0:
        return None
    try:
        with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT):
            return True
    except OSError:
        return False


def doctor(config_path, *, root=None, network=True, clock=time.time):
    """Read configuration/state and bounded identity probes; never send or approve text."""
    findings = []
    try:
        config = read_config(config_path)
    except (BridgeError, OSError):
        return [{"status": "FAIL", "area": "Configuration", "detail": "Missing or insecure private configuration."}]
    findings.append({"status": "OK", "area": "Configuration", "detail": "Owned private directory and 0600 file."})
    root = local_queue_root() if root is None else Path(root)
    try:
        db = open_state(root)
        if db is None:
            findings.append({"status": "WARN", "area": "State", "detail": "Courier queue has not been initialized."})
        else:
            try:
                db.execute("BEGIN")
                saved = db.execute("SELECT data FROM courier_settings WHERE id=1").fetchone()
                if saved is None:
                    raise BridgeError("Missing courier configuration snapshot.")
                saved = validate_config(json.loads(saved[0]))
                if any(saved[key] != config[key] for key in ("person", "listen", "url")):
                    raise BridgeError("Courier identity differs from stored state.")
                if saved != config:
                    findings.append({"status": "WARN", "area": "Snapshot", "detail": "JSON snapshot is stale; SQLite remains authoritative."})
                config = saved
                held = db.execute("SELECT count(*) FROM courier_inbound WHERE state='held'").fetchone()[0]
                expired = db.execute("SELECT count(*) FROM courier_inbound WHERE state='held' AND expires<=?", (clock(),)).fetchone()[0]
                sending = db.execute("SELECT count(*) FROM courier_sent WHERE state='sending'").fetchone()[0]
                retry_due = db.execute("SELECT count(*) FROM courier_sent WHERE state='retry' AND next_attempt<=?", (clock(),)).fetchone()[0]
                findings.append({"status": "WARN" if sending or retry_due or expired else "OK",
                                 "area": "Courier state", "held": held, "expiry_due": expired,
                                 "sending_review": sending, "retry_due": retry_due})
            finally:
                db.close()
            from opencode_connect.bridge.collab import queue_summary
            counts = queue_summary(root)
            findings.append({"status": "WARN" if any(counts.get(k) for k in ("pending", "sending", "uncertain", "failed")) else "OK",
                             "area": "Local queue", "counts": counts,
                             "detail": "All namespaces; pending may require approval consumers or a local relay."})
    except (BridgeError, OSError, ValueError, sqlite3.Error):
        findings.append({"status": "FAIL", "area": "State", "detail": "Insecure, unreadable, or incompatible courier state; no repair attempted."})
        # Do not probe contacts from a possibly stale JSON snapshot if SQLite
        # could not establish the authoritative current permissions.
        return findings
    if not network:
        findings.append({"status": "WARN", "area": "Network", "detail": "Network probes explicitly skipped."})
        return findings
    answers = listener_answers(config["listen"])
    if answers is None:
        findings.append({"status": "WARN", "area": "Listener", "detail": "A specific, nonzero listen endpoint is needed for a reachability probe."})
    elif answers:
        findings.append({"status": "OK", "area": "Listener", "detail": "TCP reachable; this alone does not establish process identity."})
    else:
        findings.append({"status": "FAIL", "area": "Listener", "detail": "Configured TCP listener is not reachable."})
    for name, card in sorted(config["contacts"].items()):
        try:
            identity = http_json(card["url"] + "/v1/whoami", headers={"Authorization": "Bearer " + card["token"]},
                                 timeout=SOCKET_TIMEOUT)
            matches = (isinstance(identity, dict) and identity.get("person") == name
                       and type(identity.get("protocol")) is int and identity["protocol"] == 1)
            detail = "Authenticated courier identity matches." if matches else "Courier identity does not match the contact card."
        except BridgeError:
            matches, detail = False, "Contact unreachable, token rejected, or invalid identity response."
        findings.append({"status": "OK" if matches else "FAIL", "area": "Contact", "person": name, "detail": detail})
    return findings
