"""Courier application logic: contacts, held inbound messages, and outbound uploads."""

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time

from opencode_connect.core import BridgeError
from opencode_connect.courier.pairing import PairingMixin
from opencode_connect.courier.settings import (SOCKET_TIMEOUT, TOKEN, RequestError, clean_display, person, validate_card,
                                               validate_config, write_config)
from opencode_connect.transports import HttpError, http_json, short_text

MAX_TEXT = 12000
RATE = 60
HELD_LIMIT = 100
RETENTION = 7 * 24 * 60 * 60
MESSAGE = re.compile(r"msg_[a-f0-9]{32}\Z")
TERMINAL = {"approved", "rejected", "expired"}
# Messages a `direct` contact may deliver per hour without approval: each one can
# start an agent and use model quota, so the rest wait for a person.
DIRECT_PER_HOUR = 20
MODES = ("ask", "direct")


def local_target(target):
    """A local session an approval may name: node:ses_ID or claude:UUID, never a person."""
    if not isinstance(target, str) or not (
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}:ses_[A-Za-z0-9_-]{1,128}", target)
            or re.fullmatch(r"claude:[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", target)):
        raise BridgeError("Approval requires an explicit local session address, not a person target.")
    if target.startswith("person:"):
        raise BridgeError("Approval cannot forward to another person.")
    return target


class Courier(PairingMixin):
    """Application logic over an injected local queue sharing one SQLite transaction.

    The queue exposes db, transaction(), claim(prefix), mark(), release(),
    insert_inbound(), approve_inbound(), and cancel_person(). No queue schema is
    accessed here. LocalMailbox supplies the operational adapter.
    """

    def __init__(self, queue, config, *, config_path=None, clock=time.time, notify=None, readdress=None):
        self.queue = queue
        self.db = queue.db
        self.clock = clock
        # Called with a contact's name, outside any transaction, when its message is held.
        self.notify = notify or (lambda name: None)
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
                    "CREATE TABLE IF NOT EXISTS courier_rate (contact TEXT PRIMARY KEY, start REAL, count INTEGER)",
                    """CREATE TABLE IF NOT EXISTS courier_peers (
                        contact TEXT PRIMARY KEY, last_seen REAL, last_error TEXT, last_error_at REAL)""",
                    """CREATE TABLE IF NOT EXISTS courier_offer (
                        id INTEGER PRIMARY KEY CHECK(id=1), key TEXT NOT NULL, expires REAL NOT NULL,
                        name TEXT, replacing INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('open', 'provisional', 'confirmed')),
                        nonce TEXT, contact TEXT, request TEXT, response TEXT, previous TEXT)""",
                    """CREATE TABLE IF NOT EXISTS courier_routes (
                        contact TEXT PRIMARY KEY, target TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK(mode IN ('ask', 'direct')))"""):
                self.db.execute(statement)
            row = self.db.execute("SELECT data FROM courier_settings WHERE id=1").fetchone()
            if row is None:
                self.db.execute("INSERT INTO courier_settings VALUES(1,?)", (json.dumps({**config, **(readdress or {})}),))
            elif readdress is not None:
                # `address`: the database changes first, then the snapshot below. Either
                # side may already hold the new address after an interrupted change.
                saved = validate_config(json.loads(row[0]))
                if saved["person"] != config["person"]:
                    raise BridgeError("Courier identity differs from its stored configuration; review required.")
                self.save_settings(validate_config({**saved, **readdress}))
            else:
                saved = validate_config(json.loads(row[0]))
                if any(saved[k] != config[k] for k in ("person", "listen", "url")):
                    raise BridgeError("Courier identity/listener differs from its stored configuration; review "
                                      "required. If an `address` change was interrupted, run it again.")
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
            self._untrust_locked(name)
        self.sync_config()

    def _untrust_locked(self, name):
        """Revoke both directions and cancel what is unsent; the caller holds the transaction."""
        config = self.settings()
        config["tokens"].pop(name, None)
        config["contacts"].pop(name, None)
        self.save_settings(config)
        self.db.execute("DELETE FROM courier_routes WHERE contact=?", (name,))
        # A pairing in progress with them ends too: its lapse would restore them.
        self.db.execute("DELETE FROM courier_offer WHERE contact=?", (name,))
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

    def contacts(self):
        with self.lock:
            config = self.settings()
            return [{"person": name, "receiving": name in config["tokens"], "sending": name in config["contacts"],
                     "route": self.route(name)}
                    for name in sorted(set(config["tokens"]) | set(config["contacts"]))]

    def set_route(self, name, target, mode="ask"):
        """The local session a contact's messages go to: proposed for approval (`ask`),
        or approved on arrival (`direct`), within DIRECT_PER_HOUR."""
        person(name)
        local_target(target)
        if mode not in MODES:
            raise BridgeError("A route's mode is ask or direct.")
        with self.transaction():
            if name not in self.settings()["tokens"]:
                raise BridgeError("Routes apply to messages a contact sends; invite them first.")
            self.db.execute("INSERT OR REPLACE INTO courier_routes VALUES(?,?,?)", (name, target, mode))

    def route(self, name):
        row = self.db.execute("SELECT target, mode FROM courier_routes WHERE contact=?", (name,)).fetchone()
        return None if row is None else {"target": row[0], "mode": row[1]}

    def destination(self, name, reply):
        """Where a contact's message goes: to the session that wrote the message it
        answers, else to the contact's route, else nowhere until a person chooses."""
        if reply:
            row = self.db.execute("SELECT source FROM courier_sent WHERE contact=? AND remote_id=? AND state!='cancelled'",
                                  (name, reply)).fetchone()
            if row:
                return row[0]
        route = self.route(name)
        return route["target"] if route else None

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
            self.note_peer(name)
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
            route = self.route(name)
            state = self.deliver_directly(message_id, name, reply) if route and route["mode"] == "direct" else "held"
        if state == "held":
            self.notify(name)
        return {"id": message_id, "state": state}

    def deliver_directly(self, message_id, name, reply):
        """Approve a new message from a `direct` contact in the caller's transaction.

        Above DIRECT_PER_HOUR, or when the approval fails, such as for a route to a
        session this queue does not serve, the message stays held for a person;
        the sender is never refused because of it.
        """
        recent = self.db.execute("SELECT count(*) FROM courier_inbound WHERE contact=? AND created>?",
                                 (name, self.clock() - 3600)).fetchone()[0]
        if recent > DIRECT_PER_HOUR:  # This message included.
            return "held"
        self.db.execute("SAVEPOINT direct")
        try:
            self._approve_locked(message_id, self.destination(name, reply))
        except BridgeError:
            # Undo only this approval, including any part the queue wrote.
            self.db.execute("ROLLBACK TO direct")
            self.db.execute("RELEASE direct")
            return "held"
        self.db.execute("RELEASE direct")
        return "approved"

    def remote_status(self, authorization, message_id, *, rate_checked=False):
        with self.transaction():
            name = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            self.note_peer(name)
            self.expire()
            row = self.db.execute("SELECT state FROM courier_inbound WHERE id=? AND contact=?",
                                  (message_id, name)).fetchone()
            if row is None:
                raise RequestError(404, "Message not found.")
            return {"id": message_id, "state": row[0]}

    def whoami(self, authorization, *, rate_checked=False):
        with self.transaction():
            name = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            self.note_peer(name)
            return {"person": self.settings()["person"], "protocol": 1}

    def note_peer(self, name, error=None):
        """Record when a contact last answered, or why talking to it last failed.

        The caller holds a transaction. Only the short reason is kept, never text or tokens.
        """
        if error is None:
            self.db.execute("INSERT INTO courier_peers(contact, last_seen) VALUES(?,?) "
                            "ON CONFLICT(contact) DO UPDATE SET last_seen=excluded.last_seen", (name, self.clock()))
        else:
            self.db.execute("INSERT INTO courier_peers(contact, last_error, last_error_at) VALUES(?,?,?) "
                            "ON CONFLICT(contact) DO UPDATE SET last_error=excluded.last_error, "
                            "last_error_at=excluded.last_error_at", (name, short_text(error), self.clock()))

    def expire(self):
        # Caller holds the shared queue transaction.
        rows = self.db.execute("SELECT id FROM courier_inbound WHERE state='held' AND expires<=?",
                               (self.clock(),)).fetchall()
        for (message_id,) in rows:
            self.queue.reject_inbound(message_id, reason="Retention expired.")
            self.db.execute("UPDATE courier_inbound SET state='expired' WHERE id=?", (message_id,))

    def tidy(self):
        """Per-tick upkeep: expire held messages, and a pairing offer that lapsed."""
        with self.transaction():
            self.expire()
            revoked = self._expire_offer_locked()
        if revoked:
            self.sync_config()

    def inbox(self):
        with self.transaction():
            self.expire()
            result = []
            for message_id, name, reply, created in self.db.execute(
                    "SELECT id,contact,reply_to,created FROM courier_inbound WHERE state='held' ORDER BY created,id"):
                row = self.queue.get(message_id)
                result.append({"id": message_id, "person": name, "text": clean_display(row["payload"]["text"]),
                               "created": created, "suggested_target": self.destination(name, reply)})
            return result

    def approve(self, message_id, target):
        local_target(target)
        with self.transaction():
            self.expire()
            self._approve_locked(message_id, target)

    def approve_all(self, plan):
        """Approve exactly the (ID, target) pairs a person confirmed, each on its own.

        A message that changed since it was shown, such as one that expired, is
        skipped with its reason instead of stopping the others.
        """
        results = []
        for message_id, target in plan:
            try:
                self.approve(message_id, target)
                results.append({"id": message_id, "state": "approved", "target": target})
            except BridgeError as exc:
                results.append({"id": message_id, "state": "skipped", "reason": str(exc)})
        return results

    def _approve_locked(self, message_id, target):
        """Release a held message to a local session. The caller holds the transaction,
        which is not reentrant: receive() approves a `direct` contact's message in its own."""
        local_target(target)
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
            # Claiming and releasing uploads that still wait for their retry would write
            # on every tick: read first, and claim only when something is due.
            waiting = self.db.execute("SELECT count(*) FROM courier_sent WHERE state='retry' AND next_attempt>?",
                                      (self.clock(),)).fetchone()[0]
            if self.queue.pending_count("person:") <= waiting:
                return None
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
                        return job["id"]
                    self.note_peer(name, error=exc)
                    created = job.get("created")
                    if isinstance(exc, HttpError) and exc.status < 500 and exc.status not in {408, 429}:
                        self.queue.mark(job["id"], "failed", {"reason": str(exc)})
                        self.db.execute("UPDATE courier_sent SET state='failed' WHERE id=?", (job["id"],))
                    elif created is not None and self.clock() - created > RETENTION:
                        # Kept trying as long as an inbound message waits for approval.
                        self.queue.mark(job["id"], "failed",
                                        {"reason": f"Unreachable for {RETENTION // 86400} days: {exc}"})
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
                self.note_peer(name)
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
                state, error = "held", None
                try:
                    result = http_json(card["url"] + "/v1/messages/" + remote_id,
                                       headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT)
                    if (isinstance(result, dict) and result.get("id") == remote_id
                            and result.get("state") in {"held", *TERMINAL}):
                        state = result["state"]
                except BridgeError as exc:
                    error = exc
                with self.transaction():
                    self.db.execute("UPDATE courier_sent SET state=?,next_attempt=? WHERE id=?",
                                    (state, self.clock() + 60, message_id))
                    self.note_peer(name, error=error)
