"""The team commands from the command line: `team invite`, `join`, and the other actions."""

import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier
from opencode_connect.courier.cli import main
from opencode_connect.courier.pairing import pairing_key, seal
from opencode_connect.courier.settings import read_config, write_config
from opencode_connect.courier.worker import CourierBusy
from tests.courier.fakes import FakeQueue, configuration

BOB = {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43}
CAROL = {"person": "carol", "url": "http://192.0.2.30:8765", "token": "C" * 43}
ERIN = {"person": "erin", "url": "http://192.0.2.50:8765", "token": "E" * 43}
DECLINED = BridgeError("Confirmation did not match; nothing was changed.")


class TeamCliTests(unittest.TestCase):
    def setUp(self):
        keys = patch("opencode_connect.courier.pairing.KDF_ROUNDS", 1000)
        keys.start()
        self.addCleanup(keys.stop)
        folder = tempfile.TemporaryDirectory(prefix="courier-team-cli-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "courier.json"
        self.database = Path(folder.name) / "queue.sqlite3"
        write_config(self.path, {**configuration("alice"), "listen": "192.0.2.10:8765", "url": "http://192.0.2.10:8765"})

    def main(self, *arguments, output=None, confirm=None):
        output = output or io.StringIO()
        with patch("opencode_connect.courier.cli.require_terminal"), patch("sys.stdout", output), \
                patch("sys.stderr", output), \
                patch("opencode_connect.courier.teamflow.interactive_confirmation", side_effect=confirm) as asked:
            result = main(["--config", str(self.path), *arguments], queue_factory=lambda path: FakeQueue(self.database))
        return result, output.getvalue(), asked

    def other(self):
        queue = FakeQueue(self.database)
        self.addCleanup(queue.db.close)
        return Courier(queue, read_config(self.path))

    def member(self, app, card):
        app.invite(card["person"])
        app.trust(card["person"], card)
        with app.transaction():
            app._team_member_joined_locked(card["person"])

    def test_team_invite_prints_a_join_link_and_introduces_the_newcomer(self):
        self.member(self.other(), BOB)
        output, steps = io.StringIO(), []

        def service(seconds):
            """The service, meanwhile: carol joins with the link, then her introduction goes out."""
            app = self.other()
            if not steps:
                code = re.search(r"#([0-9A-Z-]{14})'", output.getvalue())[1]
                answer = app.join({"nonce": "a" * 32, "card": CAROL,
                                   "mac": seal(pairing_key(code), b"team-join", "a" * 32, CAROL)}, "192.0.2.30")
                self.assertEqual(answer["team"], ["bob"])
                app.confirm_pairing("Bearer " + answer["card"]["token"], {"nonce": "a" * 32})
            steps.append(app.introduce_once())

        with patch("opencode_connect.courier.pairflow.run_courier", side_effect=CourierBusy("busy")), \
                patch("opencode_connect.courier.pairflow.time.sleep", side_effect=service), \
                patch("opencode_connect.courier.pairflow.http_json", return_value={"person": "carol", "protocol": 1}), \
                patch("opencode_connect.courier.team.http_json", return_value={"person": "x", "changed": True}):
            result, text, _ = self.main("team", "invite", output=output)
        self.assertEqual(result, 0, text)
        for line in ("python3 bin/courier.py join 'http://192.0.2.10:8765#", "whoever uses it first joins your team",
                     "Paired with carol, both ways.", "Introducing carol to the team...",
                     "carol is connected with bob."):
            self.assertIn(line, text)
        self.assertEqual(self.other().members(), ["bob", "carol"])

    def test_the_first_member_has_nobody_to_meet(self):
        def service(seconds):
            app = self.other()
            code = re.search(r"#([0-9A-Z-]{14})'", output.getvalue())[1]
            answer = app.join({"nonce": "b" * 32, "card": BOB,
                               "mac": seal(pairing_key(code), b"team-join", "b" * 32, BOB)}, "192.0.2.20")
            app.confirm_pairing("Bearer " + answer["card"]["token"], {"nonce": "b" * 32})

        output = io.StringIO()
        with patch("opencode_connect.courier.pairflow.run_courier", side_effect=CourierBusy("busy")), \
                patch("opencode_connect.courier.pairflow.time.sleep", side_effect=service), \
                patch("opencode_connect.courier.pairflow.http_json", return_value={"person": "bob", "protocol": 1}):
            result, text, _ = self.main("team", "invite", "--for", "bob", output=output)
        self.assertEqual(result, 0, text)
        self.assertIn("bob is the first member of your team.", text)

    def pending(self, members):
        return {"origin": ERIN["url"], "nonce": "c" * 32, "token": "T" * 43, "card": ERIN, "team": members}

    def test_join_shows_the_owner_and_members_and_declining_stores_nothing(self):
        with patch("opencode_connect.courier.teamflow.request_pairing", return_value=self.pending(["bob", "carol"])), \
                patch("opencode_connect.courier.teamflow.store_pairing") as store:
            result, text, asked = self.main("join", ERIN["url"] + "#ABCD-EFGH-JKMN", confirm=DECLINED)
        self.assertEqual(result, 1)
        store.assert_not_called()
        self.assertIn("erin's team, with bob, carol.", text)
        self.assertIn("so erin could also write to you as one of them", text)
        self.assertIn("python3 bin/courier.py team leave` ends it", text)
        self.assertEqual(asked.call_args.args[0], "Join erin's team")
        self.assertEqual(self.other().contacts(), [])

    def test_join_stores_after_the_code_and_says_who_is_connected(self):
        def stored(courier, pending, replace):
            for card in (ERIN, BOB):  # Bob's introduction arrived; carol's not yet.
                courier.invite(card["person"])
                courier.trust(card["person"], card)
            with courier.transaction():
                courier._join_team_locked("erin")
                courier.note_peer("erin")  # Erin's courier checked the way back.

        with patch("opencode_connect.courier.teamflow.request_pairing", return_value=self.pending(["bob", "carol"])), \
                patch("opencode_connect.courier.teamflow.store_pairing", side_effect=stored), \
                patch("opencode_connect.courier.teamflow.serve_while"):
            result, text, _ = self.main("join", ERIN["url"] + "#ABCD-EFGH-JKMN", "--replace")
        self.assertEqual(result, 0, text)
        for line in ("Joined erin's team. Waiting for the introductions...",
                     "erin checked the way back: both ways work.", "Connected with bob.",
                     "Not connected with carol yet: erin's courier introduces you while both couriers run"):
            self.assertIn(line, text)
        self.assertEqual(self.other().owner(), "erin")

    def test_join_is_refused_while_this_courier_owns_a_team_or_is_in_another(self):
        app = self.other()
        self.member(app, BOB)
        with patch("opencode_connect.courier.teamflow.request_pairing") as request:
            result, text, _ = self.main("join", ERIN["url"] + "#ABCD-EFGH-JKMN")
        self.assertEqual(result, 1)
        self.assertIn("its own team", text)
        request.assert_not_called()
        app.team_remove_member("bob")
        with app.transaction():
            app._join_team_locked("carol")
        with patch("opencode_connect.courier.teamflow.request_pairing", return_value=self.pending([])), \
                patch("opencode_connect.courier.teamflow.store_pairing") as store:
            result, text, _ = self.main("join", ERIN["url"] + "#ABCD-EFGH-JKMN")
        self.assertEqual(result, 1)
        self.assertIn("already in carol's team", text)
        store.assert_not_called()

    def test_leave_asks_for_a_code_and_keeps_the_contacts(self):
        result, text, _ = self.main("team", "leave")
        self.assertEqual((result, "not in a team" in text), (1, True))
        app = self.other()
        app.invite("erin")
        with app.transaction():
            app._join_team_locked("erin")
        result, text, _ = self.main("team", "leave", confirm=DECLINED)
        self.assertEqual((result, app.owner()), (1, "erin"))
        result, text, asked = self.main("team", "leave")
        self.assertEqual(result, 0, text)
        self.assertIn("erin can no longer add or remove your contacts", asked.call_args.args[0])
        self.assertIn("Left erin's team.", text)
        self.assertIsNone(app.owner())

    def test_remove_and_introduce_apply_to_members_after_a_code(self):
        app = self.other()
        self.member(app, BOB)
        result, text, asked = self.main("team", "remove", "carol")
        self.assertEqual((result, "carol is not in your team" in text), (1, True))
        asked.assert_not_called()  # Nothing to confirm for someone who is not a member.
        self.member(app, CAROL)
        with app.transaction():
            app.db.execute("UPDATE courier_intro SET state='conflict'")
        with patch("opencode_connect.courier.teamflow.serve_while"):
            result, text, asked = self.main("team", "introduce", "bob")
        self.assertEqual(result, 0, text)
        self.assertIn("Introducing bob again to carol.", text)
        self.assertIn("Still introducing bob to carol", text)
        result, text, _ = self.main("team", "introduce", "bob")
        self.assertIn("an introduction is under way", text)
        result, text, _ = self.main("team", "remove", "carol", confirm=DECLINED)
        self.assertEqual((result, app.members()), (1, ["bob", "carol"]))
        result, text, asked = self.main("team", "remove", "carol")
        self.assertEqual(result, 0, text)
        self.assertIn("the other members drop carol", asked.call_args.args[0])
        self.assertIn("Removed carol from your team.", text)
        self.assertEqual(app.members(), ["bob"])


if __name__ == "__main__":
    unittest.main()
