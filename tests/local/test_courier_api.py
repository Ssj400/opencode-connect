"""Offline tests of the local queue API that the person-to-person courier uses."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import uuid

from opencode_connect.core import BridgeError, relay_once
from opencode_connect.local.mailbox import LocalMailbox, deliver_local
from opencode_connect.opencode_adapter import OpenCodeAdapter


class CourierQueueApiTests(unittest.TestCase):
    """The queue API a courier uses; it never writes the queue's own tables."""

    def setUp(self):
        environment = patch.dict("os.environ", {"OPENCODE_CONNECT_LOCAL_NODE": "local"})
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = LocalMailbox(Path(temporary.name))
        self.addCleanup(self.box.db.close)
        self.claude = "claude:" + str(uuid.UUID(int=1))

    def hold(self, text="Question from Alice", key="key-1", source="person:alice", reply_to=None):
        return self.box.insert_inbound(source, text, remote_key=key, reply_to=reply_to)

    def states(self):
        return {row["id"]: row["state"] for row in self.box.db.execute("SELECT id, state FROM messages")}

    def test_held_messages_have_no_target_and_no_consumer_claims_them(self):
        reply_to = "msg_" + "a" * 32
        held = self.hold(reply_to=reply_to)
        self.assertEqual((held["state"], held["duplicate"]), ("held", False))
        for match in (None, "person:", "claude:", self.claude, "local:"):
            with self.subTest(match=match):
                self.assertIsNone(self.box.claim(match))
        (row,) = self.box.inbound()
        self.assertEqual((row["id"], row["source"], row["state"], row["text"], row["reply_to"]),
                         (held["id"], "person:alice", "held", "Question from Alice", reply_to))

    def test_inbound_is_idempotent_per_person_and_key(self):
        first = self.hold()
        self.assertEqual(self.hold(text="A changed text is ignored"),
                         {"id": first["id"], "state": "held", "duplicate": True})
        self.assertNotEqual(self.hold(source="person:bob")["id"], first["id"])
        self.assertEqual(len(self.box.inbound()), 2)

    def test_invalid_inbound_is_refused(self):
        cases = ({"source": "local:ses_X"}, {"source": self.claude}, {"source": "wsl:ses_X"},
                 {"key": ""}, {"key": "has space"}, {"key": "k" * 129},
                 {"text": ""}, {"text": "a" * 12001}, {"reply_to": "not-a-message"})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(BridgeError):
                self.hold(**kwargs)
        self.assertEqual(self.box.inbound(), [])

    def test_approval_routes_to_the_chosen_session_and_delivers_quoted(self):
        held = self.hold()
        self.assertEqual(self.box.approve_inbound(held["id"], "ses_TARGET"),
                         {"id": held["id"], "state": "pending", "target": "local:ses_TARGET"})
        api = Mock()
        api.call.side_effect = lambda method, path, *args: (
            {"data": {"id": "ses_TARGET"}} if method == "get" else {"data": {"id": "inbox_EXAMPLE"}})
        outcome = relay_once(self.box, lambda job: deliver_local(job, OpenCodeAdapter(api)))
        self.assertEqual(outcome["state"], "delivered")
        text = api.call.call_args_list[1].args[2]["text"]
        self.assertIn("Reply address: person:alice", text)
        self.assertIn("> Question from Alice", text)

    def test_approval_to_claude_waits_for_that_session(self):
        held = self.hold()
        self.box.approve_inbound(held["id"], "claude:" + str(uuid.UUID(int=1)).upper())
        self.assertIsNone(self.box.claim())
        self.assertEqual(self.box.claim(self.claude)["id"], held["id"])

    def test_approval_requires_a_held_remote_message_and_a_local_target(self):
        held = self.hold()
        for target in ("person:alice", "wsl:ses_X", "not an address"):
            with self.subTest(target=target), self.assertRaises(BridgeError):
                self.box.approve_inbound(held["id"], target)
        local = self.box.enqueue("ses_SOURCE", "ses_TARGET", "Local message")
        for message_id in (local["id"], "msg_" + "0" * 32):
            with self.subTest(message_id=message_id), self.assertRaises(BridgeError):
                self.box.approve_inbound(message_id, "ses_TARGET")
        self.box.reject_inbound(held["id"])
        with self.assertRaises(BridgeError):
            self.box.approve_inbound(held["id"], "ses_TARGET")

    def test_rejection_and_state_are_visible_only_to_the_sender(self):
        held = self.hold()
        self.assertEqual(self.box.inbound_state(held["id"], "person:alice"), "held")
        self.assertEqual(self.box.reject_inbound(held["id"]), {"id": held["id"], "state": "rejected"})
        self.assertEqual(self.box.inbound_state(held["id"], "person:alice"), "rejected")
        self.assertIsNone(self.box.inbound_state(held["id"], "person:bob"))
        with self.assertRaises(BridgeError):
            self.box.reject_inbound(held["id"])
        with self.assertRaises(BridgeError):
            self.box.inbound_state(held["id"], "local:ses_TARGET")
        self.assertEqual(self.box.inbound(("rejected",))[0]["id"], held["id"])
        self.assertIsNone(self.box.get(held["id"])["result"])

    def test_get_reads_one_message_and_rejection_keeps_its_reason(self):
        self.assertIsNone(self.box.get("msg_" + "0" * 32))
        self.assertIsNone(self.box.diagnostics()["stored"])  # A read never binds the contract.
        held = self.hold()
        row = self.box.get(held["id"])
        self.assertEqual((row["id"], row["state"], row["source"], row["target"], row["payload"]["text"]),
                         (held["id"], "held", "person:alice", "", "Question from Alice"))
        self.assertFalse(self.box.db.in_transaction)
        self.box.reject_inbound(held["id"], reason="Retention expired.")
        self.assertEqual(json.loads(self.box.get(held["id"])["result"]), {"reason": "Retention expired."})
        self.box.enqueue("ses_SOURCE", "person:bob", "Upload")
        with self.box.transaction() as queue:
            job = queue.claim("person:")
            self.assertEqual(queue.get(job["id"])["state"], "sending")
        for index, reason in enumerate(("", " ", 7, "x" * 501)):
            message = self.hold(key=f"bad-{index}")
            with self.subTest(reason=index), self.assertRaises(BridgeError):
                self.box.reject_inbound(message["id"], reason=reason)
            self.assertEqual(self.box.get(message["id"])["state"], "held")

    def test_pending_count_matches_claims_without_writing(self):
        self.assertEqual(self.box.pending_count("person:"), 0)
        self.assertIsNone(self.box.diagnostics()["stored"])  # Counting binds nothing.
        for target in ("person:alice", "person:bob", "ses_TARGET", self.claude):
            self.box.enqueue("ses_SOURCE", target, "Count me")
        self.assertEqual((self.box.pending_count("person:"), self.box.pending_count("claude:"),
                          self.box.pending_count("local:")), (2, 1, 1))
        self.box.claim("person:")
        self.assertEqual(self.box.pending_count("person:"), 1)
        self.assertFalse(self.box.db.in_transaction)
        with self.assertRaises(BridgeError):
            self.box.pending_count("person")

    def test_reply_to_names_only_a_delivered_message_from_the_destination(self):
        held = self.hold()
        self.box.approve_inbound(held["id"], "ses_TARGET")
        with self.assertRaises(BridgeError):  # Approved but not yet delivered.
            self.box.enqueue("ses_TARGET", "person:alice", "Answer", reply_to=held["id"])
        self.box.mark(self.box.claim()["id"], "delivered", {"inbox_id": "inbox_EXAMPLE"})
        self.box.enqueue("ses_TARGET", "person:alice", "Answer", reply_to=held["id"])
        self.assertEqual(self.box.claim("person:")["payload"]["reply_to"], held["id"])
        local = self.box.enqueue("ses_OTHER", "ses_TARGET", "No link")
        self.assertNotIn("reply_to", self.box.get(local["id"])["payload"])
        self.box.mark(self.box.claim()["id"], "delivered", {"inbox_id": "inbox_EXAMPLE"})
        answer = self.box.enqueue("ses_TARGET", "ses_OTHER", "Local answer", reply_to=local["id"])
        self.assertEqual(self.box.get(answer["id"])["payload"]["reply_to"], local["id"])
        for index, (source, target, reply_to, error) in enumerate((
                ("ses_OTHER", "person:alice", held["id"], "delivered to this session"),  # Another session.
                ("ses_TARGET", "person:bob", held["id"], "delivered to this session"),  # Another person.
                ("ses_TARGET", "ses_THIRD", local["id"], "delivered to this session"),  # Another sender.
                ("ses_TARGET", "person:alice", "msg_" + "0" * 32, "delivered to this session"),
                ("ses_TARGET", "person:alice", "not-a-message-id", "message ID"),
                ("ses_TARGET", "person:alice", 7, "message ID"))):
            with self.subTest(case=index), self.assertRaisesRegex(BridgeError, error):
                self.box.enqueue(source, target, "Answer", reply_to=reply_to)

    def test_cancel_person_spares_sending_messages_and_other_people(self):
        sending = self.hold(key="sending")
        self.box.approve_inbound(sending["id"], "ses_OTHER")
        self.box.claim()
        approved = self.hold(key="approved")
        self.box.approve_inbound(approved["id"], "ses_TARGET")
        held = self.hold(key="held")
        bob = self.hold(key="bob", source="person:bob")
        outbound = self.box.enqueue("ses_SOURCE", "person:alice", "Unsent message to Alice")
        self.assertEqual(self.box.cancel_person("alice"), 2)
        states = self.states()
        self.assertEqual((states[held["id"]], states[approved["id"]]), ("cancelled", "cancelled"))
        self.assertEqual((states[sending["id"]], states[bob["id"]], states[outbound["id"]]),
                         ("sending", "held", "pending"))
        self.assertEqual(self.box.cancel_person("person:alice", outbound=True), 1)
        self.assertEqual(self.states()[outbound["id"]], "cancelled")
        for person in ("local:ses_X", "bad name"):
            with self.subTest(person=person), self.assertRaises(BridgeError):
                self.box.cancel_person(person)

    def test_release_returns_only_claimed_uploads_to_people(self):
        outbound = self.box.enqueue("ses_SOURCE", "person:alice", "Upload me")
        job = self.box.claim("person:")
        self.assertEqual(self.box.release(job["id"]), {"id": job["id"], "state": "pending"})
        self.assertEqual(self.box.claim("person:")["id"], outbound["id"])
        local = self.box.enqueue("ses_SOURCE", "ses_TARGET", "Local")
        self.box.claim()
        pending = self.box.enqueue("ses_SOURCE", "person:bob", "Still pending")
        for message_id in (local["id"], pending["id"], "msg_" + "0" * 32):
            with self.subTest(message_id=message_id), self.assertRaises(BridgeError):
                self.box.release(message_id)

    def test_courier_transaction_is_atomic_and_queue_methods_join_it(self):
        with self.box.transaction() as queue:
            queue.db.execute("CREATE TABLE courier_sent (message_id TEXT PRIMARY KEY)")
            held = queue.insert_inbound("person:alice", "Joined", remote_key="joined")
            queue.approve_inbound(held["id"], "ses_TARGET")
            job = queue.claim()
            queue.mark(job["id"], "delivered", {"inbox_id": "inbox_EXAMPLE"})
            queue.db.execute("INSERT INTO courier_sent VALUES (?)", (job["id"],))
        self.assertEqual(self.box.inbound(("delivered",))[0]["id"], held["id"])
        with self.assertRaises(RuntimeError):
            with self.box.transaction() as queue:
                rolled = queue.insert_inbound("person:alice", "Rolled back", remote_key="rolled")
                queue.db.execute("INSERT INTO courier_sent VALUES (?)", (rolled["id"],))
                raise RuntimeError("courier failure")
        self.assertEqual(self.box.inbound(), [])
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM courier_sent").fetchone()[0], 1)
        retry = self.box.insert_inbound("person:alice", "Rolled back", remote_key="rolled")
        self.assertFalse(retry["duplicate"])
        with self.box.transaction():
            with self.assertRaisesRegex(BridgeError, "already open"):
                with self.box.transaction():
                    pass

    def test_transaction_checks_the_contract_before_any_write(self):
        self.box.claim()
        other = LocalMailbox(self.box.root, node="other")
        self.addCleanup(other.db.close)
        ran = []
        with self.assertRaisesRegex(BridgeError, "mismatch"):
            with other.transaction():
                ran.append(True)
        self.assertEqual(ran, [])
        with self.assertRaises(BridgeError):
            other.insert_inbound("person:alice", "Wrong node", remote_key="wrong")
        self.assertEqual(self.box.inbound(), [])

    def test_inbound_does_not_consume_the_local_send_budget(self):
        for index in range(31):
            self.hold(key=f"key-{index}")
        for _ in range(30):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "Local")
        with self.assertRaisesRegex(BridgeError, "30 sends per hour"):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "One too many")
        self.assertEqual(self.hold(key="after-limit")["state"], "held")


if __name__ == "__main__":
    unittest.main()
