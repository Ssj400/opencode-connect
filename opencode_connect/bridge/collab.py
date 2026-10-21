"""Persistent mailboxes and an explicit relay between OpenCode V2 sessions."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
import uuid

from opencode_connect.bridge.bridge import BridgeError, Endpoint
from opencode_connect.config import ROOT, load_config, validate_config, private_directory
from opencode_connect.core import AddressBook, DeliveryAdapters, Peer, deliver_message, relay_once
from opencode_connect.opencode_adapter import OpenCodeAdapter
from opencode_connect.transports import CommandError

MAX_TEXT = 12000
MAX_FILE = 1024 * 1024
STATES = {"pending", "sending", "delivered", "uncertain", "failed", "cancelled"}


def settings(root=ROOT):
    return load_config(root)


def address_book(cfg):
    peers = {alias: Peer(peer["side"], peer["sessionID"]) for alias, peer in cfg["peers"].items()}
    return AddressBook(peers, {side: OpenCodeAdapter.session_id for side in ("linux", "wsl")})


def endpoint(cfg, side):
    validate_config(cfg)
    if side not in ("linux", "wsl"):
        raise BridgeError("Invalid side.")
    args = SimpleNamespace(**cfg["ssh"], local_opencode=shutil.which("opencode") or "opencode")
    return Endpoint(args, remote=side == "wsl")


def safe_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name):
        raise BridgeError("Invalid filename: use a simple name without directories.")
    if name.lower().startswith(("id_rsa", "id_ed25519", "credentials", "secrets")):
        raise BridgeError("Do not transfer credentials through the bridge.")
    return name


class Mailbox:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.state = self.root / ".state"
        private_directory(self.state)
        if (self.state / "mail.sqlite3").is_symlink():
            raise BridgeError("The database must not be a symlink.")
        self.db = sqlite3.connect(self.state / "mail.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
            target TEXT NOT NULL, priority TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT)""")
        self.db.commit()

    def enqueue(self, source_id, target, text, urgent=False, filename=None):
        cfg = settings(self.root)
        book = address_book(cfg)
        source = Peer(cfg["side"], OpenCodeAdapter.session_id(source_id))
        sources = [name for name, p in cfg["peers"].items()
                   if p["side"] == cfg["side"] and p["sessionID"] == source_id]
        peer = book.resolve(target)
        if filename is not None and (not book.registered(source) or not book.registered(peer)):
            raise BridgeError("File transfer requires explicitly registered source and destination sessions.")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise BridgeError(f"Messages must contain between 1 and {MAX_TEXT} characters.")
        if not isinstance(urgent, bool):
            raise BridgeError("urgent must be a boolean.")
        payload = {"text": text, "source_id": source_id, "target_id": peer.session,
                   "source_address": book.address(source), "target_address": book.address(peer)}
        kind = "message"
        if filename is not None:
            name = safe_name(filename)
            folder = self.root / "outgoing"
            path = folder / name
            if folder.is_symlink() or path.is_symlink() or not path.is_file():
                raise BridgeError("The file must exist in outgoing/ and must not be a symlink.")
            if path.stat().st_size > MAX_FILE:
                raise BridgeError("File exceeds 1 MiB.")
            with path.open("rb") as file:
                data = file.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise BridgeError("File exceeds 1 MiB.")
            payload["file"] = {"name": name, "data": base64.b64encode(data).decode(),
                               "sha256": hashlib.sha256(data).hexdigest()}
            kind = "file"
        message_id = "msg_" + uuid.uuid4().hex
        # Conservative rate limit, not a monetary spending guarantee.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute("SELECT count(*) FROM messages WHERE created > ?",
                                    (time.time() - 3600,)).fetchone()[0]
            if count >= 30:
                raise BridgeError("This machine has reached the limit of 30 sends per hour.")
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL)",
                            (message_id, time.time(), sources[0] if sources else book.address(source), target,
                             "steer" if urgent else "queue", kind,
                             json.dumps(payload, ensure_ascii=False), "pending"))
        return {"id": message_id, "state": "pending", "delivery": "steer" if urgent else "queue"}

    def claim(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM messages WHERE state='pending' "
                                  "ORDER BY CASE priority WHEN 'steer' THEN 0 ELSE 1 END, created LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE messages SET state='sending' WHERE id=?", (row["id"],))
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def mark(self, message_id, state, result=None):
        if state not in STATES - {"pending", "sending"}:
            raise BridgeError("Transition not allowed; messages are not retried automatically.")
        with self.db:
            row = self.db.execute("SELECT state FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row[0] != "sending":
                raise BridgeError("Only messages in the sending state can be finalized.")
            self.db.execute("UPDATE messages SET state=?,result=? WHERE id=?",
                            (state, json.dumps(result, ensure_ascii=False), message_id))
        return {"id": message_id, "state": state}

    def status(self):
        rows = self.db.execute("SELECT id,created,source,target,priority,kind,state,result "
                               "FROM messages ORDER BY created DESC LIMIT 30").fetchall()
        return [dict(row) for row in rows]

    def cancel_pending(self, message_id):
        # Cancel only an unsent local message, never an agent execution.
        with self.db:
            changed = self.db.execute("UPDATE messages SET state='cancelled' "
                                      "WHERE id=? AND state='pending'", (message_id,)).rowcount
        if not changed:
            raise BridgeError("Only pending messages can be cancelled.")
        return {"id": message_id, "state": "cancelled"}


def receive_file(root, message_id, payload):
    if not re.fullmatch(r"msg_[a-f0-9]{32}", message_id):
        raise BridgeError("Invalid transfer ID.")
    name = safe_name(payload["name"])
    if len(payload["data"]) > ((MAX_FILE + 2) // 3) * 4:
        raise BridgeError("File exceeds 1 MiB.")
    try:
        data = base64.b64decode(payload["data"], validate=True)
    except ValueError as exc:
        raise BridgeError("Invalid Base64 data.") from exc
    if len(data) > MAX_FILE or hashlib.sha256(data).hexdigest() != payload["sha256"]:
        raise BridgeError("Invalid file size or hash.")
    inbox = root / "inbox"
    folder = inbox / message_id
    if inbox.is_symlink() or folder.is_symlink():
        raise BridgeError("The destination must not be a symlink.")
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = folder / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise BridgeError("The file already exists and was not overwritten.") from exc
    with os.fdopen(fd, "wb") as file:
        file.write(data)
    return {"path": str(path), "sha256": payload["sha256"]}


def rpc(request, root=ROOT):
    op = request["op"]
    if op == "receive_file":
        return receive_file(root, request["id"], request["file"])
    if op == "config":
        return settings(root)
    box = Mailbox(root)
    try:
        if op == "claim":
            return box.claim()
        if op == "status":
            return box.status()
        if op == "mark":
            return box.mark(request["id"], request["state"], request.get("result"))
        if op == "enqueue":
            return box.enqueue(request["source_id"], request["target"], request["text"],
                               request.get("urgent", False), request.get("filename"))
        raise BridgeError("Unknown operation.")
    finally:
        box.db.close()


def remote_rpc(cfg, request):
    try:
        return endpoint(cfg, "wsl").remote_exec(
            ["python3", cfg["ssh"]["remote_directory"] + "/bin/collab.py", "rpc"],
            data=json.dumps(request, ensure_ascii=False), timeout=40)
    except CommandError as exc:
        try:
            error = json.loads(exc.stdout)
        except (ValueError, UnicodeError):
            error = None
        if (isinstance(error, dict) and error.get("_tag") == "BridgeError"
                and isinstance(error.get("message"), str)):
            raise BridgeError(error["message"]) from exc
        raise


def deliver(cfg, job, root=ROOT, origin_side=None):
    book = address_book(cfg)

    def adapters_for(side):
        def receive(message_id, payload):
            req = {"op": "receive_file", "id": message_id, "file": payload}
            return remote_rpc(cfg, req) if side == "wsl" else rpc(req, root)
        return DeliveryAdapters(OpenCodeAdapter(endpoint(cfg, side)), receive)

    return deliver_message(job, book.peers, adapters_for, label="OpenCode bridge",
                           addresses=book, origin_node=origin_side)


class RpcMailbox:
    """Application adapter for local or remote mailbox RPC, not a core concern."""

    def __init__(self, call):
        self.call = call

    def claim(self):
        return self.call({"op": "claim"})

    def mark(self, message_id, state, result=None):
        return self.call({"op": "mark", "id": message_id, "state": state, "result": result})


def relay_cycle(cfg, root=ROOT):
    for side in ("linux", "wsl"):
        call = (lambda req: rpc(req, root)) if side == "linux" else (lambda req: remote_rpc(cfg, req))
        outcome = relay_once(RpcMailbox(call), lambda job: deliver(cfg, job, root, origin_side=side))
        if outcome is None:
            continue
        if outcome["state"] == "uncertain":
            print(f"{outcome['id']}: uncertain (check status)", flush=True)
        else:
            print(f"{outcome['id']}: delivered -> {outcome['target']} ({outcome['delivery']})", flush=True)


def relay(args, root=ROOT):
    cfg = settings(root)
    if cfg["side"] != "linux":
        raise BridgeError("The only relay runs on Linux.")
    state = root / ".state"
    private_directory(state)
    with (state / "relay.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError("A relay is already running.") from exc
        (state / "relay.pid").write_text(str(os.getpid()))
        print(f"Relay running pid={os.getpid()}; Ctrl+C stops it without cancelling agents.", flush=True)
        try:
            while True:
                try:
                    relay_cycle(settings(root), root)
                except BridgeError as exc:
                    print(f"Transport unavailable: {str(exc)[:500]}", flush=True)
                    if args.once:
                        raise
                    time.sleep(10)
                if args.once:
                    return
                time.sleep(args.interval)
        finally:
            (state / "relay.pid").unlink(missing_ok=True)


def process_is_relay(pid):
    cmdline = Path("/proc") / str(pid) / "cmdline"
    if Path("/proc/self").exists():
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            return False
        return b"relay" in argv and any(arg.endswith(b"collab.py") for arg in argv)
    try:
        os.kill(pid, 0)  # Without /proc, process existence is the best available signal.
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def relay_status(root=ROOT):
    """Report this installation's relay from its pid file; never touches the relay lock."""
    try:
        pid = int((Path(root) / ".state" / "relay.pid").read_text())
    except FileNotFoundError:
        return {"running": False, "pid": None, "stale": False}
    except (OSError, ValueError):
        return {"running": False, "pid": None, "stale": True}
    running = process_is_relay(pid)
    return {"running": running, "pid": pid, "stale": not running}


def queue_summary(root=ROOT):
    """Count messages per state with a read-only connection; no database means an empty queue."""
    path = Path(root) / ".state" / "mail.sqlite3"
    if path.is_symlink():
        raise BridgeError("The database must not be a symlink.")
    if not path.is_file():
        return {}
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return dict(db.execute("SELECT state, count(*) FROM messages GROUP BY state").fetchall())
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise BridgeError(f"Could not read the mailbox: {exc}") from exc


def create_session(args, root=ROOT):
    # Explicit administration from Linux. Do not register sessions automatically
    # or change existing sessions' agents or permissions.
    cfg = settings(root)
    if cfg["side"] != "linux":
        raise BridgeError("Create remote sessions from the Linux coordinator.")
    if not args.title.strip():
        raise BridgeError("A title is required.")
    directory = str(root) if args.side == "linux" else cfg["ssh"]["remote_directory"]
    session = OpenCodeAdapter(endpoint(cfg, args.side)).create_session(title=args.title, directory=directory)
    return {"session": session, "note": "Created without starting tasks. Explicitly register an alias to collaborate."}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rpc", help=argparse.SUPPRESS)
    sub.add_parser("peers")
    sub.add_parser("status")
    send = sub.add_parser("send")
    send.add_argument("--source", required=True, help="Local session alias or node:ses_ID; MCP uses session metadata instead.")
    send.add_argument("--to", required=True, help="Alias or linux:ses_ID / wsl:ses_ID on paired nodes.")
    send.add_argument("--text", required=True)
    send.add_argument("--urgent", action="store_true")
    send.add_argument("--file", help="Name of a file explicitly staged in outgoing/.")
    cancel = sub.add_parser("cancel-pending")
    cancel.add_argument("id")
    worker = sub.add_parser("relay")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=float, default=3)
    create = sub.add_parser("create-session")
    create.add_argument("--side", choices=["linux", "wsl"], required=True)
    create.add_argument("--title", required=True)
    args = parser.parse_args()
    try:
        if args.command == "rpc":
            result = rpc(json.load(sys.stdin))
        elif args.command == "relay":
            if args.interval < 1:
                raise BridgeError("The minimum interval is 1 second.")
            relay(args)
            return 0
        elif args.command == "peers":
            result = settings()["peers"]
        elif args.command == "create-session":
            result = create_session(args)
        else:
            box = Mailbox()
            try:
                if args.command == "status":
                    result = box.status()
                elif args.command == "cancel-pending":
                    result = box.cancel_pending(args.id)
                else:
                    cfg = settings()
                    source = address_book(cfg).resolve(args.source)
                    if source.node != cfg["side"]:
                        raise BridgeError("The source alias is not local.")
                    result = box.enqueue(source.session, args.to, args.text, args.urgent, args.file)
            finally:
                box.db.close()
        print(json.dumps(result, ensure_ascii=False))
    except (BridgeError, KeyError, ValueError, OSError) as exc:
        if args.command == "rpc":
            print(json.dumps({"_tag": "BridgeError", "message": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nRelay stopped; no sessions were cancelled.", file=sys.stderr)
        return 130
    return 0
