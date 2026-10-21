"""The courier over a real LocalMailbox queue."""

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from opencode_connect.claude import hook as claude_hook
from opencode_connect.claude import mcp_server as claude_mcp_server
from opencode_connect.core import BridgeError
from opencode_connect.courier.app import RETENTION, Courier, RequestError
from opencode_connect.courier.cli import main
from opencode_connect.courier.settings import write_config
from opencode_connect.local import mailbox
from opencode_connect.local import mcp_server as local_mcp_server
from opencode_connect.transports import HttpError
from tests.courier.fakes import configuration

CLAUDE = "claude:" + str(uuid.UUID(int=5))


class RealQueueCourierTests(unittest.TestCase):
    """Real v2 mailboxes, independent connections, and mocked harness/HTTP effects."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="courier-real-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.now = 1000.0
        self.a_root, self.b_root = self.root / "alice", self.root / "bob"
        self.a_box = mailbox.LocalMailbox(self.a_root, node="wsl")
        self.b_box = mailbox.LocalMailbox(self.b_root, node="linux")
        self.addCleanup(self.a_box.db.close)
        self.addCleanup(self.b_box.db.close)
        self.alice = Courier(self.a_box, configuration("alice"), clock=lambda: self.now)
        self.bob = Courier(self.b_box, configuration("bob"), clock=lambda: self.now)
        self.a_card = self.alice.invite("bob")
        self.b_card = self.bob.invite("alice")
        self.alice.trust("bob", self.b_card)
        self.bob.trust("alice", self.a_card)

    def receive(self, key="example"):
        return self.bob.receive("Bearer " + self.b_card["token"], {"text": "hello", "idempotency_key": key})

    def relay_mock(self, box):
        adapter = Mock(supported_deliveries=frozenset({"queue"}))
        adapter.api.call.side_effect = lambda method, path: (
            {"version": "2.0.0"} if path == "/api/info" else {"data": {"id": "ses_example", "time": {}}})
        adapter.send_message.return_value = "accepted_example"
        outcome = mailbox.local_relay_once(box, adapter)
        return outcome, adapter

    def test_mcp_courier_human_approval_local_relay_and_correlated_reply(self):
        outgoing = local_mcp_server.call_tool({"name": "send", "arguments": {
            "to": "person:bob", "text": "hello"}, "_meta": {"ai.opencode/sessionID": "ses_example"}},
            root=self.a_root, node="wsl")
        def receive_at_bob(url, **kwargs):
            return self.bob.receive(kwargs["headers"]["Authorization"], kwargs["data"])
        with patch("opencode_connect.courier.app.http_json", side_effect=receive_at_bob):
            self.alice.send_once()
        receipt = json.loads(self.a_box.get(outgoing["id"])["result"])
        self.assertEqual(receipt["state"], "held")
        self.assertIsNone(self.b_box.claim())
        self.assertEqual(self.b_box.get(receipt["remote_id"])["state"], "held")
        self.bob.approve(receipt["remote_id"], "linux:ses_example")
        outcome, adapter = self.relay_mock(self.b_box)
        self.assertEqual(outcome["state"], "delivered")
        envelope = adapter.send_message.call_args.kwargs["text"]
        self.assertIn("Reply address: person:alice", envelope)
        self.assertIn("> hello", envelope)
        reply = local_mcp_server.call_tool({"name": "send", "arguments": {
            "to": "person:alice", "text": "received", "reply_to": receipt["remote_id"]},
            "_meta": {"ai.opencode/sessionID": "ses_example"}}, root=self.b_root, node="linux")
        def receive_at_alice(url, **kwargs):
            return self.alice.receive(kwargs["headers"]["Authorization"], kwargs["data"])
        with patch("opencode_connect.courier.app.http_json", side_effect=receive_at_alice):
            self.bob.send_once()
        inbound = self.alice.inbox()[0]
        self.assertEqual(inbound["suggested_target"], "wsl:ses_example")
        self.assertEqual(self.a_box.get(inbound["id"])["state"], "held")
        self.alice.approve(inbound["id"], "wsl:ses_example")
        self.assertEqual(self.relay_mock(self.a_box)[0]["state"], "delivered")
        self.assertEqual(self.b_box.get(reply["id"])["state"], "delivered")
        self.assertFalse(self.a_box.db.in_transaction)
        self.assertFalse(self.b_box.db.in_transaction)

    def claude(self, name, tool_use, **arguments):
        """A tool call from a Claude Code session on alice's machine, stamped as its hook would."""
        claude_hook.record(tool_use, CLAUDE, root=self.a_root)
        return claude_mcp_server.call_claude_tool({"name": name, "arguments": arguments,
                                                   "_meta": {"claudecode/toolUseId": tool_use}},
                                                  root=self.a_root, node="wsl")

    def test_claude_code_asks_another_persons_opencode_session_and_receives_the_reply(self):
        question = self.claude("send", "toolu_ask", to="person:bob", text="Which tests cover pairing?")
        def receive_at_bob(url, **kwargs):
            return self.bob.receive(kwargs["headers"]["Authorization"], kwargs["data"])
        with patch("opencode_connect.courier.app.http_json", side_effect=receive_at_bob):
            self.alice.send_once()
        receipt = json.loads(self.a_box.get(question["id"])["result"])
        self.bob.approve(receipt["remote_id"], "linux:ses_example")
        outcome, adapter = self.relay_mock(self.b_box)
        self.assertEqual(outcome["state"], "delivered")
        self.assertIn("Reply address: person:alice", adapter.send_message.call_args.kwargs["text"])
        reply = local_mcp_server.call_tool({"name": "send", "arguments": {
            "to": "person:alice", "text": "test_pairing.py", "reply_to": receipt["remote_id"]},
            "_meta": {"ai.opencode/sessionID": "ses_example"}}, root=self.b_root, node="linux")
        def receive_at_alice(url, **kwargs):
            return self.alice.receive(kwargs["headers"]["Authorization"], kwargs["data"])
        with patch("opencode_connect.courier.app.http_json", side_effect=receive_at_alice):
            self.bob.send_once()
        inbound = self.alice.inbox()[0]
        self.assertEqual(inbound["suggested_target"], CLAUDE)  # The session that asked.
        self.alice.approve(inbound["id"], CLAUDE)
        self.assertIsNone(self.relay_mock(self.a_box)[0])  # Claude's hooks deliver it, not the relay.
        received = self.claude("receive", "toolu_receive")
        self.assertEqual(received["id"], inbound["id"])
        self.assertIn("> test_pairing.py", received["message"])
        self.assertIn("Reply address: person:bob", received["message"])
        self.assertEqual(self.b_box.get(reply["id"])["state"], "delivered")

    def test_uploads_fail_after_the_retention_period_and_retry_before_it(self):
        young = self.a_box.enqueue("ses_example", "person:bob", "Still worth trying")["id"]
        self.now = time.time()
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("HTTP request failed (TimeoutError).")):
            self.alice.send_once()
            self.assertEqual(self.a_box.get(young)["state"], "pending")
            self.now = time.time() + RETENTION + 10
            self.alice.send_once()
        old = self.a_box.get(young)
        self.assertEqual(old["state"], "failed")
        self.assertEqual(json.loads(old["result"])["reason"],
                         "Unreachable for 7 days: HTTP request failed (TimeoutError).")

    def test_a_refused_upload_fails_at_once_and_a_busy_peer_is_retried(self):
        sent = {}
        for status, state in ((401, "failed"), (429, "pending"), (503, "pending")):
            with self.subTest(status=status):
                sent[status] = self.a_box.enqueue("ses_example", "person:bob", f"Answered {status}")["id"]
                with patch("opencode_connect.courier.app.http_json", side_effect=HttpError(status, "Example reason.")):
                    self.alice.send_once()
                self.assertEqual(self.a_box.get(sent[status])["state"], state)
        self.assertEqual(json.loads(self.a_box.get(sent[401])["result"])["reason"],
                         "HTTP request failed with status 401: Example reason.")

    def test_direct_routes_deliver_through_the_real_queue_or_stay_held(self):
        self.bob.set_route("alice", "linux:ses_example", "direct")
        self.assertEqual(self.receive("direct")["state"], "approved")
        outcome, _ = self.relay_mock(self.b_box)
        self.assertEqual(outcome["state"], "delivered")
        # A route to a node this queue does not serve fails inside the queue; the message waits.
        self.bob.set_route("alice", "wsl:ses_example", "direct")
        held = self.receive("other-node")
        self.assertEqual(held["state"], "held")
        self.assertEqual(self.b_box.get(held["id"])["state"], "held")
        self.assertEqual([row["id"] for row in self.bob.inbox()], [held["id"]])
        self.assertFalse(self.b_box.db.in_transaction)

    def test_expiry_and_rejection_use_real_held_transitions(self):
        expired, rejected = self.receive("expires"), self.receive("rejects")
        self.bob.reject(rejected["id"])
        self.now += RETENTION + 1
        self.assertEqual(self.bob.inbox(), [])
        self.assertEqual(self.b_box.get(expired["id"])["state"], "rejected")
        self.assertEqual(json.loads(self.b_box.get(expired["id"])["result"])["reason"], "Retention expired.")
        self.assertEqual(self.bob.remote_status("Bearer " + self.b_card["token"], expired["id"])["state"], "expired")

    def test_real_queue_crash_recovery_is_idempotent(self):
        job = self.a_box.enqueue("wsl:ses_example", "person:bob", "hello")
        def receive_then_crash(url, **kwargs):
            self.bob.receive(kwargs["headers"]["Authorization"], kwargs["data"])
            raise RuntimeError("Simulated crash after remote acceptance.")
        with patch("opencode_connect.courier.app.http_json", side_effect=receive_then_crash), self.assertRaises(RuntimeError):
            self.alice.send_once()
        self.assertEqual(self.a_box.get(job["id"])["state"], "sending")
        # Reopen the worker connection to exercise persistent metadata, not memory.
        recovered_box = mailbox.LocalMailbox(self.a_root, node="wsl")
        self.addCleanup(recovered_box.db.close)
        recovered = Courier(recovered_box, configuration("alice"), clock=lambda: self.now)
        recovered.recover_outbound()
        with patch("opencode_connect.courier.app.http_json", side_effect=lambda url, **kw: self.bob.receive(
                kw["headers"]["Authorization"], kw["data"])):
            recovered.send_once()
        self.assertEqual(self.a_box.get(job["id"])["state"], "delivered")
        self.assertEqual(len(self.bob.inbox()), 1)

    def test_foreign_contract_fails_before_courier_writes(self):
        other = mailbox.LocalMailbox(self.b_root, node="other")
        self.addCleanup(other.db.close)
        before = self.b_box.db.execute("SELECT data FROM courier_settings").fetchall()[0][0]
        with self.assertRaisesRegex(BridgeError, "configuration mismatch"):
            Courier(other, configuration("bob"))
        self.assertEqual(self.b_box.db.execute("SELECT data FROM courier_settings").fetchall()[0][0], before)
        self.assertFalse(other.db.in_transaction)

    def test_cli_approval_and_worker_use_independent_connections(self):
        receipt = self.receive()
        config_path = self.root / "courier.json"
        write_config(config_path, self.bob.settings())
        ready, approved = threading.Event(), threading.Event()
        results, failures = [], []
        def worker():
            box = mailbox.LocalMailbox(self.b_root, node="linux")
            try:
                self.assertIsNone(box.claim())
                ready.set()
                self.assertTrue(approved.wait(3))
                job = box.claim()
                results.append(job["id"])
                box.mark(job["id"], "delivered", {"inbox_id": "accepted_example"})
            except BaseException as exc:
                failures.append(exc)
                ready.set()
            finally:
                box.db.close()
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            real_courier = Courier
            with patch("opencode_connect.courier.cli.interactive_confirmation"), \
                    patch("opencode_connect.courier.cli.require_terminal"), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("opencode_connect.courier.cli.Courier", side_effect=lambda queue, config, **kw:
                          real_courier(queue, config, clock=lambda: self.now, **kw)):
                result = main(["--config", str(config_path), "approve", receipt["id"],
                                       "--to", "linux:ses_example"],
                                      queue_factory=lambda path: mailbox.LocalMailbox(self.b_root, node="linux"))
            self.assertEqual(result, 0)
        finally:
            approved.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [receipt["id"]])
        self.assertEqual(self.b_box.get(receipt["id"])["state"], "delivered")

    def test_revocation_racing_upload_is_atomic_across_connections(self):
        ready = threading.Barrier(3)
        inserted, revoking, proceed = threading.Event(), threading.Event(), threading.Event()
        receipts, failures = [], []
        def upload():
            box = mailbox.LocalMailbox(self.b_root, node="linux")
            try:
                app = Courier(box, configuration("bob"))
                insert = box.insert_inbound
                def pause(*args, **kwargs):
                    inserted.set()
                    self.assertTrue(proceed.wait(3))
                    return insert(*args, **kwargs)
                ready.wait(3)
                with patch.object(box, "insert_inbound", side_effect=pause):
                    receipts.append(app.receive("Bearer " + self.b_card["token"], {
                        "text": "racing", "idempotency_key": "race"}))
            except BaseException as exc:
                failures.append(exc)
            finally:
                box.db.close()
        def revoke():
            box = mailbox.LocalMailbox(self.b_root, node="linux")
            try:
                app = Courier(box, configuration("bob"))
                ready.wait(3)
                self.assertTrue(inserted.wait(3))
                revoking.set()
                app.untrust("alice")
            except BaseException as exc:
                failures.append(exc)
            finally:
                box.db.close()
        threads = [threading.Thread(target=upload), threading.Thread(target=revoke)]
        for thread in threads:
            thread.start()
        try:
            ready.wait(3)
            self.assertTrue(inserted.wait(3))
            self.assertTrue(revoking.wait(3))
        finally:
            proceed.set()
            for thread in threads:
                thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(len(receipts), 1)
        self.assertEqual(self.b_box.get(receipts[0]["id"])["state"], "cancelled")
        self.assertEqual(self.bob.inbox(), [])
        with self.assertRaises(RequestError) as denied:
            self.receive("after-revocation")
        self.assertEqual(denied.exception.status, 401)
