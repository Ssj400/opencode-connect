#!/usr/bin/env python3
"""Opt-in, consent-gated person-to-person text courier over a private LAN or VPN."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import string
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from config import private_directory
from core import BridgeError
from transports import HttpError, http_json

MAX_BODY = 64 * 1024
MAX_TEXT = 12000
RATE = 60
HELD_LIMIT = 100
RETENTION = 7 * 24 * 60 * 60
SOCKET_TIMEOUT = 3
DRAIN_SECONDS = 1
PERSON = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MESSAGE = re.compile(r"msg_[a-f0-9]{32}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
TERMINAL = {"approved", "rejected", "expired"}


def person(value):
    if not isinstance(value, str) or not PERSON.fullmatch(value):
        raise BridgeError("Person names must be lowercase ASCII identifiers (1-64 characters).")
    return value


def clean_display(value):
    """Remove terminal controls, escape sequences, and invisible formatting controls."""
    # Remove complete ANSI CSI/OSC sequences, not just their escape introducers.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if not unicodedata.category(c).startswith("C"))


def interactive_confirmation(action):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BridgeError("This command requires an interactive terminal on stdin and stdout.")
    code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(5))
    print(f"{clean_display(action)}. Type {code} to confirm:", flush=True)
    if input().strip() != code:
        raise BridgeError("Confirmation did not match; nothing was changed.")


def listen_address(value, allow_wildcard=False):
    try:
        host, port = value.rsplit(":", 1)
        address = ipaddress.ip_address(host.strip("[]"))
        port = int(port)
        if not 0 <= port <= 65535:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise BridgeError("Listen must be an IP address and port (use [IPv6]:PORT).") from exc
    if address.is_unspecified and not allow_wildcard:
        raise BridgeError("Wildcard listening requires --allow-wildcard and explicit network authorization.")
    if address.is_multicast:
        raise BridgeError("Multicast listen addresses are not supported.")
    return str(address), port


def contact_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.port == 0
                or url.username is not None or url.password is not None
                or url.path not in {"", "/"} or url.query or url.fragment
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise BridgeError("A contact URL must be an HTTP(S) origin without credentials or a path.") from exc
    return value.rstrip("/")


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"person", "listen", "url", "tokens", "contacts"}:
        raise BridgeError("Courier configuration requires person, listen, url, tokens, and contacts.")
    person(config["person"])
    listen_address(config["listen"], allow_wildcard=True)
    contact_url(config["url"])
    if not isinstance(config["tokens"], dict) or not isinstance(config["contacts"], dict):
        raise BridgeError("Courier tokens and contacts must be mappings.")
    for name, digest in config["tokens"].items():
        person(name)
        if name == config["person"] or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BridgeError("Invalid issued token digest.")
    for name, card in config["contacts"].items():
        person(name)
        validate_card(card)
        if name == config["person"] or card["person"] != name:
            raise BridgeError("Contact card identity does not match its name.")
    return config


def validate_card(card):
    if not isinstance(card, dict) or set(card) != {"person", "url", "token"}:
        raise BridgeError("A contact card requires person, url, and token.")
    person(card["person"])
    contact_url(card["url"])
    if not isinstance(card["token"], str) or not TOKEN.fullmatch(card["token"]):
        raise BridgeError("Invalid contact card token.")
    return card


def default_config_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not base.is_absolute():
        raise BridgeError("XDG_CONFIG_HOME must be absolute.")
    return base / "opencode-connect/courier.json"


def read_config(path):
    path = Path(path)
    try:
        check_private_directory(path.parent)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise BridgeError("Courier configuration must be an owned regular file with mode 0600.")
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError) as exc:
        raise BridgeError("Could not read the private courier configuration.") from exc


def check_private_directory(path):
    """Validate an existing directory without creating or repairing anything."""
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise BridgeError("Courier state paths must be absolute and must not traverse symlinks.")
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BridgeError("Courier directories require current-user ownership and private permissions.")


def write_config(path, config):
    validate_config(config)
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise BridgeError("Courier configuration must not be a symlink.")
    if path.exists():
        read_config(path)
    fd, temporary = tempfile.mkstemp(prefix=".courier-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RequestError(BridgeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Courier:
    """Application logic over an injected local queue sharing one SQLite transaction.

    The queue exposes db, transaction(), claim(prefix), mark(), release(),
    insert_inbound(), approve_inbound(), and cancel_person(). No queue schema is
    accessed here. LocalMailbox supplies the operational adapter.
    """

    def __init__(self, queue, config, *, config_path=None, clock=time.time):
        self.queue = queue
        self.db = queue.db
        self.clock = clock
        self.config_path = Path(config_path) if config_path is not None else None
        self.lock = threading.RLock()
        validate_config(config)
        with self.transaction():
            for statement in (
                    "CREATE TABLE IF NOT EXISTS courier_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)",
                    """CREATE TABLE IF NOT EXISTS courier_inbound (
                        id TEXT PRIMARY KEY, contact TEXT NOT NULL, key TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, reply_to TEXT, created REAL NOT NULL,
                        expires REAL NOT NULL, state TEXT NOT NULL, target TEXT,
                        UNIQUE(contact, key))""",
                    """CREATE TABLE IF NOT EXISTS courier_sent (
                        id TEXT PRIMARY KEY, contact TEXT NOT NULL, source TEXT NOT NULL,
                        remote_id TEXT, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt REAL NOT NULL DEFAULT 0)""",
                    "CREATE TABLE IF NOT EXISTS courier_rate (contact TEXT PRIMARY KEY, start REAL, count INTEGER)"):
                self.db.execute(statement)
            row = self.db.execute("SELECT data FROM courier_settings WHERE id=1").fetchone()
            if row is None:
                self.db.execute("INSERT INTO courier_settings VALUES(1,?)", (json.dumps(config),))
            else:
                saved = validate_config(json.loads(row[0]))
                if any(saved[k] != config[k] for k in ("person", "listen", "url")):
                    raise BridgeError("Courier identity/listener differs from its stored configuration; review required.")
        # SQLite is authoritative for contacts after initialization. Repair a stale
        # JSON snapshot after a crash; never resurrect a revoked token from it.
        self.sync_config()

    @contextmanager
    def transaction(self):
        with self.lock, self.queue.transaction():
            yield

    def settings(self):
        return json.loads(self.db.execute("SELECT data FROM courier_settings WHERE id=1").fetchone()[0])

    def save_settings(self, config):
        self.db.execute("UPDATE courier_settings SET data=? WHERE id=1", (json.dumps(config),))

    def sync_config(self):
        if self.config_path is not None:
            with self.lock:
                write_config(self.config_path, self.settings())

    def invite(self, name):
        person(name)
        token = secrets.token_urlsafe(32)
        with self.transaction():
            config = self.settings()
            if name == config["person"]:
                raise BridgeError("Cannot invite yourself.")
            config["tokens"][name] = hashlib.sha256(token.encode()).hexdigest()
            self.save_settings(config)
        self.sync_config()
        return {"person": config["person"], "url": config["url"], "token": token}

    def trust(self, name, card):
        person(name)
        validate_card(card)
        with self.transaction():
            config = self.settings()
            if name == config["person"] or card["person"] != name:
                raise BridgeError("Contact card identity does not match the chosen person.")
            config["contacts"][name] = card
            self.save_settings(config)
        self.sync_config()

    def untrust(self, name):
        person(name)
        with self.transaction():
            config = self.settings()
            config["tokens"].pop(name, None)
            config["contacts"].pop(name, None)
            self.save_settings(config)
            # Includes approved-but-pending inbound and pending outbound rows.
            cancellable = []
            for (message_id,) in self.db.execute(
                    "SELECT id FROM courier_inbound WHERE contact=? AND state IN ('held','approved')", (name,)):
                row = self.queue.get(message_id)
                if row is not None and row["state"] in {"held", "pending"}:
                    cancellable.append(message_id)
            self.queue.cancel_person("person:" + name, outbound=True)
            for message_id in cancellable:
                self.db.execute("UPDATE courier_inbound SET state='rejected' WHERE id=?", (message_id,))
            for (message_id,) in self.db.execute("SELECT id FROM courier_sent WHERE contact=?", (name,)):
                row = self.queue.get(message_id)
                if row is not None and row["state"] == "cancelled":
                    self.db.execute("UPDATE courier_sent SET state='cancelled' WHERE id=?", (message_id,))
        self.sync_config()

    def contacts(self):
        with self.lock:
            config = self.settings()
            return [{"person": name, "receiving": name in config["tokens"], "sending": name in config["contacts"]}
                    for name in sorted(set(config["tokens"]) | set(config["contacts"]))]

    def authenticate(self, authorization):
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 160:
            raise RequestError(401, "Authentication required.")
        token = authorization[7:]
        if not TOKEN.fullmatch(token):
            raise RequestError(401, "Authentication required.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            for name, expected in self.settings()["tokens"].items():
                if hmac.compare_digest(expected, digest):
                    return name
        raise RequestError(401, "Authentication required.")

    def check_request(self, authorization):
        # Called again after body reception so revocation during a slow upload is
        # respected. The HTTP handler authenticates before reading any body bytes.
        name = self.authenticate(authorization)
        now = self.clock()
        row = self.db.execute("SELECT start,count FROM courier_rate WHERE contact=?", (name,)).fetchone()
        start, count = (now, 0) if row is None or now - row[0] >= 60 else row
        if count >= RATE:
            raise RequestError(429, "Contact rate limit exceeded.")
        self.db.execute("INSERT OR REPLACE INTO courier_rate VALUES(?,?,?)", (name, start, count + 1))
        return name

    def begin_request(self, authorization):
        with self.transaction():
            return self.check_request(authorization)

    def receive(self, authorization, payload, *, rate_checked=False):
        if (not isinstance(payload, dict) or set(payload) - {"text", "reply_to", "idempotency_key"}
                or not {"text", "idempotency_key"} <= set(payload)):
            raise RequestError(400, "Invalid message fields.")
        text, key, reply = payload["text"], payload["idempotency_key"], payload.get("reply_to")
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
                or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key)
                or reply is not None and (not isinstance(reply, str) or not MESSAGE.fullmatch(reply))):
            raise RequestError(400, "Invalid message content.")
        try:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        except UnicodeError as exc:
            raise RequestError(400, "Invalid Unicode content.") from exc
        with self.transaction():
            name = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            self.expire()
            previous = self.db.execute("SELECT id,payload_hash,state FROM courier_inbound WHERE contact=? AND key=?",
                                       (name, key)).fetchone()
            if previous:
                if previous[1] != digest:
                    raise RequestError(409, "Idempotency key already identifies different content.")
                return {"id": previous[0], "state": previous[2]}
            held = self.db.execute("SELECT count(*) FROM courier_inbound WHERE contact=? AND state='held'",
                                   (name,)).fetchone()[0]
            if held >= HELD_LIMIT:
                raise RequestError(429, "Contact held-message limit exceeded.")
            # Insertion, receipt, and deduplication commit or roll back together.
            row = self.queue.insert_inbound(source="person:" + name, text=text,
                                            remote_key=key, reply_to=reply)
            if row.get("duplicate"):
                raise BridgeError("Queue receipt exists without courier metadata; review state manually.")
            message_id = row["id"]
            if not isinstance(message_id, str) or not MESSAGE.fullmatch(message_id):
                raise BridgeError("Queue returned an invalid inbound message ID.")
            self.db.execute("INSERT INTO courier_inbound VALUES(?,?,?,?,?,?,?,'held',NULL)",
                            (message_id, name, key, digest, reply, self.clock(), self.clock() + RETENTION))
            return {"id": message_id, "state": "held"}

    def remote_status(self, authorization, message_id, *, rate_checked=False):
        with self.transaction():
            name = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            self.expire()
            row = self.db.execute("SELECT state FROM courier_inbound WHERE id=? AND contact=?",
                                  (message_id, name)).fetchone()
            if row is None:
                raise RequestError(404, "Message not found.")
            return {"id": message_id, "state": row[0]}

    def whoami(self, authorization, *, rate_checked=False):
        with self.transaction():
            if rate_checked:
                self.authenticate(authorization)
            else:
                self.check_request(authorization)
            return {"person": self.settings()["person"], "protocol": 1}

    def expire(self):
        # Caller holds the shared queue transaction.
        rows = self.db.execute("SELECT id FROM courier_inbound WHERE state='held' AND expires<=?",
                               (self.clock(),)).fetchall()
        for (message_id,) in rows:
            self.queue.reject_inbound(message_id, reason="Retention expired.")
            self.db.execute("UPDATE courier_inbound SET state='expired' WHERE id=?", (message_id,))

    def inbox(self):
        with self.transaction():
            self.expire()
            result = []
            for message_id, name, reply, created in self.db.execute(
                    "SELECT id,contact,reply_to,created FROM courier_inbound WHERE state='held' ORDER BY created,id"):
                row = self.queue.get(message_id)
                suggestion = self.db.execute(
                    "SELECT source FROM courier_sent WHERE contact=? AND remote_id=? AND state!='cancelled'",
                    (name, reply)).fetchone() if reply else None
                result.append({"id": message_id, "person": name, "text": clean_display(row["payload"]["text"]),
                               "created": created, "suggested_target": suggestion[0] if suggestion else None})
            return result

    def approve(self, message_id, target):
        if not isinstance(target, str) or not (
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}:ses_[A-Za-z0-9_-]{1,128}", target)
                or re.fullmatch(r"claude:[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", target)):
            raise BridgeError("Approval requires an explicit local session address, not a person target.")
        if target.startswith("person:"):
            raise BridgeError("Approval cannot forward to another person.")
        with self.transaction():
            self.expire()
            row = self.db.execute("SELECT state,contact FROM courier_inbound WHERE id=?", (message_id,)).fetchone()
            if row is None or row[0] != "held":
                raise BridgeError("Only a held, unexpired message can be approved.")
            if row[1] not in self.settings()["tokens"]:
                raise BridgeError("Contact is no longer authorized.")
            approved = self.queue.approve_inbound(message_id, target)
            self.db.execute("UPDATE courier_inbound SET state='approved',target=? WHERE id=?", (approved["target"], message_id))

    def reject(self, message_id):
        with self.transaction():
            row = self.db.execute("SELECT state FROM courier_inbound WHERE id=?", (message_id,)).fetchone()
            if row is None or row[0] != "held":
                raise BridgeError("Only a held message can be rejected.")
            self.queue.reject_inbound(message_id, reason="Rejected by operator.")
            self.db.execute("UPDATE courier_inbound SET state='rejected' WHERE id=?", (message_id,))

    def send_once(self):
        # The queue claim and retry metadata commit together. Requests already in
        # flight cannot be retracted by another process revoking this contact.
        with self.lock:
            with self.transaction():
                deferred = []
                while True:
                    job = self.queue.claim("person:")
                    if job is None:
                        break
                    previous = self.db.execute("SELECT next_attempt FROM courier_sent WHERE id=?", (job["id"],)).fetchone()
                    if previous and previous[0] > self.clock():
                        deferred.append(job["id"])
                        continue
                    break
                for message_id in deferred:
                    self.queue.release(message_id)
                if job is None:
                    return None
                name = person(job["target"][7:])
                if job["source"].startswith("person:"):
                    self.queue.mark(job["id"], "failed", {"reason": "Automatic person forwarding is forbidden."})
                    return job["id"]
                config = self.settings()
                card = config["contacts"].get(name)
                if card is None:
                    self.queue.mark(job["id"], "failed", {"reason": "No trusted contact card."})
                    return job["id"]
                self.db.execute("""INSERT OR IGNORE INTO courier_sent(id,contact,source,state)
                                   VALUES(?,?,?,'sending')""", (job["id"], name, job["source"]))
                self.db.execute("UPDATE courier_sent SET state='sending' WHERE id=?", (job["id"],))
            payload = {"text": job["payload"]["text"], "idempotency_key": job["id"]}
            if job["payload"].get("reply_to") is not None:
                payload["reply_to"] = job["payload"]["reply_to"]
            try:
                result = http_json(card["url"] + "/v1/messages", method="POST", data=payload,
                                   headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT)
                if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                        or not MESSAGE.fullmatch(result["id"]) or result.get("state") not in {"held", *TERMINAL}):
                    raise BridgeError("Invalid remote receipt.")
            except BridgeError as exc:
                with self.transaction():
                    if name not in self.settings()["contacts"]:
                        self.queue.mark(job["id"], "cancelled", {"reason": "Contact revoked during request."})
                        self.db.execute("UPDATE courier_sent SET state='cancelled' WHERE id=?", (job["id"],))
                    elif isinstance(exc, HttpError) and exc.status < 500 and exc.status not in {408, 429}:
                        self.queue.mark(job["id"], "failed", {"reason": str(exc)})
                        self.db.execute("UPDATE courier_sent SET state='failed' WHERE id=?", (job["id"],))
                    else:
                        attempts = self.db.execute("SELECT attempts FROM courier_sent WHERE id=?", (job["id"],)).fetchone()[0] + 1
                        delay = min(300, 2 ** min(attempts, 9))
                        self.db.execute("UPDATE courier_sent SET state='retry',attempts=?,next_attempt=? WHERE id=?",
                                        (attempts, self.clock() + delay, job["id"]))
                        self.queue.release(job["id"])
                return job["id"]
            with self.transaction():
                self.queue.mark(job["id"], "delivered", {"remote_id": result["id"], "state": result["state"]})
                self.db.execute("UPDATE courier_sent SET remote_id=?,state=?,next_attempt=? WHERE id=?",
                                (result["id"], result["state"], self.clock() + 60, job["id"]))
            return job["id"]

    def recover_outbound(self):
        """Recover only our idempotent HTTP claims, after obtaining the worker lock."""
        with self.transaction():
            for message_id, name in self.db.execute("SELECT id,contact FROM courier_sent WHERE state='sending'").fetchall():
                row = self.queue.get(message_id)
                if row is None or row["state"] != "sending" or row["target"] != "person:" + name:
                    continue
                if name not in self.settings()["contacts"]:
                    self.queue.mark(message_id, "cancelled", {"reason": "Contact revoked."})
                    self.db.execute("UPDATE courier_sent SET state='cancelled' WHERE id=?", (message_id,))
                else:
                    self.queue.release(message_id)
                    self.db.execute("UPDATE courier_sent SET state='retry',next_attempt=? WHERE id=?",
                                    (self.clock(), message_id))

    def poll_once(self):
        with self.lock:
            # At most one bounded network probe per event-loop tick, so a large
            # backlog or offline peer cannot starve the inbound HTTP listener.
            rows = self.db.execute("SELECT id,contact,remote_id FROM courier_sent WHERE state='held' AND next_attempt<=? "
                                   "ORDER BY next_attempt,id LIMIT 1",
                                   (self.clock(),)).fetchall()
            for message_id, name, remote_id in rows:
                card = self.settings()["contacts"].get(name)
                if card is None:
                    # A removed contact's receipt cannot be polled; move it back so it
                    # never stays first in line and starves every other receipt.
                    with self.transaction():
                        self.db.execute("UPDATE courier_sent SET next_attempt=? WHERE id=?",
                                        (self.clock() + 60, message_id))
                    continue
                state = "held"
                try:
                    result = http_json(card["url"] + "/v1/messages/" + remote_id,
                                       headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT)
                    if (isinstance(result, dict) and result.get("id") == remote_id
                            and result.get("state") in {"held", *TERMINAL}):
                        state = result["state"]
                except BridgeError:
                    pass
                with self.transaction():
                    self.db.execute("UPDATE courier_sent SET state=?,next_attempt=? WHERE id=?",
                                    (state, self.clock() + 60, message_id))


class CourierServer(HTTPServer):
    """A bounded, single-request-at-a-time listener; never logs request headers."""

    def __init__(self, courier, listen, *, allow_wildcard=False):
        host, port = listen_address(listen, allow_wildcard)
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.courier = courier
        super().__init__((host, port), CourierHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT)
        return connection, address

    def handle_error(self, request, client_address):
        # Do not print raw remote requests, addresses, or credentials on errors.
        pass

    def shutdown_request(self, request):
        self.drain(request)
        self.close_request(request)

    def drain(self, request):
        """Read what the client still sends, briefly, before closing.

        A rejection is sent before the body is read; closing with the body still
        arriving makes the kernel reset the connection, and a proxy such as a Windows
        port proxy then forwards the reset instead of the status. Returns the bytes read.
        """
        drained = 0
        try:
            request.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + DRAIN_SECONDS
            while drained <= MAX_BODY and (remaining := deadline - time.monotonic()) > 0:
                request.settimeout(remaining)
                chunk = request.recv(4096)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass
        return drained


class CourierHandler(BaseHTTPRequestHandler):
    server_version = "Courier/1"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def handle_expect_100(self):
        # HTTP/1.0 responses never invite an unauthenticated client to upload.
        return False

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        try:
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1:
                raise RequestError(401, "Authentication required.")
            self.server.courier.begin_request(auth[0])
            if self.headers.get("Transfer-Encoding") is not None:
                raise RequestError(400, "Transfer encoding is not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0])):
                raise RequestError(411, "A single Content-Length is required.")
            length = int(lengths[0]) if lengths else 0
            if length > MAX_BODY:
                raise RequestError(413, "Message body is too large.")
            if method == "GET" and length:
                raise RequestError(400, "GET requests cannot contain a body.")
            if method == "GET" and self.path == "/v1/whoami":
                self.reply(200, self.server.courier.whoami(auth[0], rate_checked=True))
            elif method == "GET" and re.fullmatch(r"/v1/messages/msg_[a-f0-9]{32}", self.path):
                self.reply(200, self.server.courier.remote_status(auth[0], self.path.rsplit("/", 1)[1], rate_checked=True))
            elif method == "POST" and self.path == "/v1/messages":
                if not lengths:
                    raise RequestError(411, "A single Content-Length is required.")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    raise RequestError(415, "JSON content type is required.")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise RequestError(400, "Incomplete body.")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise RequestError(400, "Invalid JSON body.") from exc
                self.reply(202, self.server.courier.receive(auth[0], payload, rate_checked=True))
            else:
                raise RequestError(404, "Route not found.")
        except RequestError as exc:
            self.reply(exc.status, {"error": str(exc)})
        except (TimeoutError, ConnectionError, BrokenPipeError):
            self.close_connection = True
        except (BridgeError, sqlite3.Error):
            self.reply(503, {"error": "Courier storage unavailable."})


def real_queue(config_path):
    from local_bridge import LocalMailbox, state_root
    return LocalMailbox(state_root())


def local_queue_root():
    """Read-only counterpart of local_bridge.state_root; never creates its path."""
    base = Path(os.environ.get("OPENCODE_CONNECT_LOCAL_STATE_HOME",
                               os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))))
    if not base.is_absolute():
        raise BridgeError("The local bridge state home must be absolute.")
    return base / "opencode-connect/local"


def doctor(config_path, *, root=None, network=True, clock=time.time):
    """Read configuration/state and bounded identity probes; never send or approve text."""
    findings = []
    try:
        config = read_config(config_path)
    except (BridgeError, OSError):
        return [{"status": "FAIL", "area": "Configuration", "detail": "Missing or insecure private configuration."}]
    findings.append({"status": "OK", "area": "Configuration", "detail": "Owned private directory and 0600 file."})
    root = local_queue_root() if root is None else Path(root)
    database = root / ".state/mail.sqlite3"
    if not database.exists() and not database.is_symlink():
        findings.append({"status": "WARN", "area": "State", "detail": "Courier queue has not been initialized."})
    else:
        try:
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
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=SOCKET_TIMEOUT)
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
            from collab import queue_summary
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
    host, port = listen_address(config["listen"], allow_wildcard=True)
    if ipaddress.ip_address(host).is_unspecified or port == 0:
        findings.append({"status": "WARN", "area": "Listener", "detail": "A specific, nonzero listen endpoint is needed for a reachability probe."})
    else:
        try:
            with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT):
                pass
            findings.append({"status": "OK", "area": "Listener", "detail": "TCP reachable; this alone does not establish process identity."})
        except OSError:
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


def main(argv=None, *, queue_factory=real_queue):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a private courier identity interactively")
    init.add_argument("--person", required=True)
    init.add_argument("--listen", required=True)
    init.add_argument("--url", required=True, help="Reachable HTTP(S) origin included in invitation cards")
    run = sub.add_parser("run", help="Serve inbound messages and process outbound person messages")
    run.add_argument("--allow-wildcard", action="store_true")
    diagnostic = sub.add_parser("doctor", help="Read-only state checks and bounded authenticated identity probes")
    diagnostic.add_argument("--offline", action="store_true", help="Skip all network probes")
    invite = sub.add_parser("invite", help="Issue a token and display its card once")
    invite.add_argument("person")
    trust = sub.add_parser("trust", help="Accept an independently received contact card")
    trust.add_argument("person")
    trust.add_argument("--card", required=True, help="JSON card, or '-' to paste privately in this terminal")
    untrust = sub.add_parser("untrust", help="Revoke contact and cancel unsent messages")
    untrust.add_argument("person")
    sub.add_parser("contacts", help="List contact direction permissions, without tokens")
    inbox = sub.add_parser("inbox", help="Show sanitized held messages without approving them")
    inbox.add_argument("--watch", action="store_true")
    approve = sub.add_parser("approve", help="Release one held message to an explicit local session")
    approve.add_argument("id")
    approve.add_argument("--to", required=True)
    reject = sub.add_parser("reject", help="Reject one held message")
    reject.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            findings = doctor(args.config, network=not args.offline)
            print(json.dumps(findings, ensure_ascii=False))
            return 1 if any(row["status"] == "FAIL" for row in findings) else 0
        if args.command in {"init", "invite", "trust", "untrust", "approve", "reject"}:
            detail = args.command
            if args.command == "approve":
                detail += f" {args.id} to {args.to}"
            elif args.command == "reject":
                detail += f" {args.id}"
            elif hasattr(args, "person"):
                detail += " " + args.person
            interactive_confirmation("Confirm " + detail)
        if args.command == "init":
            if args.config.exists() or args.config.is_symlink():
                raise BridgeError("Courier configuration already exists; it was not overwritten.")
            config = {"person": args.person, "listen": args.listen, "url": args.url, "tokens": {}, "contacts": {}}
            write_config(args.config, config)
            print("Private courier configuration created; no listener was started.")
            return 0
        config = read_config(args.config)
        queue = queue_factory(args.config)
        try:
            courier = Courier(queue, config, config_path=args.config)
            if args.command == "invite":
                print(json.dumps(courier.invite(args.person)))
            elif args.command == "trust":
                if args.card == "-":
                    import getpass
                    raw = getpass.getpass("Paste contact card (hidden): ")
                else:
                    raw = args.card
                try:
                    card = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise BridgeError("Invalid contact card JSON.") from exc
                courier.trust(args.person, card)
                print("Contact trusted.")
            elif args.command == "untrust":
                courier.untrust(args.person)
                print("Contact revoked; unsent messages cancelled.")
            elif args.command == "contacts":
                print(json.dumps(courier.contacts()))
            elif args.command == "inbox":
                while True:
                    for row in courier.inbox():
                        print(json.dumps(row, ensure_ascii=False), flush=True)
                    if not args.watch:
                        break
                    time.sleep(2)
            elif args.command == "approve":
                courier.approve(args.id, args.to)
                print("Message approved for local delivery; not proof of session reception.")
            elif args.command == "reject":
                courier.reject(args.id)
                print("Message rejected.")
            elif args.command == "run":
                run_courier(courier, config["listen"], allow_wildcard=args.allow_wildcard)
        finally:
            queue.db.close()
        return 0
    except (BridgeError, OSError, sqlite3.Error) as exc:
        # Avoid disclosing config content or credentials through parser tracebacks.
        message = str(exc) if isinstance(exc, BridgeError) else "Courier storage or listener unavailable."
        print(clean_display(message), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


def run_courier(courier, listen, *, allow_wildcard=False):
    if listen_address(listen, allow_wildcard)[1] == 0:
        raise BridgeError("The operational listener requires a fixed nonzero port.")
    # One coordinator per queue; no recovery of unrelated sending/uncertain rows.
    with courier.lock:
        database = courier.db.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        raise BridgeError("The worker requires a persistent private queue.")
    lock_path = Path(database).parent / "courier.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError("A courier already owns this queue.") from exc
        with CourierServer(courier, listen, allow_wildcard=allow_wildcard) as server:
            courier.recover_outbound()
            server.timeout = 0.2
            print("Courier active; all inbound messages require local approval.", flush=True)
            while True:
                server.handle_request()
                courier.send_once()
                courier.poll_once()
                with courier.transaction():
                    courier.expire()
    finally:
        os.close(fd)


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
