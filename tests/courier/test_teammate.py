"""A member's side of a team: what its owner may change, and what it refuses."""

import ipaddress
from pathlib import Path
import tempfile
import unittest

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier
from opencode_connect.courier.pairing import digest
from opencode_connect.courier.settings import RequestError
from opencode_connect.courier.teammate import introduced_url
from tests.courier.fakes import FakeQueue, configuration
from tests.courier.test_setup import LAN, VPN, ipv4

TEAMMATE = ipv4(100, 64, 0, 20)
VPN_URL = f"http://{TEAMMATE}:8765"


class TeammateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-teammate-")
        self.addCleanup(folder.cleanup)
        queue = FakeQueue(Path(folder.name) / "dave.sqlite3")
        self.addCleanup(queue.db.close)
        self.now = 1000.0
        self.dave = Courier(queue, configuration("dave"), clock=lambda: self.now)
        self.owner = "Bearer " + self.dave.invite("alice")["token"]  # Alice's courier writes with it.
        self.stranger = "Bearer " + self.dave.invite("erin")["token"]
        with self.dave.transaction():
            self.dave._join_team_locked("alice")

    def introduce(self, name="bob", *, card=True, auth=None, **extra):
        payload = {"person": name, "accept": digest("T" * 43),
                   "card": {"person": name, "url": VPN_URL, "token": "C" * 43} if card else None, **extra}
        return self.dave.team_introduce(auth or self.owner, payload)

    def refused(self, status, **kwargs):
        with self.assertRaises(RequestError) as caught:
            self.introduce(**kwargs)
        self.assertEqual(caught.exception.status, status, str(caught.exception))
        return str(caught.exception)

    def test_the_owner_introduces_a_teammate_and_a_repeat_changes_nothing(self):
        self.assertEqual(self.introduce(card=False), {"person": "dave", "changed": True})
        self.assertEqual(self.dave.settings()["tokens"]["bob"], digest("T" * 43))
        self.assertNotIn("bob", self.dave.settings()["contacts"])
        self.assertEqual(self.introduce(), {"person": "dave", "changed": True})
        self.assertEqual(self.introduce(), {"person": "dave", "changed": False})
        self.assertEqual(self.dave.settings()["contacts"]["bob"]["url"], VPN_URL)
        self.assertEqual(self.dave.introduced(), {"bob"})

    def test_only_the_owner_may_introduce_or_remove(self):
        self.refused(410, auth=self.stranger)
        self.refused(401, auth="Bearer " + "X" * 43)
        with self.assertRaises(RequestError) as caught:
            self.dave.team_remove(self.stranger, {"person": "bob"})
        self.assertEqual(caught.exception.status, 410)
        self.assertNotIn("bob", self.dave.settings()["tokens"])

    def test_invalid_introductions_are_refused_before_anything_changes(self):
        for payload in ({}, {"person": "bob", "accept": "0" * 64}, [],
                        {"person": "bob", "accept": "0" * 64, "card": None, "extra": 1},
                        {"person": "Bob!", "accept": "0" * 64, "card": None},
                        {"person": "bob", "accept": "short", "card": None},
                        {"person": "bob", "accept": "0" * 64, "card": {"person": "carol", "url": VPN_URL,
                                                                       "token": "C" * 43}},
                        {"person": "bob", "accept": "0" * 64, "card": None, "told": "ftp://x"}):
            with self.subTest(payload=payload), self.assertRaises(RequestError) as caught:
                self.dave.team_introduce(self.owner, payload)
            self.assertEqual(caught.exception.status, 400)
        self.refused(400, name="dave")
        self.refused(400, name="alice")
        self.assertEqual(self.dave.introduced(), set())

    def test_an_introduced_courier_must_be_on_a_vpn_or_lan_ip(self):
        for url in (VPN_URL, f"http://{LAN}:8765", f"http://{ipv4(10, 0, 0, 5)}:8765",
                    f"http://[{ipaddress.ip_address((0xFD << 120) + 10)}]:8765"):
            with self.subTest(url=url):
                self.assertEqual(introduced_url(url), url)
        for url in ("http://203.0.113.5:8765", "http://127.0.0.1:8765", "http://example.com:8765",
                    f"http://{ipv4(169, 254, 1, 1)}:8765"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                introduced_url(url)
        payload = {"person": "bob", "accept": "0" * 64,
                   "card": {"person": "bob", "url": "http://203.0.113.5:8765", "token": "C" * 43}}
        with self.assertRaises(RequestError) as caught:
            self.dave.team_introduce(self.owner, payload)
        self.assertEqual(caught.exception.status, 400)

    def test_own_contacts_and_names_unpaired_by_hand_are_refused(self):
        self.dave.trust("bob", {"person": "bob", "url": VPN_URL, "token": "B" * 43})
        self.assertIn("own contact", self.refused(409))
        self.dave.untrust("bob")
        self.assertIn("unpaired bob by hand", self.refused(409))
        with self.dave.transaction():  # Pairing with them by hand lifts it.
            self.dave._own_contact_locked("bob")
        self.introduce()

    def test_a_pairing_in_progress_with_that_name_waits(self):
        with self.dave.transaction():
            self.dave.db.execute("INSERT INTO courier_offer(id, key, expires, replacing, state, contact) "
                                 "VALUES(1, '00', ?, 0, 'provisional', 'bob')", (self.now + 600,))
        self.refused(503)

    def test_the_owner_removes_only_teammates_it_introduced(self):
        self.introduce()
        self.dave.trust("carol", {"person": "carol", "url": VPN_URL, "token": "D" * 43})
        self.assertEqual(self.dave.team_remove(self.owner, {"person": "carol"}), {"person": "dave", "changed": False})
        self.assertEqual(self.dave.team_remove(self.owner, {"person": "bob"}), {"person": "dave", "changed": True})
        config = self.dave.settings()
        self.assertNotIn("bob", config["tokens"])
        self.assertIn("carol", config["contacts"])
        self.assertEqual(self.dave.introduced(), set())
        for payload in ({}, {"person": "Bob!"}, {"person": "bob", "extra": 1}):
            with self.subTest(payload=payload), self.assertRaises(RequestError):
                self.dave.team_remove(self.owner, payload)

    def test_leaving_keeps_introduced_contacts_as_own_and_ends_the_owner_s_reach(self):
        self.introduce()
        self.assertEqual(self.dave.leave_team(), "alice")
        self.assertIsNone(self.dave.owner())
        self.assertIn("bob", self.dave.settings()["contacts"])
        self.assertEqual(self.dave.introduced(), set())
        self.refused(410)
        with self.assertRaisesRegex(BridgeError, "not in a team"):
            self.dave.leave_team()

    def test_unpairing_the_owner_ends_the_membership(self):
        self.introduce()
        self.dave.untrust("alice")
        self.assertIsNone(self.dave.owner())
        self.assertIn("bob", self.dave.settings()["contacts"])

    def test_a_teammate_that_moved_before_its_card_arrived_keeps_its_new_url(self):
        self.introduce(card=False)
        moved = "http://127.0.0.1:9000"
        token = "Bearer " + "T" * 43
        self.assertEqual(self.dave.update_contact_url(token, {"url": moved}, "127.0.0.1"),
                         {"person": "dave", "updated": False})
        self.introduce()
        self.assertEqual(self.dave.settings()["contacts"]["bob"]["url"], moved)
        self.dave.update_contact_url(token, {"url": VPN_URL}, TEAMMATE)  # Its card exists now.
        self.assertEqual(self.dave.settings()["contacts"]["bob"]["url"], VPN_URL)

    def test_told_an_older_url_the_teammate_is_told_the_current_one(self):
        self.introduce(card=False)
        self.introduce(told=f"http://{VPN}:8765")
        self.assertEqual(self.dave.db.execute("SELECT contact, url, state FROM courier_announce").fetchall(),
                         [("bob", self.dave.settings()["url"], "pending")])
        self.dave.db.execute("DELETE FROM courier_announce")
        self.introduce(told=self.dave.settings()["url"])
        self.assertEqual(self.dave.db.execute("SELECT count(*) FROM courier_announce").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
