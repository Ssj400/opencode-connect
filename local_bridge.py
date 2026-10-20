#!/usr/bin/env python3
"""Local OpenCode text queues: no SSH, remote configuration, or model-backed discovery."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from collab import Mailbox
from config import private_directory
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


def session(value):
    if isinstance(value, str) and value.startswith("local:"):
        value = value[len("local:"):]
    return OpenCodeAdapter.session_id(value)


class LocalMailbox(Mailbox):
    """Separate database using the existing claim/receipt schema and crash behavior."""

    def enqueue(self, source_id, target, text):
        source_id, target_id = session(source_id), session(target)
        if source_id == target_id:
            raise BridgeError("Sending to the caller's own session is not supported.")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise BridgeError("Messages must contain between 1 and 12000 characters.")
        source, destination = "local:" + source_id, "local:" + target_id
        payload = {"text": text, "source_id": source_id, "target_id": target_id,
                   "source_address": source, "target_address": destination}
        message_id = "msg_" + uuid.uuid4().hex
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute("SELECT count(*) FROM messages WHERE created > ?",
                                    (time.time() - 3600,)).fetchone()[0]
            if count >= 30:
                raise BridgeError("The local queue has reached the limit of 30 sends per hour.")
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL)",
                            (message_id, time.time(), source, destination, "queue", "message",
                             json.dumps(payload, ensure_ascii=False), "pending"))
        return {"id": message_id, "state": "pending", "delivery": "queue"}


def deliver_local(job, adapter):
    # Only this preflight is known to have no delivery side effects. Do not wrap POSTs.
    try:
        if job["kind"] != "message" or job["priority"] != "queue":
            raise BridgeError("The local bridge supports normal text messages only.")
        book = AddressBook({}, {"local": OpenCodeAdapter.session_id})
        target = resolve_target({}, job, addresses=book, origin_node="local")
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
                           addresses=book, origin_node="local")


def local_relay_once(box, adapter):
    # Leave pending messages untouched while service discovery or access fails.
    info = adapter.api.call("get", "/api/info")
    if (not isinstance(info, dict) or not isinstance(info.get("version"), str)
            or not info["version"].startswith("2.")):
        raise BridgeError("The local OpenCode V2 service is unavailable.")
    return relay_once(box, lambda job: deliver_local(job, adapter))


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
    worker = commands.add_parser("relay", help="Deliver queued messages; may consume model quota.")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    box = None
    try:
        adapter = local_adapter()
        if args.command == "find":
            result = adapter.find_sessions(args.query, cursor=args.cursor)
        else:
            box = LocalMailbox(state_root())
            if args.command == "send":
                result = box.enqueue(args.source, args.to, args.text)
            elif args.command == "status":
                result = box.status()
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
