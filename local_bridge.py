#!/usr/bin/env python3
"""Local OpenCode text queues: no SSH, remote configuration, or model-backed discovery."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

from collab import Mailbox
from config import local_node, private_directory
from core import (AddressBook, BridgeError, DeliveryAdapters, NotSent,
                  deliver_message, relay_once, resolve_target)
from opencode_adapter import OpenCodeAdapter, OpenCodeCli
from transports import LocalCommandTransport


def state_root():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    if not base.is_absolute():
        raise BridgeError("XDG_STATE_HOME must be absolute.")
    return private_directory(base / "opencode-connect" / "local")


def local_adapter():
    return OpenCodeAdapter(OpenCodeCli(LocalCommandTransport(), shutil.which("opencode") or "opencode"))


def session(value, node=None):
    node = local_node(node)
    if isinstance(value, str) and ":" in value:
        prefix, value = value.split(":", 1)
        if prefix not in ("local", node):
            raise BridgeError("The destination is not in the configured local node.")
    return OpenCodeAdapter.session_id(value)


class LocalMailbox(Mailbox):
    """Separate database using the existing claim/receipt schema and crash behavior."""

    def __init__(self, root, node=None):
        self.node = local_node(node)
        self.contract = {"schema_version": 1, "mode": "local", "canonical_node": self.node}
        super().__init__(root)

    def _stored_contract(self):
        try:
            exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_contract'").fetchone()
            if not exists:
                return None
            rows = self.db.execute("SELECT schema_version, mode, canonical_node FROM local_contract").fetchall()
        except sqlite3.DatabaseError as exc:
            raise BridgeError("Cannot read local queue contract; preserve state and review manually.") from exc
        if len(rows) > 1:
            raise BridgeError("Invalid local queue contract; review state manually.")
        return dict(rows[0]) if rows else None

    def _check_contract(self):
        """Called only inside the transaction that inserts or claims a message."""
        stored = self._stored_contract()
        if stored is not None:
            if stored != self.contract:
                raise BridgeError("Local queue configuration mismatch; no message was inserted or claimed.")
            return
        if self.db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
            raise BridgeError("Unbound local queue contains messages; review or migrate explicitly before use.")
        self.db.execute("""CREATE TABLE IF NOT EXISTS local_contract (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL, mode TEXT NOT NULL, canonical_node TEXT NOT NULL)""")
        self.db.execute("INSERT INTO local_contract VALUES (1, ?, ?, ?)",
                        (self.contract["schema_version"], self.contract["mode"], self.node))

    def diagnostics(self):
        # Use one read snapshot; this is a configuration comparison, not service identity.
        with self.db:
            self.db.execute("BEGIN")
            stored = self._stored_contract()
            populated = self.db.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None
            return {"configured": dict(self.contract), "stored": stored,
                    "queue_path": str((self.state / "mail.sqlite3").resolve()),
                    "compatible": stored == self.contract if stored is not None else not populated}

    def claim(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_contract()
            row = self.db.execute("SELECT * FROM messages WHERE state='pending' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE messages SET state='sending' WHERE id=?", (row["id"],))
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def enqueue(self, source_id, target, text):
        source_id, target_id = session(source_id, self.node), session(target, self.node)
        if source_id == target_id:
            raise BridgeError("Sending to the caller's own session is not supported.")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise BridgeError("Messages must contain between 1 and 12000 characters.")
        source, destination = self.node + ":" + source_id, self.node + ":" + target_id
        payload = {"text": text, "source_id": source_id, "target_id": target_id,
                   "source_address": source, "target_address": destination}
        message_id = "msg_" + uuid.uuid4().hex
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_contract()
            count = self.db.execute("SELECT count(*) FROM messages WHERE created > ?",
                                    (time.time() - 3600,)).fetchone()[0]
            if count >= 30:
                raise BridgeError("The local queue has reached the limit of 30 sends per hour.")
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL)",
                            (message_id, time.time(), source, destination, "queue", "message",
                             json.dumps(payload, ensure_ascii=False), "pending"))
        return {"id": message_id, "state": "pending", "delivery": "queue"}


def deliver_local(job, adapter, *, node=None):
    # Only this preflight is known to have no delivery side effects. Do not wrap POSTs.
    try:
        if job["kind"] != "message" or job["priority"] != "queue":
            raise BridgeError("The local bridge supports normal text messages only.")
        node = local_node(node)
        book = AddressBook({}, {node: OpenCodeAdapter.session_id})
        target = resolve_target({}, job, addresses=book, origin_node=node)
        if book.resolve(job["source"]) == target:
            raise BridgeError("Self delivery is not supported.")
        if "queue" not in adapter.supported_deliveries:
            raise BridgeError("The local adapter does not support queue delivery.")
        # Recheck existence without reading conversations or changing permissions.
        response = adapter.api.call("get", f"/api/session/{target.session}")
        if (not isinstance(response, dict) or not isinstance(response.get("data"), dict)
                or response["data"].get("id") != target.session):
            raise BridgeError("The destination session could not be verified.")
        info = response["data"]
        timestamps = info.get("time", {})
        if (not isinstance(timestamps, dict) or info.get("parentID")
                or timestamps.get("archived") is not None):
            raise BridgeError("The destination must be an unarchived root session.")
    except (BridgeError, KeyError, TypeError, ValueError, OSError) as exc:
        raise NotSent(str(exc)) from exc
    return deliver_message(job, {}, lambda node: DeliveryAdapters(adapter),
                           addresses=book, origin_node=node)


def local_relay_once(box, adapter):
    # Leave pending messages untouched while service discovery or access fails.
    info = adapter.api.call("get", "/api/info")
    if (not isinstance(info, dict) or not isinstance(info.get("version"), str)
            or not info["version"].startswith("2.")):
        raise BridgeError("The local OpenCode V2 service is unavailable.")
    return relay_once(box, lambda job: deliver_local(job, adapter, node=box.node))


def relay(box, adapter, *, once=False):
    path = box.state / "relay.lock"
    if path.is_symlink():
        raise BridgeError("The local relay lock must not be a symlink.")
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError("A local relay is already running.") from exc
        while True:
            try:
                outcome = local_relay_once(box, adapter)
            except (BridgeError, OSError) as exc:
                if once:
                    raise
                print(json.dumps({"state": "unavailable", "error": str(exc)}), flush=True)
                time.sleep(1)
                continue
            if outcome:
                print(json.dumps(outcome), flush=True)
            if once:
                return
            time.sleep(1)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("find", help="Search session titles; never reads conversations.")
    find.add_argument("query")
    find.add_argument("--cursor")
    send = commands.add_parser("send", help="Queue an explicitly authorized text message by session ID.")
    send.add_argument("--source", required=True)
    send.add_argument("--to", required=True)
    send.add_argument("--text", required=True)
    commands.add_parser("status", help="Inspect local queue receipts, not message contents.")
    commands.add_parser("diagnose", help="Compare local node and queue contracts; does not verify service identity.")
    worker = commands.add_parser("relay", help="Deliver queued messages; may consume model quota.")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    box = None
    try:
        node = local_node()
        adapter = local_adapter()
        if args.command == "find":
            result = adapter.find_sessions(args.query, cursor=args.cursor)
        else:
            box = LocalMailbox(state_root(), node=node)
            if args.command == "send":
                result = box.enqueue(args.source, args.to, args.text)
            elif args.command == "status":
                result = box.status()
            elif args.command == "diagnose":
                result = box.diagnostics()
            else:
                relay(box, adapter, once=args.once)
                return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (BridgeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if box is not None:
            box.db.close()


if __name__ == "__main__":
    sys.exit(main())
