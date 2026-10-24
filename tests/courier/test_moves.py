"""A courier whose address changed tells its contacts, over loopback HTTP; no VPN or network."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier
from opencode_connect.courier.server import CourierServer
from opencode_connect.courier.settings import RequestError
from opencode_connect.transports import HttpError
from tests.courier.fakes import FakeQueue, configuration

NEW = "http://127.0.0.1:9999"


class MoveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-moves-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = 1000.0
        self.alice, self.alice_url = self.start("alice")
        self.bob, self.bob_url = self.start("bob")
        self.carol, _ = self.start("carol")
        for one, other in ((self.alice, self.bob), (self.bob, self.alice), (self.alice, self.carol),
                           (self.carol, self.alice)):
            card = other.invite(one.settings()["person"])
            one.trust(other.settings()["person"], card)

    def start(self, name):
        queue = FakeQueue(self.root / f"{name}.sqlite3")
        self.addCleanup(queue.db.close)
        app = Courier(queue, configuration(name), clock=lambda: self.now)
        server = CourierServer(app, "127.0.0.1:0")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

        self.addCleanup(stop)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with app.transaction():
            config = app.settings()
            config["url"] = url
            app.save_settings(config)
        return app, url

    def moved(self, url=NEW, listen="127.0.0.1:0"):
        """Alice's `address` change: the same queue, opened again with a new address."""
        return Courier(self.alice.queue, {**self.alice.settings()}, clock=lambda: self.now,
                       readdress={"listen": listen, "url": url})

    def pending(self, app):
        return app.db.execute("SELECT contact, url, state, attempts FROM courier_announce ORDER BY contact").fetchall()

    def test_a_new_url_is_queued_for_each_contact_and_reaches_them(self):
        token = self.bob.settings()["contacts"]["alice"]["token"]
        alice = self.moved()
        self.assertEqual(self.pending(alice), [("bob", NEW, "pending", 0), ("carol", NEW, "pending", 0)])
        self.assertEqual(alice.announce_once(), "bob")
        self.assertEqual(self.bob.settings()["contacts"]["alice"], {"person": "alice", "url": NEW, "token": token})
        self.assertEqual(alice.announce_once(), "carol")
        self.assertEqual(self.pending(alice), [])
        self.assertIsNone(alice.announce_once())
        self.assertIsNotNone(alice.last_seen("bob"))

    def test_only_a_changed_url_is_announced(self):
        self.assertEqual(self.pending(self.moved(url=self.alice_url, listen="127.0.0.1:8000")), [])

    def test_the_url_must_be_the_address_the_announcement_comes_from(self):
        alice = self.moved(url="http://192.0.2.50:9000")
        self.assertEqual(alice.announce_once(), "bob")
        self.assertEqual(self.bob.settings()["contacts"]["alice"]["url"], self.alice_url)
        (contact, _, state, _), _ = self.pending(alice)
        self.assertEqual((contact, state), ("bob", "refused"))
        reason = alice.db.execute("SELECT last_error FROM courier_announce WHERE contact='bob'").fetchone()[0]
        self.assertIn("127.0.0.1", reason)

    def test_an_older_courier_is_marked_unsupported_and_failures_are_retried_later(self):
        alice = self.moved()
        with patch("opencode_connect.courier.moves.http_json", side_effect=HttpError(404, "Route not found.")):
            alice.announce_once()
        with patch("opencode_connect.courier.moves.http_json", side_effect=BridgeError("Connection refused.")):
            alice.announce_once()
            self.assertIsNone(alice.announce_once())  # Carol's retry is not due yet.
        self.assertEqual([row[2:] for row in self.pending(alice)], [("unsupported", 0), ("pending", 1)])
        self.now += 3
        self.assertEqual(alice.announce_once(), "carol")
        self.assertEqual(self.pending(alice), [("bob", NEW, "unsupported", 0)])
        self.assertEqual(self.carol.settings()["contacts"]["alice"]["url"], NEW)

    def test_a_removed_contact_or_a_newer_change_replaces_what_was_queued(self):
        alice = self.moved()
        alice.untrust("carol")
        self.assertEqual([row[0] for row in self.pending(alice)], ["bob"])
        again = self.moved(url="http://127.0.0.1:9998")
        self.assertEqual(self.pending(again), [("bob", "http://127.0.0.1:9998", "pending", 0)])

    def test_a_url_that_is_no_longer_this_couriers_is_never_sent(self):
        alice = self.moved()
        with alice.transaction():
            alice.db.execute("UPDATE courier_announce SET url='http://127.0.0.1:9997' WHERE contact='bob'")
        with patch("opencode_connect.courier.moves.http_json") as http:
            self.assertEqual(alice.announce_once(), "bob")
        http.assert_not_called()
        self.assertEqual([row[0] for row in self.pending(alice)], ["carol"])

    def test_messages_waiting_for_a_contact_go_to_its_new_url(self):
        self.moved().announce_once()
        self.bob.queue.enqueue("wsl:ses_example", "person:alice", "hello")
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Offline.")) as http:
            self.bob.send_once()
        self.assertTrue(http.call_args.args[0].startswith(NEW + "/"))

    def test_the_receiver_checks_the_request(self):
        token = "Bearer " + self.alice.settings()["contacts"]["bob"]["token"]
        for payload in ({}, {"url": NEW, "extra": 1}, {"url": "ftp://127.0.0.1"}, {"url": "http://127.0.0.1/path"}, []):
            with self.subTest(payload=payload), self.assertRaises(RequestError) as caught:
                self.bob.update_contact_url(token, payload, "127.0.0.1")
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(RequestError) as caught:
            self.bob.update_contact_url("Bearer " + "X" * 43, {"url": NEW}, "127.0.0.1")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.bob.update_contact_url(token, {"url": self.alice_url}, "127.0.0.1"),
                         {"person": "bob", "updated": False})

    def test_the_contact_rate_limit_applies(self):
        token = "Bearer " + self.alice.settings()["contacts"]["bob"]["token"]
        with patch("opencode_connect.courier.app.RATE", 1):
            self.bob.update_contact_url(token, {"url": self.alice_url}, "127.0.0.1")
            with self.assertRaises(RequestError) as caught:
                self.bob.update_contact_url(token, {"url": self.alice_url}, "127.0.0.1")
        self.assertEqual(caught.exception.status, 429)

    def test_no_card_means_nothing_to_move(self):
        dave = self.bob.invite("dave")  # Dave may write to bob, but bob has no card for dave.
        with self.assertRaises(RequestError) as caught:
            self.bob.update_contact_url("Bearer " + dave["token"], {"url": NEW}, "127.0.0.1")
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
