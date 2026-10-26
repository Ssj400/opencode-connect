"""What agents learn about the courier through MCP: its contacts, and what `held` means."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencode_connect.claude.mcp_server import TOOLS as CLAUDE_TOOLS
from opencode_connect.courier.app import Courier
from opencode_connect.courier.settings import write_config
from opencode_connect.local import mailbox
from opencode_connect.local.mcp_server import TOOLS, call_tool
from tests.courier.fakes import configuration


class PeopleTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"OPENCODE_CONNECT_LOCAL_NODE": "local"})
        environment.start()
        self.addCleanup(environment.stop)
        folder = tempfile.TemporaryDirectory(prefix="courier-people-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "queue"
        self.box = mailbox.LocalMailbox(self.root, node="local")
        self.addCleanup(self.box.db.close)
        (self.box.state / "mail.sqlite3").chmod(0o600)
        self.now = 1000.0
        path = Path(folder.name) / "courier.json"
        write_config(path, configuration())
        self.app = Courier(self.box, configuration(), config_path=path, clock=lambda: self.now)

    def call(self, name, root=None):
        return call_tool({"name": name, "arguments": {}, "_meta": {"ai.opencode/sessionID": "ses_SOURCE"}},
                         root=self.root if root is None else root)

    def test_people_lists_each_contact_and_which_way_it_works_without_secrets(self):
        bob = self.app.invite("bob")
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43})
        carol = self.app.invite("carol")
        self.app.trust("dave", {"person": "dave", "url": "http://192.0.2.30:8765", "token": "D" * 43})
        self.app.set_route("bob", "local:ses_TARGET", "direct")
        database = self.box.state / "mail.sqlite3"
        before = (database.read_bytes(), database.stat().st_mtime_ns)
        people = self.call("people")
        self.assertEqual(people, {"you": "person:alice", "contacts": [
            {"address": "person:bob", "you_can_write": True, "they_can_write": True},
            {"address": "person:carol", "you_can_write": False, "they_can_write": True},
            {"address": "person:dave", "you_can_write": True, "they_can_write": False}]})
        self.assertEqual((database.read_bytes(), database.stat().st_mtime_ns), before)
        text = json.dumps(people)
        for secret in (bob["token"], carol["token"], "B" * 43, "192.0.2.20", "http", "ses_TARGET", "direct"):
            self.assertNotIn(secret, text)

    def test_without_a_courier_or_contacts_people_says_what_the_user_can_run(self):
        missing = self.root.parent / "missing"
        people = self.call("people", root=missing)
        self.assertEqual((people["you"], people["contacts"]), (None, []))
        self.assertIn("python3 bin/courier.py setup", people["note"])
        self.assertFalse(missing.exists())
        alone = Path(self.root.parent / "alone")
        mailbox.LocalMailbox(alone, node="local").db.close()  # A queue whose courier never ran.
        (alone / ".state/mail.sqlite3").chmod(0o600)
        self.assertIn("courier.py setup", self.call("people", root=alone)["note"])
        people = self.call("people")
        self.assertEqual((people["you"], people["contacts"]), ("person:alice", []))
        self.assertIn("python3 bin/courier.py pair", people["note"])

    def test_both_servers_offer_people(self):
        for tools in (TOOLS, CLAUDE_TOOLS):
            self.assertIn("people", {tool["name"] for tool in tools})

    def test_status_says_what_held_means_on_each_side(self):
        carol = self.app.invite("carol")
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43})
        inbound = self.app.receive("Bearer " + carol["token"], {"text": "hello", "idempotency_key": "one"})
        outbound = self.box.enqueue("local:ses_SOURCE", "person:bob", "question")
        local = self.box.enqueue("local:ses_SOURCE", "local:ses_OTHER", "note")
        remote = "msg_" + "3" * 32
        with patch("opencode_connect.courier.app.http_json", return_value={"id": remote, "state": "held"}):
            self.app.send_once()

        def rows():
            return {row["id"]: row for row in self.call("status")}

        status = rows()
        self.assertIn("courier.py approve", status[inbound["id"]]["meaning"])
        self.assertEqual((status[outbound["id"]]["their_state"], status[outbound["id"]]["meaning"]),
                         ("held", "bob's courier holds it until bob approves it."))
        self.assertNotIn("meaning", status[local["id"]])
        self.now += 61
        with patch("opencode_connect.courier.app.http_json", return_value={"id": remote, "state": "approved"}):
            self.app.poll_once()
        status = rows()  # The latest receipt, not the state stored at upload.
        self.assertEqual(status[outbound["id"]]["their_state"], "approved")
        self.assertIn("bob approved it", status[outbound["id"]]["meaning"])

    def test_without_the_courier_s_receipts_the_state_stored_at_upload_is_used(self):
        plain = self.root.parent / "plain"
        box = mailbox.LocalMailbox(plain, node="local")
        self.addCleanup(box.db.close)
        sent = box.enqueue("local:ses_SOURCE", "person:bob", "question")
        box.mark(box.claim("person:")["id"], "delivered", {"remote_id": "msg_" + "4" * 32, "state": "held"})
        (row,) = self.call("status", root=plain)
        self.assertEqual((row["id"], row["their_state"]), (sent["id"], "held"))


if __name__ == "__main__":
    unittest.main()
