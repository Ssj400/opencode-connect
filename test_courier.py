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
import unittest
from unittest.mock import Mock, patch
import uuid

import courier
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

    def insert_inbound(self, *, source, target, text, remote_key):
        assert self.db.in_transaction
        assert source.startswith("person:") and target is None
        message_id = "msg_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO fake_messages VALUES(?,?,?,?, 'held', NULL,?,NULL)",
                        (message_id, source, target, text, remote_key))
        return self.get(message_id)

    def enqueue(self, source, target, text, reply_to=None):
        message_id = "msg_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO fake_messages VALUES(?,?,?,?, 'pending', NULL,NULL,?)",
                        (message_id, source, target, text, reply_to))
        return message_id

    def get(self, message_id):
        columns = ("id", "source", "target", "text", "state", "result", "remote_key", "reply_to")
        row = self.db.execute("SELECT * FROM fake_messages WHERE id=?", (message_id,)).fetchone()
        return dict(zip(columns, row)) if row else None

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

    def cancel_person(self, source):
        assert self.db.in_transaction and source.startswith("person:")
        self.db.execute("""UPDATE fake_messages SET state='cancelled' WHERE
                           (source=? OR target=?) AND state IN ('held','pending')""", (source, source))


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
        self.assertEqual(self.bob.queue.get(second["id"])["state"], "expired")
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
            self.alice.queue.mark(local, "sending", {})
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
        self.assertIn("\x1b", self.bob.queue.get(row["id"])["text"])

    def test_http_authenticates_before_body_read(self):
        # No body is sent despite the advertised length; a 401 must arrive now,
        # rather than waiting for the body or socket timeout.
        with socket.create_connection(self.bob_server.server_address, timeout=1) as connection:
            connection.sendall(b"POST /v1/messages HTTP/1.0\r\nContent-Length: 100\r\n\r\n")
            response = connection.recv(4096)
        self.assertIn(b"401", response)

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

    def test_cli_real_run_fails_closed_until_queue_integration(self):
        with tempfile.TemporaryDirectory(prefix="courier-cli-") as folder:
            path = Path(folder) / "courier.json"
            courier.write_config(path, configuration())
            with patch("courier.CourierServer") as server, patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(courier.main(["--config", str(path), "run"]), 1)
                self.assertIn("C0.8/C0.9", error.getvalue())
                server.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
