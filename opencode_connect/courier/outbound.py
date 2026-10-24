"""Uploads to contacts, their recovery after a crash, and polling the receipts they
gave: the courier's outbound side, as a mixin of `Courier`."""

from opencode_connect.core import BridgeError
from opencode_connect.courier.settings import MESSAGE, RETENTION, SOCKET_TIMEOUT, TERMINAL, person
from opencode_connect.transports import HttpError, http_json


class OutboundMixin:
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
