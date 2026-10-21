"""Shared courier test doubles: a queue with the LocalMailbox contract, and a configuration."""

from contextlib import contextmanager
import json
import sqlite3
import uuid



class FakeQueue:
    """Contract fake, not an operational local queue or replacement relay.

    Courier metadata and queue rows use the same connection/transaction. Adapter
    methods participating in a transaction must never commit independently.
    """

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=2)
        self.db.execute("""CREATE TABLE IF NOT EXISTS fake_messages (
            id TEXT PRIMARY KEY, source TEXT, target TEXT, text TEXT,
            state TEXT, result TEXT, remote_key TEXT, reply_to TEXT)""")
        self.claimed = []

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def insert_inbound(self, source, text, *, remote_key, reply_to=None):
        assert self.db.in_transaction
        assert source.startswith("person:")
        message_id = "msg_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO fake_messages VALUES(?,?,NULL,?, 'held', NULL,?,?)",
                        (message_id, source, text, remote_key, reply_to))
        return {"id": message_id, "state": "held", "duplicate": False}

    def enqueue(self, source, target, text, reply_to=None):
        message_id = "msg_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO fake_messages VALUES(?,?,?,?, 'pending', NULL,NULL,?)",
                        (message_id, source, target, text, reply_to))
        return message_id

    def get(self, message_id):
        columns = ("id", "source", "target", "text", "state", "result", "remote_key", "reply_to")
        row = self.db.execute("SELECT * FROM fake_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            return None
        result = dict(zip(columns, row))
        result["payload"] = {"text": result.pop("text"), "reply_to": result.pop("reply_to")}
        return result

    def pending_count(self, match):
        return self.db.execute("SELECT count(*) FROM fake_messages WHERE state='pending' AND substr(target,1,?)=?",
                               (len(match), match)).fetchone()[0]

    def claim(self, match):
        assert self.db.in_transaction and match == "person:"
        row = self.db.execute("SELECT id FROM fake_messages WHERE state='pending' AND substr(target,1,7)=? ORDER BY rowid LIMIT 1",
                              (match,)).fetchone()
        if row is None:
            return None
        self.db.execute("UPDATE fake_messages SET state='sending' WHERE id=?", row)
        self.claimed.append(row[0])
        return self.get(row[0])

    def mark(self, message_id, state, result):
        assert self.db.in_transaction
        assert self.get(message_id)["state"] == "sending"
        assert state in {"delivered", "uncertain", "failed", "cancelled"}
        self.db.execute("UPDATE fake_messages SET state=?,result=? WHERE id=?",
                        (state, json.dumps(result), message_id))

    def release(self, message_id):
        assert self.db.in_transaction
        row = self.get(message_id)
        assert row["target"].startswith("person:") and row["state"] == "sending"
        self.db.execute("UPDATE fake_messages SET state='pending' WHERE id=?", (message_id,))

    def approve_inbound(self, message_id, target):
        assert self.db.in_transaction
        self.db.execute("UPDATE fake_messages SET target=?,state='pending' WHERE id=? AND state='held' AND substr(source,1,7)='person:'",
                        (target, message_id))
        return {"id": message_id, "state": "pending", "target": target}

    def reject_inbound(self, message_id, reason=None):
        assert self.db.in_transaction
        assert self.get(message_id)["state"] == "held"
        self.db.execute("UPDATE fake_messages SET state='rejected',result=? WHERE id=?",
                        (json.dumps({"reason": reason}), message_id))
        return {"id": message_id, "state": "rejected"}

    def cancel_person(self, source, *, outbound=False):
        assert self.db.in_transaction and source.startswith("person:")
        self.db.execute("UPDATE fake_messages SET state='cancelled' WHERE source=? AND state IN ('held','pending')", (source,))
        if outbound:
            self.db.execute("UPDATE fake_messages SET state='cancelled' WHERE target=? AND state='pending'", (source,))


def configuration(name="alice"):
    return {"person": name, "listen": "127.0.0.1:0", "url": "http://192.0.2.10:8765",
            "tokens": {}, "contacts": {}}
