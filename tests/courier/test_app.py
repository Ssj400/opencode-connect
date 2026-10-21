"""Courier application logic and its HTTP listener, over a fake queue."""

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import RETENTION, Courier
from opencode_connect.courier.server import MAX_BODY, CourierServer
from opencode_connect.transports import HttpError, http_json
from tests.courier.fakes import FakeQueue, configuration


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
        return Courier(queue, configuration(name), clock=lambda: self.now)

    def serve(self, app):
        server = CourierServer(app, "127.0.0.1:0")
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
        self.now += RETENTION + 1
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
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Destination unavailable.")) as request:
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
        with patch("opencode_connect.courier.app.http_json", side_effect=accepted_then_lost):
            self.alice.send_once()
        self.assertEqual(len(self.bob.inbox()), 1)
        self.now += 3
        self.alice.send_once()
        self.assertEqual(len(self.bob.inbox()), 1)
        self.assertEqual(self.alice.queue.get(message_id)["state"], "delivered")

    def test_backoff_does_not_block_other_ready_outbound(self):
        first = self.alice.queue.enqueue("wsl:ses_example", "person:bob", "offline")
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Destination unavailable.")):
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
        with patch("opencode_connect.courier.app.http_json", side_effect=accepted_then_crashed):
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
        with patch("opencode_connect.courier.app.http_json", side_effect=RuntimeError("Simulated crash.")):
            with self.assertRaises(RuntimeError):
                self.alice.send_once()
        self.alice.untrust("bob")
        self.alice.recover_outbound()
        self.assertEqual(self.alice.queue.get(message_id)["state"], "cancelled")

    def test_person_sources_cannot_be_forwarded_automatically(self):
        message_id = self.alice.queue.enqueue("person:carol", "person:bob", "forged relay")
        with patch("opencode_connect.courier.app.http_json") as request:
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
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Peer unavailable.")) as http:
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
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("HTTP request failed (Connection\x1b[2JRefusedError).")):
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
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Peer unavailable.")):
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
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Peer unavailable.")) as http:
            for _ in range(2):
                self.alice.poll_once()
        self.assertEqual(len(http.call_args_list), 1)
        self.assertTrue(http.call_args.args[0].startswith(self.carol_url + "/v1/messages/"))

    def test_contact_rate_and_held_limits(self):
        with patch("opencode_connect.courier.app.RATE", 2):
            self.receive(key="one")
            self.receive(key="two")
            with self.assertRaises(HttpError) as caught:
                self.receive(key="three")
            self.assertEqual(caught.exception.status, 429)
        self.now += 61
        with patch("opencode_connect.courier.app.HELD_LIMIT", 2):
            with self.assertRaises(HttpError) as caught:
                self.receive(key="three")
            self.assertEqual(caught.exception.status, 429)

    def test_invalid_http_requests_also_consume_contact_rate_limit(self):
        with patch("opencode_connect.courier.app.RATE", 1):
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

        class Recording(CourierServer):
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
        for extra, status in ((f"Content-Length: {MAX_BODY + 1}\r\n", 413),
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
        with patch("opencode_connect.transports.http.client.HTTPConnection") as constructor:
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
        with patch("opencode_connect.transports.http.client.HTTPConnection") as constructor:
            constructor.return_value.getresponse.return_value = response
            with self.assertRaises(HttpError) as caught:
                http_json("http://192.0.2.10:8765/redirect", headers=self.headers())
            self.assertEqual(caught.exception.status, 302)
            self.assertNotIn(self.bob_card["token"], str(caught.exception))
            response.read.assert_not_called()
            constructor.return_value.request.assert_called_once()

    def test_http_helper_bounds_output_and_redacts_network_exceptions(self):
        with patch("opencode_connect.transports.http.client.HTTPConnection") as constructor:
            response = constructor.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b"x" * 11
            with self.assertRaisesRegex(BridgeError, "response exceeds"):
                http_json("http://192.0.2.10/example", maximum=10)
            response.read.assert_called_once_with(11)
            constructor.return_value.close.assert_called_once()
        with patch("opencode_connect.transports.http.client.HTTPConnection") as constructor:
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
        with patch("opencode_connect.courier.app.http_json") as request:
            self.bob.send_once()
        request.assert_not_called()
        self.assertEqual(self.bob.queue.get(message_id)["state"], "failed")
