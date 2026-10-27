"""A team over loopback HTTP: the owner admits machines and introduces them to each other."""

import ipaddress
from itertools import permutations
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from opencode_connect.core import BridgeError
from opencode_connect.courier import team
from opencode_connect.courier.app import Courier
from opencode_connect.courier.pairing import (digest, join_offer, pairing_key, request_pairing, seal,
                                              store_pairing)
from opencode_connect.courier.server import CourierServer
from opencode_connect.courier.status import describe_contact, read_state, team_lines
from opencode_connect.courier.teammate import INTRODUCED_NETWORKS
from opencode_connect.transports import HttpError, http_json
from tests.courier.fakes import FakeQueue, configuration

NEW = "http://127.0.0.1:9999"
LOOPBACK = INTRODUCED_NETWORKS + (ipaddress.ip_network("127.0.0.0/8"),)


class TeamTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-team-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = 1000.0
        for patcher in (patch("opencode_connect.courier.pairing.KDF_ROUNDS", 1000),
                        patch("opencode_connect.courier.teammate.INTRODUCED_NETWORKS", LOOPBACK)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.apps = {name: self.start(name) for name in ("alice", "bob", "carol", "dave")}
        self.alice = self.apps["alice"]

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
        with app.transaction():
            config = app.settings()
            config["url"] = f"http://127.0.0.1:{server.server_address[1]}"
            app.save_settings(config)
        return app

    def join(self, name, *, team_link=True, replace=False):
        """`name` joins alice's team with a link from `team invite`; the members it was told of."""
        code = self.alice.team_invite(replace=replace) if team_link else self.alice.offer()
        pending = request_pairing(self.apps[name], f"{self.alice.settings()['url']}#{code}", team=True,
                                  replace=replace, attempts=1)
        store_pairing(self.apps[name], pending, replace=replace, attempts=1)
        return pending["team"]

    def check(self):
        """Nobody holds an introduced card that its contact does not accept."""
        for one, other in permutations(self.apps, 2):
            card = self.apps[one].settings()["contacts"].get(other)
            if card is not None and other in self.apps[one].introduced():
                self.assertEqual(self.apps[other].settings()["tokens"].get(one), digest(card["token"]),
                                 f"{one} holds a card that {other} does not accept")

    def drain(self, chore="introduce_once", limit=40, check=True):
        """Run a background task until it has nothing due; introductions are checked
        after each step. A removal drops a contact on one side before the other."""
        for _ in range(limit):
            if check and chore == "introduce_once":
                self.check()
            if getattr(self.alice, chore)() is None:
                if check and chore == "introduce_once":
                    self.check()
                return
        self.fail(f"{chore} never finished")

    def connected(self, one, other):
        card = self.apps[one].settings()["contacts"].get(other)
        if card is None:
            return False
        answer = http_json(card["url"] + "/v1/whoami", headers={"Authorization": "Bearer " + card["token"]})
        return answer["person"] == other

    def intro(self, one, other):
        return self.alice.db.execute("SELECT step, state, last_error, newcomer_token FROM courier_intro "
                                     "WHERE low=? AND high=?", tuple(sorted((one, other)))).fetchone()

    def test_a_newcomer_is_introduced_to_every_member_and_each_pair_works_both_ways(self):
        self.assertEqual(self.join("bob"), [])
        self.assertEqual(self.join("carol"), ["bob"])
        self.drain()
        self.assertEqual(self.join("dave"), ["bob", "carol"])
        self.assertEqual(self.apps["dave"].owner(), "alice")
        self.drain()
        self.assertEqual(self.alice.members(), ["bob", "carol", "dave"])
        for one, other in permutations(("bob", "carol", "dave"), 2):
            with self.subTest(one=one, other=other):
                self.assertTrue(self.connected(one, other))
                self.assertEqual(self.intro(one, other)[:2], (3, "done"))
                self.assertIsNone(self.intro(one, other)[3])  # No token is kept in clear once done.
        self.assertEqual(self.apps["bob"].introduced(), {"carol", "dave"})

    def test_the_worker_s_background_tasks_carry_the_team_s_work(self):
        self.join("bob")
        self.join("carol")
        self.alice.team_remove_member("carol")
        self.join("dave")
        for _ in range(20):
            if self.alice.chores_once() is None:
                break
        self.assertTrue(self.connected("bob", "dave") and self.connected("dave", "bob"))
        self.assertEqual(self.alice.db.execute("SELECT count(*) FROM courier_team_remove").fetchone()[0], 0)

    def test_a_repeated_confirmation_does_not_restart_the_introductions(self):
        self.join("bob")
        code = self.alice.team_invite()
        pending = request_pairing(self.apps["carol"], f"{self.alice.settings()['url']}#{code}", team=True, attempts=1)
        store_pairing(self.apps["carol"], pending, attempts=1)
        self.alice.introduce_once()
        token = "Bearer " + self.apps["carol"].settings()["contacts"]["alice"]["token"]
        self.alice.confirm_pairing(token, {"nonce": pending["nonce"]})  # The same confirmation again.
        self.assertEqual(self.intro("bob", "carol")[:2], (2, "pending"))

    def test_pair_and_join_each_refuse_the_other_s_link(self):
        code = self.alice.team_invite()
        with self.assertRaises(HttpError) as caught:
            request_pairing(self.apps["bob"], f"{self.alice.settings()['url']}#{code}", attempts=1)
        self.assertIn("courier.py join LINK", str(caught.exception))
        self.alice.close_offer()
        with self.assertRaises(HttpError) as caught:
            self.join("bob", team_link=False)
        self.assertIn("courier.py pair LINK", str(caught.exception))
        self.assertIsNone(self.apps["bob"].owner())

    def test_a_lost_answer_is_sent_again_and_changes_nothing_twice(self):
        self.join("bob")
        self.join("carol")

        def lost(*args, **kwargs):
            http_json(*args, **kwargs)
            raise BridgeError("Connection reset.")

        with patch("opencode_connect.courier.team.http_json", side_effect=lost):
            self.alice.introduce_once()
        self.assertEqual(self.intro("bob", "carol")[:3], (1, "pending", "Connection reset."))
        self.assertIsNone(self.alice.introduce_once())  # Not due yet.
        self.now += 2
        self.drain()
        self.assertTrue(self.connected("bob", "carol") and self.connected("carol", "bob"))

    def test_an_unreachable_member_is_introduced_once_it_answers(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()  # Step 1, at carol.
        with patch("opencode_connect.courier.team.http_json", side_effect=BridgeError("Connection refused.")):
            for _ in range(3):
                self.now += 300
                self.alice.introduce_once()
        step, state, error, _ = self.intro("bob", "carol")
        self.assertEqual((step, state, error), (2, "pending", "Connection refused."))
        self.check()
        self.now += 300
        self.drain()
        self.assertTrue(self.connected("bob", "carol"))

    def test_an_introduction_unfinished_after_30_days_is_undone(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()  # Carol now accepts bob's token.
        self.now += team.INTRO_SECONDS + 1
        self.alice.introduce_once()
        self.assertEqual(self.intro("bob", "carol")[1], "expired")
        self.drain("remove_once")
        self.assertNotIn("bob", self.apps["carol"].settings()["tokens"])

    def test_a_member_s_own_contact_or_a_name_it_unpaired_is_never_introduced(self):
        bob, carol, dave = (self.apps[name] for name in ("bob", "carol", "dave"))
        own = {"person": "dave", "url": dave.settings()["url"], "token": "D" * 43}
        bob.trust("dave", own)  # Bob already writes to dave by hand.
        carol.trust("dave", {**own, "token": "E" * 43})
        carol.untrust("dave")  # And carol unpaired dave by hand.
        for name in ("bob", "carol", "dave"):
            self.join(name)
        self.drain()
        self.drain("remove_once")
        self.assertEqual(bob.settings()["contacts"]["dave"], own)
        self.assertEqual(self.intro("bob", "dave")[1:3], ("conflict", "HTTP request failed with status 409: "
                                                                      "dave is already bob's own contact."))
        self.assertEqual(self.intro("carol", "dave")[1], "conflict")
        self.assertNotIn("dave", carol.settings()["tokens"])
        self.assertTrue(self.connected("bob", "carol") and self.connected("carol", "bob"))

    def test_a_newcomer_s_own_contact_is_skipped_at_once(self):
        self.join("bob")
        self.apps["carol"].trust("bob", {"person": "bob", "url": self.apps["bob"].settings()["url"],
                                         "token": "B" * 43})
        self.join("carol")
        self.drain()
        self.assertEqual(self.intro("bob", "carol")[1], "skipped")
        self.assertEqual(self.alice.db.execute("SELECT count(*) FROM courier_team_remove").fetchone()[0], 0)

    def test_a_pairing_in_progress_postpones_the_introduction(self):
        self.join("bob")
        self.join("carol")
        carol = self.apps["carol"]
        with carol.transaction():
            carol.db.execute("INSERT INTO courier_offer(id, key, expires, replacing, state, contact) "
                             "VALUES(1, '00', ?, 0, 'provisional', 'bob')", (self.now + 600,))
        self.alice.introduce_once()
        step, state, error, _ = self.intro("bob", "carol")
        self.assertEqual((step, state), (1, "pending"))
        self.assertIn("503", error)

    def test_a_member_that_left_or_unpaired_the_owner_is_no_longer_managed(self):
        self.join("bob")
        self.join("carol")
        self.drain()
        self.assertEqual(self.apps["bob"].leave_team(), "alice")
        self.apps["carol"].untrust("alice")
        self.assertIsNone(self.apps["carol"].owner())
        self.join("dave")
        self.drain()
        self.drain("remove_once")
        self.assertEqual(self.alice.members(), ["dave"])
        self.assertTrue(self.connected("bob", "carol"))  # Contacts made in the team stay.
        for name in ("bob", "carol"):
            self.assertNotIn(name, self.apps["dave"].settings()["tokens"])

    def test_an_older_courier_is_marked_and_the_step_taken_is_undone(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()
        with patch("opencode_connect.courier.team.http_json", side_effect=HttpError(404, "Route not found.")):
            self.alice.introduce_once()
        self.assertEqual(self.intro("bob", "carol")[1], "unsupported")
        self.drain("remove_once")
        self.assertNotIn("bob", self.apps["carol"].settings()["tokens"])
        self.assertEqual(self.alice.introduce_again("carol"), ["bob"])
        self.drain()
        self.assertTrue(self.connected("carol", "bob"))
        self.assertEqual(self.alice.introduce_again("carol"), [])  # Done pairs stay as they are.

    def test_introducing_again_cancels_the_undoing_still_queued(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()
        with patch("opencode_connect.courier.team.http_json", side_effect=HttpError(404, "Route not found.")):
            self.alice.introduce_once()
        self.assertEqual(self.alice.introduce_again("bob"), ["carol"])  # Before the undoing went out.
        self.drain()
        self.drain("remove_once")
        self.assertTrue(self.connected("bob", "carol") and self.connected("carol", "bob"))

    def test_a_refusal_at_the_last_step_is_undone_on_both_sides(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()
        self.alice.introduce_once()
        with patch("opencode_connect.courier.team.http_json", side_effect=HttpError(409, "Refused.")):
            self.alice.introduce_once()
        self.assertEqual(self.intro("bob", "carol")[1], "conflict")
        self.drain("remove_once")
        self.assertNotIn("carol", self.apps["bob"].settings()["contacts"])
        self.assertNotIn("bob", self.apps["carol"].settings()["tokens"])

    def test_a_removal_answered_by_a_member_that_left_ends_its_membership(self):
        self.join("bob")
        self.apps["bob"].leave_team()
        with self.alice.transaction():
            self.alice._queue_removal_locked("bob", "carol")
        self.assertEqual(self.alice.remove_once(), "bob")
        self.assertEqual(self.alice.members(), [])

    def test_an_introduction_whose_contact_is_gone_ends_instead_of_failing(self):
        self.join("bob")
        self.join("carol")
        with self.alice.transaction():
            config = self.alice.settings()
            del config["contacts"]["carol"]
            self.alice.save_settings(config)
        self.assertEqual(self.alice.introduce_once(), "carol")
        self.assertEqual(self.intro("bob", "carol")[1:3], ("conflict", "No longer a contact here."))

    def test_pairing_by_hand_makes_an_introduced_teammate_an_own_contact(self):
        self.join("bob")
        self.join("carol")
        self.drain()
        code = self.apps["bob"].offer(replace=True)
        join_offer(self.apps["carol"], f"{self.apps['bob'].settings()['url']}#{code}", replace=True, attempts=1)
        self.assertEqual((self.apps["bob"].introduced(), self.apps["carol"].introduced()), (set(), set()))

    def test_the_joiner_refuses_a_member_list_it_cannot_use(self):
        code = self.alice.team_invite()
        card = {"person": "alice", "url": self.alice.settings()["url"], "token": "A" * 43}
        for members in (["Bob!"], ["m%d" % number for number in range(101)], "bob"):
            def answer(url, *, data, **kwargs):
                return {"card": card, "team": members,
                        "mac": seal(pairing_key(code), b"team-offer", data["nonce"], {"card": card, "team": members})}

            with self.subTest(members=members), patch("opencode_connect.courier.pairing.http_json", side_effect=answer), \
                    self.assertRaisesRegex(BridgeError, "did not prove"):
                request_pairing(self.apps["bob"], f"{card['url']}#{code}", team=True, attempts=1)

    def test_removing_or_unpairing_a_member_drops_it_from_the_others(self):
        for name in ("bob", "carol", "dave"):
            self.join(name)
        self.drain()
        self.alice.team_remove_member("dave")
        self.drain("remove_once")
        for name in ("bob", "carol"):
            self.assertNotIn("dave", self.apps[name].settings()["contacts"])
            self.assertNotIn(name, self.apps["dave"].settings()["contacts"])
        self.assertTrue(self.connected("alice", "dave"))  # Its own pairing with the owner stays.
        self.alice.untrust("carol")
        self.drain("remove_once")
        self.assertEqual(self.alice.members(), ["bob"])
        self.assertNotIn("carol", self.apps["bob"].settings()["contacts"])
        with self.assertRaises(BridgeError):
            self.alice.team_remove_member("carol")

    def test_an_answer_to_a_replaced_introduction_is_not_applied(self):
        self.join("bob")
        self.join("carol")

        def replaced(*args, **kwargs):
            http_json(*args, **kwargs)
            with self.alice.transaction():
                self.alice._introduce_locked("carol", "bob")

        with patch("opencode_connect.courier.team.http_json", side_effect=replaced):
            self.alice.introduce_once()
        self.assertEqual(self.intro("bob", "carol")[:2], (1, "pending"))
        self.drain()
        self.assertTrue(self.connected("bob", "carol") and self.connected("carol", "bob"))

    def test_a_newcomer_that_moved_tells_the_member_its_current_url(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()
        self.alice.introduce_once()  # Bob now has carol's card, with the URL alice knows.
        carol = self.apps["carol"]
        with carol.transaction():
            config = carol.settings()
            config["url"] = NEW
            carol.save_settings(config)
        self.alice.introduce_once()
        self.assertEqual(carol.db.execute("SELECT contact, url, state FROM courier_announce").fetchall(),
                         [("bob", NEW, "pending")])

    def test_a_member_that_moved_before_its_card_arrived_is_reached_at_its_new_url(self):
        self.join("bob")
        self.join("carol")
        self.alice.introduce_once()
        self.alice.introduce_once()
        token = self.apps["bob"].settings()["contacts"]["carol"]["token"]
        self.assertEqual(self.apps["carol"].update_contact_url("Bearer " + token, {"url": NEW}, "127.0.0.1"),
                         {"person": "carol", "updated": False})
        self.alice.introduce_once()
        self.assertEqual(self.apps["carol"].settings()["contacts"]["bob"]["url"], NEW)

    def test_a_courier_is_in_one_team_at_a_time(self):
        self.join("bob")
        with self.assertRaisesRegex(BridgeError, "in alice's team"):
            self.apps["bob"].team_invite()
        with self.assertRaisesRegex(BridgeError, "its own team"), self.alice.transaction():
            self.alice._join_team_locked("dave")
        with self.assertRaisesRegex(BridgeError, "already in alice's team"), self.apps["bob"].transaction():
            self.apps["bob"]._join_team_locked("dave")

    def test_a_member_that_lost_its_state_rejoins_and_is_introduced_again(self):
        self.join("bob")
        self.join("carol")
        self.drain()
        carol = self.apps["carol"]
        with carol.transaction():  # As after reinstalling: its teammates' tokens are gone.
            carol._untrust_locked("bob")
        self.join("carol", replace=True)
        self.assertEqual(self.intro("bob", "carol")[:2], (1, "pending"))
        self.drain(check=False)
        self.assertTrue(self.connected("bob", "carol") and self.connected("carol", "bob"))

    def status(self, name):
        app = self.apps[name]
        state = read_state(app.db, self.now)
        app.db.rollback()  # read_state's transaction only read.
        contacts = [line for contact in sorted(state["config"]["contacts"])
                    for line in describe_contact(contact, state["config"], state, self.now)]
        return contacts + team_lines(state)

    def test_status_shows_the_team_and_what_is_under_way(self):
        for name in ("bob", "carol", "dave"):
            self.join(name)
        with patch("opencode_connect.courier.team.http_json", side_effect=BridgeError("Connection refused.")):
            self.alice.introduce_once()
        with patch("opencode_connect.courier.team.http_json", side_effect=HttpError(404, "Route not found.")):
            self.alice.introduce_once()
        with self.alice.transaction():
            self.alice._queue_removal_locked("bob", "erin")
        lines = self.status("alice")
        for line in ("Team: yours, with bob, carol, dave.",
                     "  Introducing carol to bob: step 1 of 3; the last try failed: Connection refused.",
                     "  Introducing dave to carol: step 1 of 3.",
                     "  1 teammate removal(s) wait to reach bob."):
            self.assertIn(line, lines)
        (older,) = [line for line in lines if line.startswith("  dave and bob were not introduced")]
        self.assertIn("one of their couriers is older", older)
        self.assertTrue(older.endswith("`python3 bin/courier.py team introduce dave` tries again."))
        self.now += 2
        self.drain()
        lines = self.status("carol")
        self.assertIn("Team: you are in alice's team; alice can add and remove teammates as your contacts, and "
                      "`python3 bin/courier.py team leave` stops it.", lines)
        self.assertIn("introduced by alice", next(line for line in lines if line.startswith("  bob:")))
        self.assertNotIn("introduced", next(line for line in lines if line.startswith("  alice:")))


class MigrationTests(unittest.TestCase):
    def test_an_offer_table_from_before_teams_gains_its_column(self):
        with tempfile.TemporaryDirectory(prefix="courier-migration-") as folder:
            queue = FakeQueue(Path(folder) / "queue.sqlite3")
            self.addCleanup(queue.db.close)
            queue.db.execute("""CREATE TABLE courier_offer (
                id INTEGER PRIMARY KEY CHECK(id=1), key TEXT NOT NULL, expires REAL NOT NULL,
                name TEXT, replacing INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open', 'provisional', 'confirmed')),
                nonce TEXT, contact TEXT, request TEXT, response TEXT, previous TEXT)""")
            queue.db.commit()
            for _ in range(2):  # Opening it again changes nothing more.
                app = Courier(queue, configuration())
            columns = {row[1] for row in app.db.execute("PRAGMA table_info(courier_offer)")}
            self.assertIn("team", columns)


if __name__ == "__main__":
    unittest.main()
