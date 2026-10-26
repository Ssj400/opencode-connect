"""What agents learn about the courier through MCP: who its contacts are, and what a
person message's state means. Names and states only, never URLs, tokens, or routes.
"""

import json
import time

from opencode_connect.courier.doctor import open_state
from opencode_connect.courier.status import COURIER, read_state

NO_COURIER = (f"No courier is set up on this machine, so there is no person:NAME to write to. "
              f"The user sets one up with `{COURIER} setup`, then `{COURIER} pair`.")
NO_CONTACTS = f"No contacts yet: the user pairs with someone with `{COURIER} pair`."
HELD_HERE = f"Held for your user's approval: they see and approve it with `{COURIER} approve`."
THEIRS = {"held": "{name}'s courier holds it until {name} approves it.",
          "approved": "{name} approved it for one of their sessions.",
          "rejected": "{name}'s courier rejected it.",
          "expired": "{name} did not approve it in time, so it expired."}


def read_people(root, now=None):
    """This courier's contacts and which way each works. Reads only: nothing is created."""
    db = open_state(root)
    state = None
    if db is not None:
        try:
            state = read_state(db, time.time() if now is None else now)
        finally:
            db.close()
    if state is None:
        return {"you": None, "contacts": [], "note": NO_COURIER}
    config = state["config"]
    names = sorted(set(config["tokens"]) | set(config["contacts"]))
    people = {"you": "person:" + config["person"],
              "contacts": [{"address": "person:" + name, "you_can_write": name in config["contacts"],
                            "they_can_write": name in config["tokens"]} for name in names]}
    if not names:
        people["note"] = NO_CONTACTS
    return people


def explain(db, rows):
    """Queue status `rows`, with what `held` means for messages from and to people. Their
    state is the courier's latest receipt, newer than the one stored at upload."""
    sent = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='courier_sent'").fetchone())
    for row in rows:
        if row["state"] == "held" and (row["source"] or "").startswith("person:"):
            row["meaning"] = HELD_HERE
        elif row["state"] == "delivered" and (row["target"] or "").startswith("person:"):
            latest = sent and db.execute("SELECT state FROM courier_sent WHERE id=?", (row["id"],)).fetchone()
            stored = json.loads(row["result"] or "null")
            theirs = latest[0] if latest else stored.get("state") if isinstance(stored, dict) else None
            if theirs in THEIRS:
                row["their_state"] = theirs
                row["meaning"] = THEIRS[theirs].format(name=row["target"].removeprefix("person:"))
    return rows
