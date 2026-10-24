"""Telling contacts this courier's new URL after `address`, and taking theirs.

The sender queues one announcement per contact when its URL changes, and sends one
per worker tick, retrying with backoff while a contact cannot be reached. The receiver
changes only that contact's card URL, and only to the address the announcement came
from, as pairing requires of a card.
"""

from opencode_connect.core import BridgeError
from opencode_connect.courier.pairing import same_host
from opencode_connect.courier.settings import SOCKET_TIMEOUT, RequestError, contact_url
from opencode_connect.transports import HttpError, http_json, short_text

ANNOUNCE_TABLE = """CREATE TABLE IF NOT EXISTS courier_announce (
    contact TEXT PRIMARY KEY, url TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending', 'unsupported', 'refused')))"""


class AddressMixin:
    def _queue_announcements_locked(self, config):
        """One pending announcement per contact, replacing any earlier ones; the caller
        holds the transaction."""
        self.db.execute("DELETE FROM courier_announce")
        for name in config["contacts"]:
            self.db.execute("INSERT INTO courier_announce(contact, url, state) VALUES(?,?,'pending')",
                            (name, config["url"]))

    def announce_once(self):
        """Tell one contact whose announcement is due this courier's URL; its name, or None.

        A courier that answers 404 predates announcements, and one that refuses is not
        asked again; either needs pairing again. Network failures are retried.
        """
        with self.lock:
            row = self.db.execute("SELECT contact, url, attempts FROM courier_announce WHERE state='pending' "
                                  "AND next_attempt<=? ORDER BY next_attempt, contact LIMIT 1",
                                  (self.clock(),)).fetchone()
            if row is None:
                return None
            name, url, attempts = row
            config = self.settings()
            card = config["contacts"].get(name)
            if card is None or url != config["url"]:
                with self.transaction():
                    self.db.execute("DELETE FROM courier_announce WHERE contact=? AND url=?", (name, url))
                return name
        state, error = None, None
        try:
            http_json(card["url"] + "/v1/address", method="POST", data={"url": url},
                      headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT)
        except HttpError as exc:
            error = exc
            state = ("pending" if exc.status >= 500 or exc.status in {408, 429}
                     else "unsupported" if exc.status == 404 else "refused")
        except BridgeError as exc:
            state, error = "pending", exc
        with self.transaction():
            if state is None:
                self.db.execute("DELETE FROM courier_announce WHERE contact=? AND url=?", (name, url))
            elif state == "pending":
                self.db.execute("UPDATE courier_announce SET attempts=?, next_attempt=?, last_error=? "
                                "WHERE contact=? AND url=?",
                                (attempts + 1, self.clock() + min(300, 2 ** min(attempts + 1, 9)),
                                 short_text(error), name, url))
            else:
                self.db.execute("UPDATE courier_announce SET state=?, last_error=? WHERE contact=? AND url=?",
                                (state, short_text(error), name, url))
            if name in self.settings()["contacts"]:
                # An HTTP answer, even a refusal, shows that the contact is reachable.
                self.note_peer(name, error=None if error is None or isinstance(error, HttpError) else error)
        return name

    def update_contact_url(self, authorization, payload, peer, *, rate_checked=False):
        """A contact's courier moved: take its new URL, which must be the address this
        request came from. Only that contact's card changes."""
        if not isinstance(payload, dict) or set(payload) != {"url"}:
            raise RequestError(400, "Invalid address update.")
        try:
            url = contact_url(payload["url"])
        except BridgeError as exc:
            raise RequestError(400, "Invalid contact URL.") from exc
        with self.transaction():
            name = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            if not same_host(url, peer):
                raise RequestError(400, f"The new URL must use the address this request came from, {peer}.")
            config = self.settings()
            card = config["contacts"].get(name)
            if card is None:
                raise RequestError(409, "There is no card for you here to update; pair again.")
            updated = card["url"] != url
            if updated:
                config["contacts"][name] = {**card, "url": url}
                self.save_settings(config)
            self.note_peer(name)
        if updated:
            self.sync_config()
        return {"person": config["person"], "updated": updated}
