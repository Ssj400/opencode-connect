"""A team's owner: one link admits a machine, and the owner introduces it to each member.

For each new pair, the owner generates both tokens and delivers them in three steps,
so that nobody holds a card the other side does not accept yet:

1. to the newcomer: accept the member's token, with no card;
2. to the member: accept the newcomer's token, and take the newcomer's card;
3. to the newcomer: take the member's card, and the URL the member was given for it.

Each step can be repeated harmlessly. Network failures are retried with backoff; an
introduction that cannot finish is undone where a step was already taken. Since the
owner knows both tokens, it could write to a member as another member: each member
accepted that when it joined.
"""

import secrets

from opencode_connect.core import BridgeError
from opencode_connect.courier.pairing import digest
from opencode_connect.courier.settings import SOCKET_TIMEOUT, person
from opencode_connect.transports import HttpError, http_json, short_text

INTRO_SECONDS = 30 * 24 * 60 * 60

OWNER_TABLES = (
    "CREATE TABLE IF NOT EXISTS courier_members (name TEXT PRIMARY KEY, joined REAL NOT NULL)",
    # One row per pair of members, kept once done. `generation` changes whenever the
    # row is replaced, so a step's answer applies only to the introduction it was for.
    """CREATE TABLE IF NOT EXISTS courier_intro (
        low TEXT NOT NULL, high TEXT NOT NULL, newcomer TEXT NOT NULL, generation TEXT NOT NULL,
        step INTEGER NOT NULL CHECK(step IN (1, 2, 3)),
        state TEXT NOT NULL CHECK(state IN ('pending', 'done', 'skipped', 'conflict', 'unsupported', 'expired')),
        newcomer_token TEXT, member_token TEXT, told TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, created REAL NOT NULL,
        PRIMARY KEY (low, high))""",
    """CREATE TABLE IF NOT EXISTS courier_team_remove (
        target TEXT NOT NULL, person TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, PRIMARY KEY (target, person))""",
)


def retried(error):
    """Whether a failed request is tried again later rather than taken as an answer."""
    return not isinstance(error, HttpError) or error.status >= 500 or error.status in {408, 429}


def backoff(attempts):
    return min(300, 2 ** min(attempts, 9))


class TeamMixin:
    """For Courier: the team it owns, and the introductions and removals it sends."""

    def members(self):
        return [name for (name,) in self.db.execute("SELECT name FROM courier_members ORDER BY name")]

    def introductions(self, name):
        """How `name`'s introduction to each other member stands: {member: state}."""
        with self.lock:
            return {high if low == name else low: state for low, high, state in self.db.execute(
                "SELECT low, high, state FROM courier_intro WHERE low=? OR high=?", (name, name))}

    def team_invite(self, name=None, *, replace=False):
        """Open a team offer: whoever joins with its code becomes a member. Returns the code."""
        with self.lock:
            owner = self.owner()
        if owner is not None:
            raise BridgeError(f"This courier is in {owner}'s team, so it cannot have its own.")
        return self.offer(name, replace=replace, team=True)

    def _team_answer_locked(self, name):
        """The members a newcomer will be introduced to, named in the offer's answer."""
        return [member for member in self.members() if member != name]

    def _team_member_joined_locked(self, name):
        """A newcomer confirmed its pairing: it becomes a member, and its introductions
        are queued in the same transaction."""
        others = self._team_answer_locked(name)
        self.db.execute("INSERT OR REPLACE INTO courier_members VALUES(?,?)", (name, self.clock()))
        for member in others:
            self._introduce_locked(name, member)

    def _introduce_locked(self, newcomer, member):
        """A fresh introduction of the pair, replacing any earlier one and its undoing."""
        low, high = sorted((newcomer, member))
        self.db.execute("DELETE FROM courier_team_remove WHERE target IN (?,?) AND person IN (?,?)",
                        (low, high, low, high))
        self.db.execute("INSERT OR REPLACE INTO courier_intro(low, high, newcomer, generation, step, state, "
                        "newcomer_token, member_token, created) VALUES(?,?,?,?,1,'pending',?,?,?)",
                        (low, high, newcomer, secrets.token_hex(8), secrets.token_urlsafe(32),
                         secrets.token_urlsafe(32), self.clock()))

    def introduce_again(self, name):
        """Introduce a member again to each member it is not connected with, after a
        conflict, an expiry, or a courier update. Pairs already done stay as they are."""
        person(name)
        with self.transaction():
            members = self.members()
            if name not in members:
                raise BridgeError(f"{name} is not in your team.")
            again = []
            for member in members:
                if member == name:
                    continue
                row = self.db.execute("SELECT state FROM courier_intro WHERE low=? AND high=?",
                                      tuple(sorted((name, member)))).fetchone()
                if row is None or row[0] not in {"pending", "done"}:
                    self._introduce_locked(name, member)
                    again.append(member)
        return again

    def team_remove_member(self, name):
        """Remove a member: the other members drop it, and it drops them."""
        person(name)
        with self.transaction():
            if not self._team_forget_locked(name):
                raise BridgeError(f"{name} is not in your team.")

    def _team_forget_locked(self, name):
        """Remove `name` from the team, if a member; also when this courier unpairs it.
        True if it was one."""
        members = self.members()
        if name not in members:
            return False
        self._member_gone_locked(name)
        for other in members:
            if other != name:
                self._queue_removal_locked(other, name)
                self._queue_removal_locked(name, other)  # Dropped if `name` is no longer a contact.
        return True

    def _member_gone_locked(self, name):
        """`name` is no longer a member, as when it left or unpaired this courier. Its
        unfinished introductions are undone on the other side; finished ones stay."""
        self.db.execute("DELETE FROM courier_members WHERE name=?", (name,))
        for low, high, newcomer, step in self.db.execute(
                "SELECT low, high, newcomer, step FROM courier_intro WHERE (low=? OR high=?) AND state='pending'",
                (name, name)).fetchall():
            member = high if newcomer == low else low
            if newcomer != name and step >= 2:
                self._queue_removal_locked(newcomer, member)
            if member != name and step == 3:
                self._queue_removal_locked(member, newcomer)
        self.db.execute("DELETE FROM courier_intro WHERE low=? OR high=?", (name, name))
        self.db.execute("DELETE FROM courier_team_remove WHERE target=?", (name,))

    def _queue_removal_locked(self, target, name):
        self.db.execute("INSERT OR REPLACE INTO courier_team_remove(target, person) VALUES(?,?)", (target, name))

    def _intro_ends_locked(self, low, high, step, state, reason):
        """The introduction stops: undo the steps already taken, and keep why."""
        newcomer = self.db.execute("SELECT newcomer FROM courier_intro WHERE low=? AND high=?",
                                   (low, high)).fetchone()[0]
        member = high if newcomer == low else low
        if step >= 2:
            self._queue_removal_locked(newcomer, member)  # Step 1 was taken at the newcomer.
        if step == 3:
            self._queue_removal_locked(member, newcomer)  # And step 2 at the member.
        self.db.execute("UPDATE courier_intro SET state=?, last_error=?, newcomer_token=NULL, member_token=NULL, "
                        "told=NULL WHERE low=? AND high=?", (state, short_text(reason), low, high))

    def introduce_once(self):
        """Take one due introduction a step further. The newcomer's name, or None."""
        with self.lock:
            row = self.db.execute(
                "SELECT low, high, newcomer, generation, step, newcomer_token, member_token, told, attempts, created "
                "FROM courier_intro WHERE state='pending' AND next_attempt<=? ORDER BY next_attempt, low, high LIMIT 1",
                (self.clock(),)).fetchone()
            if row is None:
                return None
            low, high, newcomer, generation, step, newcomer_token, member_token, told, attempts, created = row
            member = high if newcomer == low else low
            contacts = self.settings()["contacts"]
            ended = ("expired", "Not finished within 30 days.") if self.clock() - created > INTRO_SECONDS else \
                None if newcomer in contacts and member in contacts else ("conflict", "No longer a contact here.")
            if ended:
                with self.transaction():
                    self._intro_ends_locked(low, high, step, *ended)
                return newcomer
            target = member if step == 2 else newcomer
            if step == 1:
                body = {"person": member, "accept": digest(newcomer_token), "card": None}
            elif step == 2:
                told = contacts[newcomer]["url"]
                body = {"person": newcomer, "accept": digest(member_token),
                        "card": {"person": newcomer, "url": told, "token": newcomer_token}}
            else:
                body = {"person": member, "accept": digest(newcomer_token), "told": told,
                        "card": {"person": member, "url": contacts[member]["url"], "token": member_token}}
            card = contacts[target]
        error = None
        try:
            http_json(card["url"] + "/v1/team/introduce", method="POST", data=body,
                      headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT)
        except BridgeError as exc:
            error = exc
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM courier_intro WHERE low=? AND high=? AND generation=? AND step=? "
                                   "AND state='pending'", (low, high, generation, step)).fetchone():
                return newcomer  # Replaced or removed meanwhile: this answer no longer applies.
            if error is None and step == 3:
                self.db.execute("UPDATE courier_intro SET state='done', newcomer_token=NULL, member_token=NULL, "
                                "told=NULL, attempts=0, last_error=NULL WHERE low=? AND high=?", (low, high))
            elif error is None:
                self.db.execute("UPDATE courier_intro SET step=?, told=?, attempts=0, next_attempt=?, last_error=NULL "
                                "WHERE low=? AND high=?", (step + 1, told, self.clock(), low, high))
            elif retried(error):
                self.db.execute("UPDATE courier_intro SET attempts=?, next_attempt=?, last_error=? WHERE low=? AND high=?",
                                (attempts + 1, self.clock() + backoff(attempts + 1), short_text(error), low, high))
            elif error.status in {401, 410}:
                self._member_gone_locked(target)  # It left the team, or unpaired this courier.
            else:
                state = ("skipped" if step == 1 and error.status == 409 else
                         "unsupported" if error.status == 404 else "conflict")
                self._intro_ends_locked(low, high, step, state, error)
            if target in self.settings()["contacts"]:
                # An HTTP answer, even a refusal, shows that the contact is reachable.
                self.note_peer(target, error=error if error is not None and not isinstance(error, HttpError) else None)
        return newcomer

    def remove_once(self):
        """Ask one member to drop a contact this team introduced. The member's name, or None."""
        with self.lock:
            row = self.db.execute("SELECT target, person, attempts FROM courier_team_remove WHERE next_attempt<=? "
                                  "ORDER BY next_attempt, target, person LIMIT 1", (self.clock(),)).fetchone()
            if row is None:
                return None
            target, name, attempts = row
            card = self.settings()["contacts"].get(target)
            if card is None:
                with self.transaction():
                    self.db.execute("DELETE FROM courier_team_remove WHERE target=? AND person=?", (target, name))
                return target
        error = None
        try:
            http_json(card["url"] + "/v1/team/remove", method="POST", data={"person": name},
                      headers={"Authorization": "Bearer " + card["token"]}, timeout=SOCKET_TIMEOUT)
        except BridgeError as exc:
            error = exc
        with self.transaction():
            if retried(error) and error is not None:
                self.db.execute("UPDATE courier_team_remove SET attempts=?, next_attempt=?, last_error=? "
                                "WHERE target=? AND person=?",
                                (attempts + 1, self.clock() + backoff(attempts + 1), short_text(error), target, name))
            else:
                self.db.execute("DELETE FROM courier_team_remove WHERE target=? AND person=?", (target, name))
                if error is not None and error.status in {401, 410}:
                    self._member_gone_locked(target)
            if target in self.settings()["contacts"]:
                self.note_peer(target, error=error if error is not None and not isinstance(error, HttpError) else None)
        return target
