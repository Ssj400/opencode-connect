"""Deterministic offline courier tests; no harness, relay, VPN, or real credentials."""

from contextlib import contextmanager
import hashlib
import http.client
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
import uuid

import courier
import local_bridge
import local_mcp_server
from core import BridgeError
from transports import HttpError, http_json


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


class CourierTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="courier-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.now = 1000.0
        self.alice = self.make_courier("alice")
        self.bob = self.make_courier("bob")
        self.alice_server = self.serve(self.alice)
        self.bob_server = self.serve(self.bob)
        self.alice_card = self.alice.invite("bob")
        self.bob_card = self.bob.invite("alice")
        self.alice_card["url"] = self.alice_url
        self.bob_card["url"] = self.bob_url
        self.alice.trust("bob", self.bob_card)
        self.bob.trust("alice", self.alice_card)

    def make_courier(self, name):
        queue = FakeQueue(self.root / (name + ".sqlite3"))
        self.addCleanup(queue.db.close)
        return courier.Courier(queue, configuration(name), clock=lambda: self.now)

    def serve(self, app):
        server = courier.CourierServer(app, "127.0.0.1:0")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        setattr(self, app.settings()["person"] + "_url", f"http://127.0.0.1:{server.server_address[1]}")
        return server

    def headers(self, card=None):
        return {"Authorization": "Bearer " + (card or self.bob_card)["token"]}

    def receive(self, text="hello", key="sample", **extra):
        return http_json(self.bob_url + "/v1/messages", method="POST", headers=self.headers(),
                         data={"text": text, "idempotency_key": key, **extra})

    def test_two_couriers_send_and_hold_without_local_delivery(self):
        message_id = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        self.assertEqual(self.alice.send_once(), message_id)
        row = self.alice.queue.get(message_id)
        self.assertEqual(row["state"], "delivered")
        receipt = json.loads(row["result"])
        self.assertEqual(receipt["state"], "held")
        remote = self.bob.queue.get(receipt["remote_id"])
        self.assertEqual((remote["state"], remote["target"], remote["source"]), ("held", None, "person:alice"))
        self.assertIsNone(self.bob.send_once())

    def test_idempotency_and_conflicting_reuse(self):
        first = self.receive()
        self.assertEqual(self.receive(), first)
        self.assertEqual(len(self.bob.inbox()), 1)
        with self.assertRaises(HttpError) as caught:
            self.receive(text="different")
        self.assertEqual(caught.exception.status, 409)

    def test_atomic_inbound_rollback_does_not_leave_queue_row(self):
        original = self.bob.queue.insert_inbound
        def fail(**kwargs):
            original(**kwargs)
            raise BridgeError("Simulated insertion failure.")
        with patch.object(self.bob.queue, "insert_inbound", side_effect=fail):
            with self.assertRaises(HttpError) as caught:
                self.receive()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.bob.queue.db.execute("SELECT count(*) FROM fake_messages").fetchone()[0], 0)
        self.assertEqual(self.bob.inbox(), [])
        self.assertEqual(self.receive()["state"], "held")

    def test_foreign_and_revoked_tokens_rejected(self):
        for token in ("X" * 43, self.alice_card["token"]):
            with self.subTest(token_length=len(token)), self.assertRaises(HttpError) as caught:
                http_json(self.bob_url + "/v1/whoami", headers={"Authorization": "Bearer " + token})
            self.assertEqual(caught.exception.status, 401)
        self.bob.untrust("alice")
        with self.assertRaises(HttpError) as caught:
            self.receive()
        self.assertEqual(caught.exception.status, 401)

    def test_remote_status_only_for_token_owner(self):
        receipt = self.receive()
        stranger = self.bob.invite("carol")
        with self.assertRaises(HttpError) as caught:
            http_json(self.bob_url + "/v1/messages/" + receipt["id"], headers=self.headers(stranger))
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(http_json(self.bob_url + "/v1/messages/" + receipt["id"], headers=self.headers()), receipt)

    def test_approval_is_explicit_and_releases_exactly_one_message(self):
        first, second = self.receive(), self.receive(key="other")
        self.bob.approve(first["id"], "wsl:ses_example")
        self.assertEqual(self.bob.queue.get(first["id"])["target"], "wsl:ses_example")
        self.assertEqual(self.bob.queue.get(first["id"])["state"], "pending")
        self.assertEqual(self.bob.queue.get(second["id"])["state"], "held")
        self.assertIsNone(self.bob.send_once())
        self.assertEqual(self.bob.remote_status("Bearer " + self.bob_card["token"], first["id"])["state"], "approved")
        with self.assertRaises(BridgeError):
            self.bob.approve(first["id"], "wsl:ses_other")

    def test_approval_rejects_person_and_noncanonical_targets(self):
        receipt = self.receive()
        for target in ("person:bob", "person:ses_example", "ses_example", "claude:not-a-uuid", "wsl:ses_example\n"):
            with self.subTest(target=target), self.assertRaises(BridgeError):
                self.bob.approve(receipt["id"], target)
        self.bob.approve(receipt["id"], "claude:" + str(uuid.uuid4()))

    def test_reject_and_retention_never_release(self):
        first, second = self.receive(), self.receive(key="expire")
        self.bob.reject(first["id"])
        self.now += courier.RETENTION + 1
        self.assertEqual(self.bob.inbox(), [])
        self.assertEqual(self.bob.queue.get(first["id"])["state"], "rejected")
        self.assertEqual(self.bob.queue.get(second["id"])["state"], "rejected")
        self.assertEqual(self.receive(key="expire")["state"], "expired")
        with self.assertRaises(BridgeError):
            self.bob.approve(second["id"], "wsl:ses_example")

    def test_untrust_cancels_held_approved_pending_and_outbound_atomically(self):
        held, approved = self.receive(), self.receive(key="approved")
        self.bob.approve(approved["id"], "wsl:ses_example")
        outbound = self.bob.queue.enqueue("wsl:ses_example", "person:alice", "reply")
        other = self.bob.queue.enqueue("wsl:ses_example", "person:carol", "unrelated")
        self.bob.untrust("alice")
        self.assertEqual(self.bob.contacts(), [])
        for message_id in (held["id"], approved["id"], outbound):
            self.assertEqual(self.bob.queue.get(message_id)["state"], "cancelled")
        self.assertEqual(self.bob.queue.get(other)["state"], "pending")
        self.assertEqual(self.bob.inbox(), [])

    def test_untrust_rollback_restores_token_card_and_pending(self):
        receipt = self.receive()
        with patch.object(self.bob.queue, "cancel_person", side_effect=BridgeError("Simulated failure.")):
            with self.assertRaises(BridgeError):
                self.bob.untrust("alice")
        self.assertEqual(self.bob.authenticate("Bearer " + self.bob_card["token"]), "alice")
        self.assertEqual(self.bob.queue.get(receipt["id"])["state"], "held")
        self.assertTrue(self.bob.contacts()[0]["sending"])

    def test_offline_destination_releases_and_retries_without_duplicates(self):
        message_id = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        with patch("courier.http_json", side_effect=BridgeError("Destination unavailable.")) as request:
            self.alice.send_once()
            self.alice.send_once()
        request.assert_called_once()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "pending")
        self.now += 3
        self.alice.send_once()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "delivered")
        self.assertEqual(len(self.bob.inbox()), 1)

    def test_unreachable_loopback_port_releases_then_delivers(self):
        message_id = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        # Reserve a local port without listening: deterministic connection refusal,
        # not a live LAN/VPN probe or a guessed unused port.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            offline = {**self.bob_card, "url": f"http://127.0.0.1:{reservation.getsockname()[1]}"}
            self.alice.trust("bob", offline)
            self.alice.send_once()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "pending")
        self.alice.trust("bob", self.bob_card)
        self.now += 3
        self.alice.send_once()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "delivered")
        self.assertEqual(len(self.bob.inbox()), 1)

    def test_lost_post_response_retries_same_key_without_duplicate(self):
        message_id = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        def accepted_then_lost(*args, **kwargs):
            http_json(*args, **kwargs)
            raise BridgeError("Response lost after commit.")
        with patch("courier.http_json", side_effect=accepted_then_lost):
            self.alice.send_once()
        self.assertEqual(len(self.bob.inbox()), 1)
        self.now += 3
        self.alice.send_once()
        self.assertEqual(len(self.bob.inbox()), 1)
        self.assertEqual(self.alice.queue.get(message_id)["state"], "delivered")

    def test_backoff_does_not_block_other_ready_outbound(self):
        first = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "offline")
        with patch("courier.http_json", side_effect=BridgeError("Destination unavailable.")):
            self.alice.send_once()
        second = self.alice.queue.enqueue("wsl:ses_other", "person:bob", "ready")
        self.assertEqual(self.alice.send_once(), second)
        self.assertEqual(self.alice.queue.get(first)["state"], "pending")
        self.assertEqual(self.alice.queue.get(second)["state"], "delivered")

    def test_crash_recovery_retries_only_owned_http_claims_idempotently(self):
        message_id = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        local = self.alice.queue.enqueue("person:bob", "wsl:ses_example", "unrelated local delivery")
        with self.alice.transaction():
            self.alice.db.execute("UPDATE fake_messages SET state='sending' WHERE id=?", (local,))
        def accepted_then_crashed(*args, **kwargs):
            http_json(*args, **kwargs)
            raise RuntimeError("Simulated process crash.")
        with patch("courier.http_json", side_effect=accepted_then_crashed):
            with self.assertRaises(RuntimeError):
                self.alice.send_once()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "sending")
        self.alice.recover_outbound()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "pending")
        self.assertEqual(self.alice.queue.get(local)["state"], "sending")
        self.alice.send_once()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "delivered")
        self.assertEqual(len(self.bob.inbox()), 1)

    def test_untrust_cancels_crashed_claim_on_worker_recovery(self):
        message_id = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        with patch("courier.http_json", side_effect=RuntimeError("Simulated crash.")):
            with self.assertRaises(RuntimeError):
                self.alice.send_once()
        self.alice.untrust("bob")
        self.alice.recover_outbound()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "cancelled")

    def test_person_sources_cannot_be_forwarded_automatically(self):
        message_id = self.alice.queue.enqueue("person:carol", "person:bob", "forged relay")
        with patch("courier.http_json") as request:
            self.alice.send_once()
        request.assert_not_called()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "failed")

    def test_untrust_does_not_reclassify_already_delivered_inbound(self):
        receipt = self.receive()
        self.bob.approve(receipt["id"], "wsl:ses_example")
        with self.bob.transaction():
            self.bob.db.execute("UPDATE fake_messages SET state='sending' WHERE id=?", (receipt["id"],))
            self.bob.queue.mark(receipt["id"], "delivered", {"accepted": True})
        self.bob.untrust("alice")
        self.assertEqual(self.bob.queue.get(receipt["id"])["state"], "delivered")
        self.assertEqual(self.bob.db.execute("SELECT state FROM courier_inbound WHERE id=?", (receipt["id"],)).fetchone()[0], "approved")

    def test_poll_and_reply_suggestion_are_contact_scoped(self):
        outgoing = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "question")
        self.alice.send_once()
        remote_id = json.loads(self.alice.queue.get(outgoing)["result"])["remote_id"]
        self.bob.approve(remote_id, "wsl:ses_other")
        self.now += 61
        self.alice.poll_once()
        self.assertEqual(self.alice.db.execute("SELECT state FROM courier_sent WHERE id=?", (outgoing,)).fetchone()[0], "approved")
        reply = self.bob.queue.enqueue("wsl:ses_other", "person:alice", "answer", reply_to=remote_id)
        self.bob.send_once()
        self.assertEqual(self.bob.queue.get(reply)["state"], "delivered")
        self.assertEqual(self.alice.inbox()[0]["suggested_target"], "wsl:ses_example")
        stranger = self.alice.invite("carol")
        self.alice.receive("Bearer " + stranger["token"], {"text": "forged", "idempotency_key": "foreign", "reply_to": remote_id})
        foreign = next(row for row in self.alice.inbox() if row["person"] == "carol")
        self.assertIsNone(foreign["suggested_target"])

    def test_poll_backlog_is_bounded_to_one_probe_per_tick(self):
        for source in ("wsl:ses_first", "wsl:ses_second"):
            self.alice.queue.enqueue(source, "person:bob", "hello")
            self.alice.send_once()
        self.now += 61
        with patch("courier.http_json", side_effect=BridgeError("Peer unavailable.")) as http:
            self.alice.poll_once()
            http.assert_called_once()
            self.alice.poll_once()
            self.assertEqual(http.call_count, 2)
            self.alice.poll_once()
            self.assertEqual(http.call_count, 2)

    def peer(self, app, name):
        row = app.db.execute("SELECT last_seen, last_error, last_error_at FROM courier_peers WHERE contact=?",
                             (name,)).fetchone()
        return tuple(row) if row else None

    def test_contacts_record_when_they_last_answered_and_why_they_failed(self):
        self.assertIsNone(self.peer(self.alice, "bob"))
        self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        with patch("courier.http_json", side_effect=BridgeError("HTTP request failed (Connection\x1b[2JRefusedError).")):
            self.alice.send_once()
        self.assertEqual(self.peer(self.alice, "bob"),
                         (None, "HTTP request failed (Connection[2JRefusedError).", self.now))
        self.now += 5
        self.alice.send_once()
        self.assertEqual(self.peer(self.alice, "bob")[0], self.now)
        self.assertEqual(self.peer(self.bob, "alice")[0], self.now)  # The receiver saw alice too.

    def test_waiting_uploads_cost_no_writes_until_their_retry_is_due(self):
        self.assertIsNone(self.alice.send_once())
        self.alice.queue.enqueue("wsl:ses_example", "person:bob", "hello")
        with patch("courier.http_json", side_effect=BridgeError("Peer unavailable.")):
            self.alice.send_once()
        statements = []
        self.alice.db.set_trace_callback(statements.append)
        self.addCleanup(self.alice.db.set_trace_callback, None)
        for _ in range(5):
            self.assertIsNone(self.alice.send_once())
        self.assertEqual([sql for sql in statements if not sql.lstrip().upper().startswith("SELECT")], [])
        fresh = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "A new message is not held back")
        self.assertEqual(self.alice.send_once(), fresh)

    def test_receipts_of_a_removed_contact_do_not_block_polling_others(self):
        carol = self.make_courier("carol")
        self.serve(carol)
        for_carol, for_alice = self.alice.invite("carol"), carol.invite("alice")
        for_carol["url"], for_alice["url"] = self.alice_url, self.carol_url
        self.alice.trust("carol", for_alice)
        carol.trust("alice", for_carol)
        for target in ("person:bob", "person:carol"):
            self.alice.queue.enqueue("wsl:ses_example", target, "hello")
            self.alice.send_once()
        with self.alice.transaction():  # The removed contact's receipt is first in line.
            self.alice.db.execute("UPDATE courier_sent SET next_attempt=next_attempt-1 WHERE contact='bob'")
        self.alice.untrust("bob")
        self.now += 61
        with patch("courier.http_json", side_effect=BridgeError("Peer unavailable.")) as http:
            for _ in range(2):
                self.alice.poll_once()
        self.assertEqual(len(http.call_args_list), 1)
        self.assertTrue(http.call_args.args[0].startswith(self.carol_url + "/v1/messages/"))

    def test_contact_rate_and_held_limits(self):
        with patch("courier.RATE", 2):
            self.receive(key="one")
            self.receive(key="two")
            with self.assertRaises(HttpError) as caught:
                self.receive(key="three")
            self.assertEqual(caught.exception.status, 429)
        self.now += 61
        with patch("courier.HELD_LIMIT", 2):
            with self.assertRaises(HttpError) as caught:
                self.receive(key="three")
            self.assertEqual(caught.exception.status, 429)

    def test_invalid_http_requests_also_consume_contact_rate_limit(self):
        with patch("courier.RATE", 1):
            with self.assertRaises(HttpError) as invalid:
                http_json(self.bob_url + "/v1/messages", method="POST", headers=self.headers(), data={})
            self.assertEqual(invalid.exception.status, 400)
            with self.assertRaises(HttpError) as limited:
                self.receive()
            self.assertEqual(limited.exception.status, 429)

    def test_sanitized_inbox_keeps_raw_user_content_out_of_terminal(self):
        self.receive(text="hola\x1b[31mred\x1b[0m\x1b]0;bad title\x07\n\r\x00\x7f\u202e\u009bworld")
        row = self.bob.inbox()[0]
        self.assertEqual(row["text"], "holaredworld")
        self.assertIn("\x1b", self.bob.queue.get(row["id"])["payload"]["text"])

    def test_http_authenticates_before_body_read(self):
        # No body is sent despite the advertised length; a 401 must arrive now,
        # rather than waiting for the body or socket timeout.
        with socket.create_connection(self.bob_server.server_address, timeout=1) as connection:
            connection.sendall(b"POST /v1/messages HTTP/1.0\r\nContent-Length: 100\r\n\r\n")
            response = connection.recv(4096)
        self.assertIn(b"401", response)

    def test_rejected_request_body_is_read_before_the_connection_closes(self):
        # Closing with unread bytes makes the kernel send a reset, which a proxy can
        # forward instead of the response; the server reads late bytes before closing.
        drained = []

        class Recording(courier.CourierServer):
            def drain(self, request):
                drained.append(super().drain(request))
                return drained[-1]

        server = Recording(self.bob, "127.0.0.1:0")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with socket.create_connection(server.server_address, timeout=2) as connection:
            connection.sendall(b"POST /v1/messages HTTP/1.0\r\nContent-Length: 20\r\n\r\n")
            status = b""
            while chunk := connection.recv(4096):  # Until the server shuts down its side.
                status += chunk
            connection.sendall(b"x" * 20)
            connection.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + 3
        while not drained and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(b"401", status)
        self.assertEqual(drained, [20])

    def test_http_size_transfer_encoding_and_duplicate_lengths_rejected(self):
        for extra, status in ((f"Content-Length: {courier.MAX_BODY + 1}\r\n", 413),
                              ("Content-Length: 1\r\nContent-Length: 1\r\n", 411),
                              ("Transfer-Encoding: chunked\r\n", 400)):
            with self.subTest(status=status), socket.create_connection(self.bob_server.server_address, timeout=1) as connection:
                request = ("POST /v1/messages HTTP/1.0\r\nAuthorization: Bearer " + self.bob_card["token"] + "\r\n" + extra + "\r\n")
                connection.sendall(request.encode())
                self.assertIn(str(status).encode(), connection.recv(4096))

    def test_invalid_payloads_are_rejected(self):
        for body in ({}, [], {"text": "", "idempotency_key": "empty"},
                     {"text": "x", "idempotency_key": "bad/key"},
                     {"text": "x", "idempotency_key": "key", "reply_to": "invalid"},
                     {"text": "x", "idempotency_key": "key", "source": "person:carol"}):
            with self.subTest(body=body), self.assertRaises(HttpError) as caught:
                http_json(self.bob_url + "/v1/messages", method="POST", headers=self.headers(), data=body)
            self.assertEqual(caught.exception.status, 400)

    def test_http_errors_carry_the_servers_short_reason(self):
        with self.assertRaises(HttpError) as caught:
            http_json(self.bob_url + "/v1/whoami", headers={"Authorization": "Bearer " + "X" * 43})
        self.assertEqual((caught.exception.status, caught.exception.detail), (401, "Authentication required."))
        self.assertIn("401: Authentication required.", str(caught.exception))
        with patch("transports.http.client.HTTPConnection") as constructor:
            response = constructor.return_value.getresponse.return_value
            response.status = 500
            response.read.return_value = json.dumps({"error": "bad\x1b[31m‮" + "x" * 300}).encode()
            with self.assertRaises(HttpError) as caught:
                http_json("http://192.0.2.10:8765/v1/whoami")
            response.read.assert_called_once_with(1024)
        self.assertTrue(caught.exception.detail.startswith("bad[31mx"))
        self.assertEqual(len(caught.exception.detail), 200)

    def test_http_helper_does_not_follow_redirects_or_expose_token(self):
        response = Mock(status=302)
        with patch("transports.http.client.HTTPConnection") as constructor:
            constructor.return_value.getresponse.return_value = response
            with self.assertRaises(HttpError) as caught:
                http_json("http://192.0.2.10:8765/redirect", headers=self.headers())
            self.assertEqual(caught.exception.status, 302)
            self.assertNotIn(self.bob_card["token"], str(caught.exception))
            response.read.assert_not_called()
            constructor.return_value.request.assert_called_once()

    def test_http_helper_bounds_output_and_redacts_network_exceptions(self):
        with patch("transports.http.client.HTTPConnection") as constructor:
            response = constructor.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b"x" * 11
            with self.assertRaisesRegex(BridgeError, "response exceeds"):
                http_json("http://192.0.2.10/example", maximum=10)
            response.read.assert_called_once_with(11)
            constructor.return_value.close.assert_called_once()
        with patch("transports.http.client.HTTPConnection") as constructor:
            constructor.return_value.request.side_effect = TimeoutError(self.bob_card["token"])
            with self.assertRaises(BridgeError) as caught:
                http_json("http://192.0.2.10/example", headers=self.headers())
            self.assertNotIn(self.bob_card["token"], str(caught.exception))
            self.assertIn("TimeoutError", str(caught.exception))
            constructor.return_value.request.assert_called_once()

    def test_token_rotation_and_bilateral_consent(self):
        replacement = self.bob.invite("alice")
        with self.assertRaises(HttpError) as revoked:
            self.receive()
        self.assertEqual(revoked.exception.status, 401)
        self.assertEqual(http_json(self.bob_url + "/v1/whoami", headers=self.headers(replacement)),
                         {"person": "bob", "protocol": 1})
        # A card issued to Carol grants Carol inbound access, but does not install
        # a Carol outbound card or let this courier send to Carol.
        self.bob.invite("carol")
        message_id = self.bob.queue.enqueue("wsl:ses_example", "person:carol", "hello")
        with patch("courier.http_json") as request:
            self.bob.send_once()
        request.assert_not_called()
        self.assertEqual(self.bob.queue.get(message_id)["state"], "failed")


class WorkerLoopTests(unittest.TestCase):
    def loop(self, ticks_before_stop, **failures):
        stop, server, worker, ticks = threading.Event(), Mock(), MagicMock(), []

        def handle():
            ticks.append(1)
            if len(ticks) == ticks_before_stop:
                stop.set()

        server.handle_request.side_effect = handle
        for name, side_effect in failures.items():
            getattr(worker, name).side_effect = side_effect
        return stop, server, worker, ticks

    def test_a_failing_tick_is_logged_and_the_worker_keeps_going(self):
        stop, server, worker, ticks = self.loop(3, send_once=[sqlite3.OperationalError("database is locked"), 0, 0])
        with patch("courier.log") as log:
            courier.serve_until(worker, server, stop, backoff=lambda failures: 0)
        self.assertEqual(len(ticks), 3)
        log.assert_called_once()
        self.assertIn("database is locked", log.call_args.args[0])
        worker.recover_outbound.assert_called_once()  # Owned claims are released again.

    def test_unexpected_errors_are_not_swallowed(self):
        stop, server, worker, _ = self.loop(5, poll_once=ValueError("a bug"))
        with patch("courier.log"), self.assertRaises(ValueError):
            courier.serve_until(worker, server, stop, backoff=lambda failures: 0)


class CourierConfigurationTests(unittest.TestCase):
    def test_wildcards_require_explicit_flag(self):
        for address in (str(__import__("ipaddress").ip_address(0)) + ":8765", "[::]:8765"):
            with self.subTest(address=address), self.assertRaises(BridgeError):
                courier.listen_address(address)
            self.assertEqual(courier.listen_address(address, True)[1], 8765)
        for address in ("localhost:8765", "192.0.2.10:-1", "192.0.2.10:65536"):
            with self.subTest(address=address), self.assertRaises(BridgeError):
                courier.listen_address(address)

    def test_contact_urls_and_names_are_strict(self):
        for url in ("file:///example", "http://user:password@192.0.2.10", "http://192.0.2.10/path",
                    "http://192.0.2.10#fragment", "http://192.0.2.10:0", "http://192.0.2.10\n"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                courier.contact_url(url)
        for name in ("Bob", "alice\n", "a/b", "", "../alice"):
            with self.subTest(name=name), self.assertRaises(BridgeError):
                courier.person(name)

    def test_config_permissions_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory(prefix="courier-config-") as folder:
            path = Path(folder) / "courier.json"
            courier.write_config(path, configuration())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(courier.read_config(path), configuration())
            path.chmod(0o644)
            with self.assertRaises(BridgeError):
                courier.read_config(path)
            with self.assertRaises(BridgeError):
                courier.write_config(path, configuration())
            path.chmod(0o600)
            link = Path(folder) / "linked.json"
            link.symlink_to(path)
            with self.assertRaises(BridgeError):
                courier.read_config(link)

    def test_revoked_contact_cannot_be_resurrected_by_stale_config_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="courier-restart-") as folder:
            path = Path(folder) / "courier.json"
            queue = FakeQueue(Path(folder) / "queue.sqlite3")
            self.addCleanup(queue.db.close)
            courier.write_config(path, configuration())
            app = courier.Courier(queue, courier.read_config(path), config_path=path)
            card = app.invite("bob")
            snapshot = courier.read_config(path)
            self.assertEqual(snapshot["tokens"]["bob"], hashlib.sha256(card["token"].encode()).hexdigest())
            self.assertNotIn(card["token"], path.read_text())
            with patch.object(app, "sync_config", side_effect=OSError("Simulated interrupted snapshot write.")):
                with self.assertRaises(OSError):
                    app.untrust("bob")
            restarted = courier.Courier(queue, snapshot, config_path=path)
            self.assertEqual(restarted.contacts(), [])
            with self.assertRaises(courier.RequestError):
                restarted.authenticate("Bearer " + card["token"])
            self.assertEqual(courier.read_config(path)["tokens"], {})

    def test_confirmation_requires_both_ttys_and_fresh_five_letter_code(self):
        for input_tty, output_tty in ((False, True), (True, False), (False, False)):
            with patch("sys.stdin.isatty", return_value=input_tty), patch("sys.stdout.isatty", return_value=output_tty):
                with self.assertRaises(BridgeError):
                    courier.interactive_confirmation("Approve")
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
                patch("courier.secrets.choice", side_effect=list("ABCDE")), patch("builtins.input", return_value="ABCDE"), \
                patch("builtins.print") as output:
            courier.interactive_confirmation("Approve")
            self.assertIn("ABCDE", output.call_args.args[0])
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
                patch("builtins.input", return_value="incorrect"), patch("builtins.print"):
            with self.assertRaises(BridgeError):
                courier.interactive_confirmation("Approve")

    def test_cli_noninteractive_approval_denied_before_config_or_queue(self):
        for arguments in (["approve", "example", "--to", "wsl:ses_example"], ["invite", "bob"],
                          ["trust", "bob", "--card", "{}"]):
            with self.subTest(arguments=arguments), patch("sys.stdin.isatty", return_value=False), \
                    patch("sys.stderr", new_callable=io.StringIO), patch("courier.read_config") as read:
                self.assertEqual(courier.main(arguments, queue_factory=Mock()), 1)
                read.assert_not_called()

    def test_real_queue_factory_uses_local_mailbox_and_unchanged_environment(self):
        with tempfile.TemporaryDirectory(prefix="courier-cli-") as folder:
            path = Path(folder) / "courier.json"
            import os
            from local_bridge import LocalMailbox
            with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "wsl",
                                         "OPENCODE_CONNECT_LOCAL_STATE_HOME": folder}):
                queue = courier.real_queue(path)
                try:
                    self.assertIsInstance(queue, LocalMailbox)
                    self.assertEqual(queue.node, "wsl")
                    self.assertEqual(queue.state.parent, Path(folder) / "opencode-connect/local")
                finally:
                    queue.db.close()

    def test_injected_cli_invite_trust_inbox_and_approval(self):
        with tempfile.TemporaryDirectory(prefix="courier-cli-contract-") as folder:
            path = Path(folder) / "courier.json"
            database = Path(folder) / "queue.sqlite3"
            courier.write_config(path, configuration())
            factory = lambda config_path: FakeQueue(database)
            def command(*arguments):
                with patch("courier.interactive_confirmation") as confirm, \
                        patch("sys.stdout", new_callable=io.StringIO) as output:
                    result = courier.main(["--config", str(path), *arguments], queue_factory=factory)
                self.assertEqual(result, 0)
                return output.getvalue(), confirm
            output, confirmation = command("invite", "bob")
            card = json.loads(output)
            confirmation.assert_called_once()
            self.assertNotIn(card["token"], path.read_text())
            incoming = {"person": "bob", "url": "http://192.0.2.20:8765", "token": "X" * 43}
            with patch("getpass.getpass", return_value=json.dumps(incoming)) as prompt:
                command("trust", "bob", "--card", "-")
            prompt.assert_called_once()
            queue = factory(path)
            try:
                app = courier.Courier(queue, courier.read_config(path))
                receipt = app.receive("Bearer " + card["token"], {"text": "hello\x1b[31m", "idempotency_key": "cli"})
            finally:
                queue.db.close()
            inbox, confirmation = command("inbox")
            self.assertEqual(json.loads(inbox)["text"], "hello")
            confirmation.assert_not_called()
            _, confirmation = command("approve", receipt["id"], "--to", "wsl:ses_example")
            self.assertIn("wsl:ses_example", confirmation.call_args.args[0])
            queue = factory(path)
            try:
                self.assertEqual(queue.get(receipt["id"])["state"], "pending")
                self.assertEqual(queue.get(receipt["id"])["target"], "wsl:ses_example")
            finally:
                queue.db.close()

    def test_cli_help_without_config_or_network(self):
        result = subprocess.run([sys.executable, "-B", str(Path(courier.__file__)), "--help"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("consent-gated", result.stdout)


class RealQueueCourierTests(unittest.TestCase):
    """Real v2 mailboxes, independent connections, and mocked harness/HTTP effects."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="courier-real-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.now = 1000.0
        self.a_root, self.b_root = self.root / "alice", self.root / "bob"
        self.a_box = local_bridge.LocalMailbox(self.a_root, node="wsl")
        self.b_box = local_bridge.LocalMailbox(self.b_root, node="linux")
        self.addCleanup(self.a_box.db.close)
        self.addCleanup(self.b_box.db.close)
        self.alice = courier.Courier(self.a_box, configuration("alice"), clock=lambda: self.now)
        self.bob = courier.Courier(self.b_box, configuration("bob"), clock=lambda: self.now)
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
        outcome = local_bridge.local_relay_once(box, adapter)
        return outcome, adapter

    def test_mcp_courier_human_approval_local_relay_and_correlated_reply(self):
        outgoing = local_mcp_server.call_tool({"name": "send", "arguments": {
            "to": "person:bob", "text": "hello"}, "_meta": {"ai.opencode/sessionID": "ses_example"}},
            root=self.a_root, node="wsl")
        def receive_at_bob(url, **kwargs):
            return self.bob.receive(kwargs["headers"]["Authorization"], kwargs["data"])
        with patch("courier.http_json", side_effect=receive_at_bob):
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
        with patch("courier.http_json", side_effect=receive_at_alice):
            self.bob.send_once()
        inbound = self.alice.inbox()[0]
        self.assertEqual(inbound["suggested_target"], "wsl:ses_example")
        self.assertEqual(self.a_box.get(inbound["id"])["state"], "held")
        self.alice.approve(inbound["id"], "wsl:ses_example")
        self.assertEqual(self.relay_mock(self.a_box)[0]["state"], "delivered")
        self.assertEqual(self.b_box.get(reply["id"])["state"], "delivered")
        self.assertFalse(self.a_box.db.in_transaction)
        self.assertFalse(self.b_box.db.in_transaction)

    def test_uploads_fail_after_the_retention_period_and_retry_before_it(self):
        young = self.a_box.enqueue("ses_example", "person:bob", "Still worth trying")["id"]
        self.now = time.time()
        with patch("courier.http_json", side_effect=BridgeError("HTTP request failed (TimeoutError).")):
            self.alice.send_once()
            self.assertEqual(self.a_box.get(young)["state"], "pending")
            self.now = time.time() + courier.RETENTION + 10
            self.alice.send_once()
        old = self.a_box.get(young)
        self.assertEqual(old["state"], "failed")
        self.assertEqual(json.loads(old["result"])["reason"],
                         "Unreachable for 7 days: HTTP request failed (TimeoutError).")

    def test_expiry_and_rejection_use_real_held_transitions(self):
        expired, rejected = self.receive("expires"), self.receive("rejects")
        self.bob.reject(rejected["id"])
        self.now += courier.RETENTION + 1
        self.assertEqual(self.bob.inbox(), [])
        self.assertEqual(self.b_box.get(expired["id"])["state"], "rejected")
        self.assertEqual(json.loads(self.b_box.get(expired["id"])["result"])["reason"], "Retention expired.")
        self.assertEqual(self.bob.remote_status("Bearer " + self.b_card["token"], expired["id"])["state"], "expired")

    def test_real_queue_crash_recovery_is_idempotent(self):
        job = self.a_box.enqueue("wsl:ses_example", "person:bob", "hello")
        def receive_then_crash(url, **kwargs):
            self.bob.receive(kwargs["headers"]["Authorization"], kwargs["data"])
            raise RuntimeError("Simulated crash after remote acceptance.")
        with patch("courier.http_json", side_effect=receive_then_crash), self.assertRaises(RuntimeError):
            self.alice.send_once()
        self.assertEqual(self.a_box.get(job["id"])["state"], "sending")
        # Reopen the worker connection to exercise persistent metadata, not memory.
        recovered_box = local_bridge.LocalMailbox(self.a_root, node="wsl")
        self.addCleanup(recovered_box.db.close)
        recovered = courier.Courier(recovered_box, configuration("alice"), clock=lambda: self.now)
        recovered.recover_outbound()
        with patch("courier.http_json", side_effect=lambda url, **kw: self.bob.receive(
                kw["headers"]["Authorization"], kw["data"])):
            recovered.send_once()
        self.assertEqual(self.a_box.get(job["id"])["state"], "delivered")
        self.assertEqual(len(self.bob.inbox()), 1)

    def test_foreign_contract_fails_before_courier_writes(self):
        other = local_bridge.LocalMailbox(self.b_root, node="other")
        self.addCleanup(other.db.close)
        before = self.b_box.db.execute("SELECT data FROM courier_settings").fetchall()[0][0]
        with self.assertRaisesRegex(BridgeError, "configuration mismatch"):
            courier.Courier(other, configuration("bob"))
        self.assertEqual(self.b_box.db.execute("SELECT data FROM courier_settings").fetchall()[0][0], before)
        self.assertFalse(other.db.in_transaction)

    def test_cli_approval_and_worker_use_independent_connections(self):
        receipt = self.receive()
        config_path = self.root / "courier.json"
        courier.write_config(config_path, self.bob.settings())
        ready, approved = threading.Event(), threading.Event()
        results, failures = [], []
        def worker():
            box = local_bridge.LocalMailbox(self.b_root, node="linux")
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
            real_courier = courier.Courier
            with patch("courier.interactive_confirmation"), patch("sys.stdout", new_callable=io.StringIO), \
                    patch("courier.Courier", side_effect=lambda queue, config, **kw:
                          real_courier(queue, config, clock=lambda: self.now, **kw)):
                result = courier.main(["--config", str(config_path), "approve", receipt["id"],
                                       "--to", "linux:ses_example"],
                                      queue_factory=lambda path: local_bridge.LocalMailbox(self.b_root, node="linux"))
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
            box = local_bridge.LocalMailbox(self.b_root, node="linux")
            try:
                app = courier.Courier(box, configuration("bob"))
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
            box = local_bridge.LocalMailbox(self.b_root, node="linux")
            try:
                app = courier.Courier(box, configuration("bob"))
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
        with self.assertRaises(courier.RequestError) as denied:
            self.receive("after-revocation")
        self.assertEqual(denied.exception.status, 401)


class CourierDoctorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="courier-doctor-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "courier.json"
        self.queue_root = self.root / "queue"
        self.config = {**configuration(), "listen": "127.0.0.1:8765"}
        courier.write_config(self.path, self.config)
        self.box = local_bridge.LocalMailbox(self.queue_root, node="wsl")
        self.addCleanup(self.box.db.close)
        (self.box.state / "mail.sqlite3").chmod(0o600)
        self.app = courier.Courier(self.box, self.config, config_path=self.path, clock=lambda: 1000)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_offline_doctor_is_readonly_and_never_opens_a_worker_or_socket(self):
        before = self.snapshot()
        with patch("courier.real_queue") as factory, patch("courier.CourierServer") as server, \
                patch("courier.http_json") as http, patch("courier.socket.create_connection") as tcp:
            result = courier.doctor(self.path, root=self.queue_root, network=False)
        self.assertFalse(any(row["status"] == "FAIL" for row in result))
        for effect in (factory, server, http, tcp):
            effect.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_missing_config_and_queue_do_not_create_directories(self):
        missing = self.root / "not-created/courier.json"
        result = courier.doctor(missing, root=self.root / "new-queue", network=False)
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertFalse(missing.parent.exists())
        with patch("courier.local_queue_root", return_value=self.root / "new-queue"), \
                patch("sys.stdout", new_callable=io.StringIO), patch("courier.real_queue") as factory:
            self.assertEqual(courier.main(["--config", str(self.path), "doctor", "--offline"]), 0)
        self.assertFalse((self.root / "new-queue").exists())
        factory.assert_not_called()

    def test_doctor_reports_held_expiry_due_and_retry_without_changing_rows(self):
        card = self.app.invite("bob")
        self.app.trust("bob", {**card, "person": "bob"})
        inbound = self.app.receive("Bearer " + card["token"], {"text": "held", "idempotency_key": "expiry"})
        outgoing = self.box.enqueue("wsl:ses_example", "person:bob", "outgoing")
        with patch("courier.http_json", side_effect=BridgeError("Offline.")):
            self.app.send_once()
        before = self.snapshot()
        result = courier.doctor(self.path, root=self.queue_root, network=False, clock=lambda: 1000 + courier.RETENTION + 1)
        summary = next(row for row in result if row["area"] == "Courier state")
        self.assertEqual((summary["held"], summary["expiry_due"], summary["retry_due"]), (1, 1, 1))
        self.assertEqual(self.box.get(inbound["id"])["state"], "held")
        self.assertEqual(self.box.get(outgoing["id"])["state"], "pending")
        self.assertEqual(self.snapshot(), before)

    def test_doctor_checks_listener_and_contact_identity_without_echoing_credentials(self):
        token = "X" * 43
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": token})
        with patch("courier.socket.create_connection") as tcp, patch("courier.http_json", return_value={
                "person": "bob", "protocol": 1}) as http:
            result = courier.doctor(self.path, root=self.queue_root)
        self.assertTrue(all(row["status"] == "OK" for row in result))
        tcp.assert_called_once_with(("127.0.0.1", 8765), timeout=courier.SOCKET_TIMEOUT)
        self.assertEqual(http.call_args.kwargs["headers"], {"Authorization": "Bearer " + token})
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn("192.0.2.20", json.dumps(result))

    def test_doctor_distinguishes_bad_contact_and_inactive_listener(self):
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "X" * 43})
        for response in ({"person": "carol", "protocol": 1}, BridgeError("Private remote error.")):
            with self.subTest(response_type=type(response).__name__), \
                    patch("courier.socket.create_connection", side_effect=ConnectionRefusedError), \
                    patch("courier.http_json", **({"side_effect": response} if isinstance(response, Exception)
                                                 else {"return_value": response})):
                result = courier.doctor(self.path, root=self.queue_root)
            self.assertEqual(next(row for row in result if row["area"] == "Listener")["status"], "FAIL")
            self.assertEqual(next(row for row in result if row["area"] == "Contact")["status"], "FAIL")
            self.assertNotIn("Private remote error", json.dumps(result))

    def test_insecure_state_stops_network_probes(self):
        (self.box.state / "mail.sqlite3").chmod(0o644)
        with patch("courier.socket.create_connection") as tcp, patch("courier.http_json") as http:
            result = courier.doctor(self.path, root=self.queue_root)
        self.assertEqual(result[-1]["status"], "FAIL")
        tcp.assert_not_called()
        http.assert_not_called()

    def test_stale_json_does_not_probe_revoked_contact(self):
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "X" * 43})
        with patch.object(self.app, "sync_config"):
            self.app.untrust("bob")
        before = self.snapshot()
        with patch("courier.socket.create_connection"), patch("courier.http_json") as http:
            result = courier.doctor(self.path, root=self.queue_root)
        self.assertTrue(any(row["area"] == "Snapshot" for row in result))
        http.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_readonly_state_path_matches_the_real_factory(self):
        import os
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_STATE_HOME": str(self.root)}):
            self.assertEqual(courier.local_queue_root(), self.root / "opencode-connect/local")
            self.assertFalse(courier.local_queue_root().exists())


if __name__ == "__main__":
    unittest.main()
