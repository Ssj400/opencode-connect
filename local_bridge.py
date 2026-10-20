#!/usr/bin/env python3
"""Local OpenCode text queues: no SSH, remote configuration, or model-backed discovery."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
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


def state_path():
    """The local bridge's state folder, without creating it."""
    base = Path(os.environ.get("OPENCODE_CONNECT_LOCAL_STATE_HOME",
                              os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))))
    if not base.is_absolute():
        raise BridgeError("The local bridge state home must be absolute.")
    return base / "opencode-connect" / "local"


def state_root():
    return private_directory(state_path())


def local_adapter():
    return OpenCodeAdapter(OpenCodeCli(LocalCommandTransport(), shutil.which("opencode") or "opencode"))


def session(value, node=None):
    node = local_node(node)
    if isinstance(value, str) and ":" in value:
        prefix, value = value.split(":", 1)
        if prefix not in ("local", node):
            raise BridgeError("The destination is not in the configured local node.")
    return OpenCodeAdapter.session_id(value)


CLAUDE_SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
PERSON_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def claude_session_id(value):
    """Claude Code session IDs are UUIDs, compared in lowercase."""
    if not isinstance(value, str) or not CLAUDE_SESSION_ID.fullmatch(value.lower()):
        raise BridgeError("Invalid Claude Code session ID.")
    return value.lower()


def person_name(value):
    if not isinstance(value, str) or not PERSON_NAME.fullmatch(value.lower()):
        raise BridgeError("Invalid person name.")
    return value.lower()


def namespaces(node=None):
    """Address namespaces of one local queue: this node's OpenCode sessions,
    Claude Code sessions, and remote people. Any other prefix is rejected."""
    return {local_node(node): OpenCodeAdapter.session_id,
            "claude": claude_session_id, "person": person_name}


def canonical(value, node=None):
    """Canonical queue address; a bare ID or `local:` means this node's OpenCode session."""
    node = local_node(node)
    if not isinstance(value, str):
        raise BridgeError("Invalid address.")
    prefix, separator, identifier = value.partition(":")
    if not separator:
        prefix, identifier = node, value
    elif prefix == "local":
        prefix = node
    validate = namespaces(node).get(prefix)
    if validate is None:
        raise BridgeError("The destination is not in the configured local node.")
    return f"{prefix}:{validate(identifier)}"


# Version 2 adds the claude: and person: namespaces. Rows keep the same schema.
SCHEMA_VERSION = 2
STATES = frozenset(("held", "pending", "sending", "delivered", "uncertain", "failed", "cancelled", "rejected"))
REMOTE_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MESSAGE_ID = re.compile(r"msg_[0-9a-f]{32}")


class LocalMailbox(Mailbox):
    """Separate database using the existing claim/receipt schema and crash behavior."""

    def __init__(self, root, node=None):
        self.node = local_node(node)
        self.contract = {"schema_version": SCHEMA_VERSION, "mode": "local", "canonical_node": self.node}
        super().__init__(root)

    def _migratable(self, stored):
        return (stored is not None and stored.get("schema_version") == 1
                and stored.get("mode") == "local" and stored.get("canonical_node") == self.node)

    def migrate(self):
        """Upgrade a version 1 contract bound to this node, leaving every row untouched.

        Version 1 rows are OpenCode-only and stay valid, stale sending rows included,
        so they never block the upgrade. Version 1 code then refuses the queue.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            stored = self._stored_contract()
            if stored is None or stored == self.contract:
                return {"migrated": False, "contract": stored}
            if not self._migratable(stored):
                raise BridgeError("Only a version 1 queue bound to this node can be migrated.")
            self.db.execute("UPDATE local_contract SET schema_version=? WHERE singleton=1", (SCHEMA_VERSION,))
        return {"migrated": True, "contract": dict(self.contract)}

    def resolve(self, message_id, state):
        """Record a human decision about a stale sending or uncertain message; never resends."""
        if state not in ("delivered", "failed", "cancelled"):
            raise BridgeError("A resolved message must become delivered, failed, or cancelled.")
        with self._write():
            row = self.db.execute("SELECT state FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["state"] not in ("sending", "uncertain"):
                raise BridgeError("Only sending or uncertain messages can be resolved.")
            self.db.execute("UPDATE messages SET state=?, result=? WHERE id=?",
                            (state, json.dumps({"resolved_from": row["state"], "by": "operator"}), message_id))
        return {"id": message_id, "state": state}

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
                    "compatible": stored == self.contract if stored is not None else not populated,
                    "migration_needed": self._migratable(stored)}

    def claim(self, match=None):
        """Claim the oldest pending message whose target starts with `match`.

        Each consumer claims only its own namespace. The default is this node's
        OpenCode sessions, so the OpenCode relay never takes another consumer's rows.
        `substr` rather than LIKE: `_` is a wildcard there and appears in names.
        """
        match = self.node + ":" if match is None else match
        if not isinstance(match, str) or ":" not in match:
            raise BridgeError("A claim must name an address namespace.")
        with self._write():
            row = self.db.execute("SELECT * FROM messages WHERE state='pending' AND substr(target, 1, ?) = ? "
                                  "ORDER BY created LIMIT 1", (len(match), match)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE messages SET state='sending' WHERE id=?", (row["id"],))
        return self._decoded(row)

    @staticmethod
    def _decoded(row):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def get(self, message_id):
        """One message in the shape `claim` returns, or None. Read-only."""
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return None if row is None else self._decoded(row)

    @contextlib.contextmanager
    def transaction(self):
        """One write transaction that checks the queue contract before anything else.

        Every queue method joins it. A courier may use `self.db` inside it for its own
        `courier_` tables only, never for `messages`, `local_contract`, or
        `local_inbound_keys`. Commits on exit and rolls back on any exception.
        """
        if self.db.in_transaction:
            raise BridgeError("A local queue transaction is already open.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._check_contract()
            yield self

    @contextlib.contextmanager
    def _write(self):
        """Join the caller's transaction, or open one."""
        if self.db.in_transaction:
            yield
        else:
            with self.transaction():
                yield

    def mark(self, message_id, state, result=None):
        """Finalize a sending message, with the cross-machine mailbox's rules."""
        if state not in ("delivered", "uncertain", "failed", "cancelled"):
            raise BridgeError("Transition not allowed; messages are not retried automatically.")
        with self._write():
            row = self.db.execute("SELECT state FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["state"] != "sending":
                raise BridgeError("Only messages in the sending state can be finalized.")
            self.db.execute("UPDATE messages SET state=?,result=? WHERE id=?",
                            (state, json.dumps(result, ensure_ascii=False), message_id))
        return {"id": message_id, "state": state}

    def release(self, message_id):
        """Return a claimed upload to a remote person to pending: the courier's upload is idempotent."""
        with self._write():
            row = self.db.execute("SELECT state, target FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["state"] != "sending" or not row["target"].startswith("person:"):
                raise BridgeError("Only a sending message for a remote person can be released.")
            self.db.execute("UPDATE messages SET state='pending' WHERE id=?", (message_id,))
        return {"id": message_id, "state": "pending"}

    def insert_inbound(self, source, text, *, remote_key, reply_to=None):
        """Hold a remote person's message, with no target, until a human approves it.

        Idempotent per (source, remote_key): a repeat returns the first message.
        """
        source = canonical(source, self.node)
        if not source.startswith("person:"):
            raise BridgeError("Only a remote person's message is held for approval.")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise BridgeError("Messages must contain between 1 and 12000 characters.")
        if not isinstance(remote_key, str) or not REMOTE_KEY.fullmatch(remote_key):
            raise BridgeError("Invalid remote idempotency key.")
        if reply_to is not None and (not isinstance(reply_to, str) or not MESSAGE_ID.fullmatch(reply_to)):
            raise BridgeError("Invalid reply_to message ID.")
        with self._write():
            self.db.execute("""CREATE TABLE IF NOT EXISTS local_inbound_keys (
                source TEXT NOT NULL, remote_key TEXT NOT NULL, message_id TEXT NOT NULL,
                PRIMARY KEY (source, remote_key))""")
            known = self.db.execute("SELECT message_id FROM local_inbound_keys WHERE source=? AND remote_key=?",
                                    (source, remote_key)).fetchone()
            if known is not None:
                row = self.db.execute("SELECT state FROM messages WHERE id=?", (known[0],)).fetchone()
                return {"id": known[0], "state": row["state"], "duplicate": True}
            message_id = "msg_" + uuid.uuid4().hex
            payload = {"text": text, "source_id": source.split(":", 1)[1], "target_id": None,
                       "source_address": source, "target_address": None, "reply_to": reply_to}
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL)",
                            (message_id, time.time(), source, "", "queue", "message",
                             json.dumps(payload, ensure_ascii=False), "held"))
            self.db.execute("INSERT INTO local_inbound_keys VALUES (?,?,?)", (source, remote_key, message_id))
        return {"id": message_id, "state": "held", "duplicate": False}

    def approve_inbound(self, message_id, target):
        """Give a held message the destination a human chose; its consumer then claims it."""
        destination = canonical(target, self.node)
        if destination.startswith("person:"):
            raise BridgeError("An approved message must go to a local OpenCode or Claude Code session.")
        with self._write():
            row = self.db.execute("SELECT state, source, payload FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["state"] != "held" or not row["source"].startswith("person:"):
                raise BridgeError("Only a held message from a remote person can be approved.")
            payload = json.loads(row["payload"])
            payload.update(target_id=destination.split(":", 1)[1], target_address=destination)
            self.db.execute("UPDATE messages SET state='pending', target=?, payload=? WHERE id=?",
                            (destination, json.dumps(payload, ensure_ascii=False), message_id))
        return {"id": message_id, "state": "pending", "target": destination}

    def reject_inbound(self, message_id, reason=None):
        """Mark a held message `rejected`; a reason, such as an expiry, is kept as its result."""
        if reason is not None and (not isinstance(reason, str) or not reason.strip() or len(reason) > 500):
            raise BridgeError("A rejection reason must be short text.")
        result = None if reason is None else json.dumps({"reason": reason}, ensure_ascii=False)
        with self._write():
            changed = self.db.execute("UPDATE messages SET state='rejected', result=? WHERE id=? AND state='held' "
                                      "AND substr(source, 1, 7)='person:'", (result, message_id)).rowcount
        if not changed:
            raise BridgeError("Only a held message from a remote person can be rejected.")
        return {"id": message_id, "state": "rejected"}

    def cancel_person(self, person, *, outbound=False):
        """Cancel a person's held and approved-but-unclaimed messages; sending ones stay.

        With outbound=True, unsent pending messages to that person are cancelled too.
        """
        address = canonical(person if isinstance(person, str) and ":" in person else f"person:{person}",
                            self.node)
        if not address.startswith("person:"):
            raise BridgeError("Only a remote person's messages can be cancelled this way.")
        with self._write():
            changed = self.db.execute("UPDATE messages SET state='cancelled' WHERE source=? "
                                      "AND state IN ('held', 'pending')", (address,)).rowcount
            if outbound:
                changed += self.db.execute("UPDATE messages SET state='cancelled' WHERE target=? "
                                           "AND state='pending'", (address,)).rowcount
        return changed

    def inbound(self, states=("held",)):
        """Remote people's messages in the given states, oldest first. Text is raw."""
        states = tuple(states)
        if not states or set(states) - STATES:
            raise BridgeError("Unknown message state.")
        rows = self.db.execute(
            "SELECT id, source, created, state, payload FROM messages WHERE substr(source, 1, 7)='person:' "
            f"AND state IN ({','.join('?' * len(states))}) ORDER BY created", states).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            result.append({"id": row["id"], "source": row["source"], "created": row["created"],
                           "state": row["state"], "text": payload["text"], "reply_to": payload.get("reply_to")})
        return result

    def inbound_state(self, message_id, source):
        """The state of one message, only when it came from that remote person."""
        source = canonical(source, self.node)
        if not source.startswith("person:"):
            raise BridgeError("Only a remote person's message state can be read this way.")
        row = self.db.execute("SELECT state FROM messages WHERE id=? AND source=?", (message_id, source)).fetchone()
        return row["state"] if row else None

    def enqueue(self, source_id, target, text, reply_to=None):
        """Queue a local caller's message. `reply_to` names the message it answers, which
        must come from the destination and have been delivered to this caller. For a remote
        person, the courier uses it to tell them which of their sessions the reply belongs to.
        """
        source, destination = canonical(source_id, self.node), canonical(target, self.node)
        if source.startswith("person:"):
            raise BridgeError("Remote people enter the local queue only through the courier.")
        if source == destination:
            raise BridgeError("Sending to the caller's own session is not supported.")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise BridgeError("Messages must contain between 1 and 12000 characters.")
        if reply_to is not None and (not isinstance(reply_to, str) or not MESSAGE_ID.fullmatch(reply_to)):
            raise BridgeError("reply_to must be a message ID.")
        payload = {"text": text, "source_id": source.split(":", 1)[1],
                   "target_id": destination.split(":", 1)[1],
                   "source_address": source, "target_address": destination}
        if reply_to is not None:
            payload["reply_to"] = reply_to
        message_id = "msg_" + uuid.uuid4().hex
        with self._write():
            if reply_to is not None and not self.db.execute(
                    "SELECT 1 FROM messages WHERE id=? AND source=? AND target=? AND state='delivered'",
                    (reply_to, destination, source)).fetchone():
                raise BridgeError("reply_to must name a message from the destination delivered to this session.")
            # Only local sources count: held messages from other people must not consume this budget.
            count = self.db.execute("SELECT count(*) FROM messages WHERE created > ? "
                                    "AND substr(source, 1, 7) != 'person:'", (time.time() - 3600,)).fetchone()[0]
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
        book = AddressBook({}, namespaces(node))
        target = resolve_target({}, job, addresses=book, origin_node=frozenset(namespaces(node)))
        if target.node != node:
            raise BridgeError("The OpenCode relay delivers only to this node's sessions.")
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
    return deliver_message(job, {}, lambda node: DeliveryAdapters(adapter), addresses=book,
                           origin_node=frozenset(namespaces(node)), quoted_nodes=frozenset(("person",)))


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
    commands.add_parser("migrate", help="Upgrade a version 1 queue bound to this node; rows stay untouched.")
    resolve = commands.add_parser("resolve", help="Record a reviewed outcome for a stale sending or uncertain message.")
    resolve.add_argument("id")
    resolve.add_argument("--state", required=True, choices=("delivered", "failed", "cancelled"))
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
            elif args.command == "migrate":
                result = box.migrate()
            elif args.command == "resolve":
                result = box.resolve(args.id, args.state)
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
