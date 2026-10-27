"""A team member's side: the owner it joined may add and remove teammates as contacts.

Joining a team (`courier.py join LINK`) lets its owner introduce teammates: the owner
generates the tokens each pair uses and delivers them, and may later remove the
contacts it introduced. Nothing more: this courier's own contacts, the names it
unpaired by hand, and its routes stay out of the owner's reach, and `team leave`, or
unpairing the owner, ends the membership.
"""

import ipaddress
from urllib.parse import urlsplit

from opencode_connect.core import BridgeError
from opencode_connect.courier.host import LAN_NETWORKS
from opencode_connect.courier.pairing import DIGEST, VPN_NETWORKS
from opencode_connect.courier.settings import RequestError, contact_url, person, validate_card

# An introduced teammate's courier must be on a VPN or LAN, named by its IP.
INTRODUCED_NETWORKS = VPN_NETWORKS + LAN_NETWORKS

MEMBER_TABLES = (
    """CREATE TABLE IF NOT EXISTS courier_membership (
        id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, joined REAL NOT NULL)""",
    # Contacts the owner introduced; `moved` keeps a URL a teammate announced before its card arrived.
    "CREATE TABLE IF NOT EXISTS courier_introduced (name TEXT PRIMARY KEY, moved TEXT)",
    # Names unpaired by hand, which an owner cannot introduce again.
    "CREATE TABLE IF NOT EXISTS courier_blocked (name TEXT PRIMARY KEY, since REAL NOT NULL)",
)


def introduced_url(url):
    """An introduced card's URL: an IP literal on a VPN or LAN, never a host name or a
    public address, so that an owner cannot point a member anywhere else."""
    url = contact_url(url)
    try:
        address = ipaddress.ip_address(urlsplit(url).hostname or "")
    except ValueError as exc:
        raise BridgeError("An introduced courier must be named by its IP address.") from exc
    if not any(address in network for network in INTRODUCED_NETWORKS):
        raise BridgeError("An introduced courier must be on a VPN or LAN address.")
    return url


class TeammateMixin:
    """For Courier: membership in one owner's team, and what that owner may change."""

    def owner(self):
        """The owner of the team this courier joined, or None."""
        row = self.db.execute("SELECT owner FROM courier_membership WHERE id=1").fetchone()
        return None if row is None else row[0]

    def introduced(self):
        return {name for (name,) in self.db.execute("SELECT name FROM courier_introduced")}

    def _join_team_locked(self, owner):
        current = self.owner()
        if current is not None and current != owner:
            raise BridgeError(f"This courier is already in {current}'s team; "
                              "`python3 bin/courier.py team leave` first.")
        if self.members():
            raise BridgeError("This courier has its own team, and a courier is in one team at a time.")
        self.db.execute("INSERT OR REPLACE INTO courier_membership VALUES(1,?,?)", (owner, self.clock()))

    def _leave_team_locked(self):
        """End the membership. The contacts the owner introduced stay, as this courier's own."""
        owner = self.owner()
        self.db.execute("DELETE FROM courier_membership")
        self.db.execute("DELETE FROM courier_introduced")
        return owner

    def leave_team(self):
        with self.transaction():
            owner = self._leave_team_locked()
        if owner is None:
            raise BridgeError("This courier is not in a team.")
        return owner

    def _own_contact_locked(self, name):
        """`name` was paired by hand: no longer introduced, and no longer blocked."""
        self.db.execute("DELETE FROM courier_introduced WHERE name=?", (name,))
        self.db.execute("DELETE FROM courier_blocked WHERE name=?", (name,))

    def _block_locked(self, name):
        self.db.execute("INSERT OR REPLACE INTO courier_blocked VALUES(?,?)", (name, self.clock()))

    def _introduced_moved_locked(self, name, url):
        """A teammate announced a new URL before its card arrived: keep it for the card.
        True if `name` is such a teammate."""
        return bool(self.db.execute("UPDATE courier_introduced SET moved=? WHERE name=?", (url, name)).rowcount)

    def team_introduce(self, authorization, payload, *, rate_checked=False):
        """The owner introduces a teammate: accept its token's digest, and take its card
        when the owner sends one. Only the owner may, and never over this courier's own
        contacts or a name it unpaired by hand."""
        if (not isinstance(payload, dict) or not {"person", "accept", "card"} <= set(payload)
                or set(payload) - {"person", "accept", "card", "told"}):
            raise RequestError(400, "Invalid introduction.")
        name, accept, card, told = payload["person"], payload["accept"], payload["card"], payload.get("told")
        try:
            person(name)
            if not isinstance(accept, str) or not DIGEST.fullmatch(accept):
                raise BridgeError("Invalid token digest.")
            if card is not None:
                validate_card(card)
                if card["person"] != name:
                    raise BridgeError("The card is not that teammate's.")
                introduced_url(card["url"])
            if told is not None:
                contact_url(told)
        except BridgeError as exc:
            raise RequestError(400, f"Invalid introduction: {exc}") from exc
        with self.transaction():
            owner = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            if owner != self.owner():
                raise RequestError(410, "This courier is not in your team.")
            config = self.settings()
            if name in (config["person"], owner):
                raise RequestError(400, "An introduction names another teammate.")
            if self.db.execute("SELECT 1 FROM courier_blocked WHERE name=?", (name,)).fetchone():
                raise RequestError(409, f"{config['person']} unpaired {name} by hand.")
            introduced = self.db.execute("SELECT moved FROM courier_introduced WHERE name=?", (name,)).fetchone()
            if introduced is None and (name in config["tokens"] or name in config["contacts"]):
                raise RequestError(409, f"{name} is already {config['person']}'s own contact.")
            if self.db.execute("SELECT 1 FROM courier_offer WHERE contact=? AND state='provisional'",
                               (name,)).fetchone():
                raise RequestError(503, f"A pairing with {name} is in progress; try again later.")
            if card is not None and introduced is not None and introduced[0]:
                card = {**card, "url": introduced[0]}  # Its own announcement is newer than the owner's view.
            changed = config["tokens"].get(name) != accept or card is not None and config["contacts"].get(name) != card
            config["tokens"][name] = accept
            if card is not None:
                config["contacts"][name] = card
                self.db.execute("INSERT OR REPLACE INTO courier_introduced(name, moved) VALUES(?, NULL)", (name,))
            else:
                self.db.execute("INSERT OR IGNORE INTO courier_introduced(name) VALUES(?)", (name,))
            self.save_settings(config)
            if card is not None and told is not None and told != config["url"]:
                # The teammate was given an older URL for this courier: tell it the current one.
                self.db.execute("INSERT OR REPLACE INTO courier_announce(contact, url, state) VALUES(?,?,'pending')",
                                (name, config["url"]))
            self.note_peer(owner)
        if changed:
            self.sync_config()
        return {"person": config["person"], "changed": changed}

    def team_remove(self, authorization, payload, *, rate_checked=False):
        """The owner removes a teammate it introduced; any other contact stays."""
        if not isinstance(payload, dict) or set(payload) != {"person"}:
            raise RequestError(400, "Invalid removal.")
        try:
            name = person(payload["person"])
        except BridgeError as exc:
            raise RequestError(400, "Invalid removal.") from exc
        with self.transaction():
            owner = self.authenticate(authorization) if rate_checked else self.check_request(authorization)
            if owner != self.owner():
                raise RequestError(410, "This courier is not in your team.")
            removed = name in self.introduced()
            if removed:
                self._untrust_locked(name)
            config = self.settings()
            self.note_peer(owner)
        if removed:
            self.sync_config()
        return {"person": config["person"], "changed": removed}
