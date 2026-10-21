"""Per-contact routes: proposed destinations, direct delivery, batch approval, and desktop notices."""

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier
from opencode_connect.courier.cli import main
from opencode_connect.courier.notify import desktop_notification
from opencode_connect.courier.settings import read_config, write_config
from tests.courier.fakes import FakeQueue, configuration

ASKER = "claude:" + str(uuid.UUID(int=3))


class RouteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-routes-")
        self.addCleanup(folder.cleanup)
        self.now = 1000.0
        self.notified = []
        self.queue = FakeQueue(Path(folder.name) / "bob.sqlite3")
        self.addCleanup(self.queue.db.close)
        self.bob = Courier(self.queue, configuration("bob"), clock=lambda: self.now,
                           notify=lambda name: self.notified.append((name, self.queue.db.in_transaction)))
        self.card = self.bob.invite("alice")

    def receive(self, key="one", **extra):
        return self.bob.receive("Bearer " + self.card["token"], {"text": "hello", "idempotency_key": key, **extra})

    def asked(self):
        """A message this side sent to alice from ASKER; returns the ID alice gave it."""
        remote_id = "msg_" + "2" * 32
        with self.bob.transaction():
            self.bob.db.execute("INSERT INTO courier_sent(id,contact,source,remote_id,state) VALUES(?,?,?,?,'held')",
                                ("msg_" + "1" * 32, "alice", ASKER, remote_id))
        return remote_id

    def test_a_route_needs_a_contact_and_a_local_session(self):
        with self.assertRaisesRegex(BridgeError, "invite"):
            self.bob.set_route("carol", "wsl:ses_example")
        for target in ("person:alice", "person:ses_example", "wsl", "claude:not-a-uuid", None):
            with self.subTest(target=target), self.assertRaises(BridgeError):
                self.bob.set_route("alice", target)
        with self.assertRaises(BridgeError):
            self.bob.set_route("alice", "wsl:ses_example", "always")
        self.bob.set_route("alice", "wsl:ses_example", "direct")
        self.assertEqual(self.bob.contacts(), [{"person": "alice", "receiving": True, "sending": False,
                                                "route": {"target": "wsl:ses_example", "mode": "direct"}}])
        self.bob.untrust("alice")
        self.bob.invite("alice")
        self.assertIsNone(self.bob.contacts()[0]["route"])

    def test_ask_holds_proposes_the_route_and_notifies_once_outside_the_transaction(self):
        self.bob.set_route("alice", "wsl:ses_example")
        receipt = self.receive()
        self.assertEqual(receipt["state"], "held")
        self.assertEqual(self.queue.get(receipt["id"])["state"], "held")
        self.assertEqual(self.bob.inbox()[0]["suggested_target"], "wsl:ses_example")
        self.assertEqual(self.receive(), receipt)  # A repeated upload is not a new message.
        self.assertEqual(self.notified, [("alice", False)])

    def test_direct_approves_on_arrival_without_a_notice(self):
        self.bob.set_route("alice", "wsl:ses_example", "direct")
        receipt = self.receive()
        self.assertEqual(receipt["state"], "approved")
        row = self.queue.get(receipt["id"])
        self.assertEqual((row["state"], row["target"]), ("pending", "wsl:ses_example"))
        self.assertEqual(self.bob.inbox(), [])
        self.assertEqual(self.bob.remote_status("Bearer " + self.card["token"], receipt["id"])["state"], "approved")
        self.assertEqual(self.notified, [])

    def test_a_reply_goes_to_the_session_that_asked(self):
        remote_id = self.asked()
        self.bob.set_route("alice", "wsl:ses_example")
        self.receive(key="ask", reply_to=remote_id)
        self.assertEqual(self.bob.inbox()[0]["suggested_target"], ASKER)
        self.bob.set_route("alice", "wsl:ses_example", "direct")
        receipt = self.receive(key="direct", reply_to=remote_id)
        self.assertEqual(self.queue.get(receipt["id"])["target"], ASKER)

    def test_direct_holds_what_exceeds_the_hourly_limit(self):
        self.bob.set_route("alice", "wsl:ses_example", "direct")
        with patch("opencode_connect.courier.app.DIRECT_PER_HOUR", 2):
            self.assertEqual([self.receive(key=str(n))["state"] for n in range(3)], ["approved", "approved", "held"])
            self.assertEqual(self.notified, [("alice", False)])
            self.now += 3601
            self.assertEqual(self.receive(key="later")["state"], "approved")

    def test_a_direct_approval_that_fails_leaves_the_message_held(self):
        self.bob.set_route("alice", "wsl:ses_example", "direct")

        def partial(message_id, target):
            # A queue that wrote part of the approval before failing.
            self.queue.db.execute("UPDATE fake_messages SET state='pending', target=? WHERE id=?", (target, message_id))
            raise BridgeError("The destination is not in the configured local node.")

        with patch.object(self.queue, "approve_inbound", side_effect=partial):
            receipt = self.receive()
        self.assertEqual(receipt["state"], "held")
        row = self.queue.get(receipt["id"])
        self.assertEqual((row["state"], row["target"]), ("held", None))
        self.assertEqual([held["id"] for held in self.bob.inbox()], [receipt["id"]])
        self.assertEqual(self.notified, [("alice", False)])

    def test_approve_all_uses_the_shown_targets_and_skips_what_changed(self):
        first, second = self.receive(key="1"), self.receive(key="2")
        self.bob.reject(second["id"])
        results = self.bob.approve_all([(first["id"], "wsl:ses_example"), (second["id"], "wsl:ses_example")])
        self.assertEqual([(row["id"], row["state"]) for row in results],
                         [(first["id"], "approved"), (second["id"], "skipped")])
        self.assertIn("held", results[1]["reason"])
        self.assertEqual(self.queue.get(first["id"])["target"], "wsl:ses_example")


class RouteCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-route-cli-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "courier.json"
        self.database = Path(folder.name) / "queue.sqlite3"
        write_config(self.path, configuration("bob"))
        app = self.courier()
        self.alice, self.carol = app.invite("alice"), app.invite("carol")

    def courier(self):
        queue = FakeQueue(self.database)
        self.addCleanup(queue.db.close)
        return Courier(queue, read_config(self.path), config_path=self.path)

    def receive(self, card, key):
        return self.courier().receive("Bearer " + card["token"], {"text": "hello\x1b[2J", "idempotency_key": key})

    def command(self, *arguments, confirm=None):
        """The CLI in a terminal; `confirm` stands in for typing the code and sees its prompt."""
        prompts = []

        def confirmation(prompt):
            prompts.append(prompt)
            if confirm:
                confirm()

        with patch("opencode_connect.courier.cli.require_terminal"), \
                patch("opencode_connect.courier.cli.interactive_confirmation", side_effect=confirmation), \
                patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("sys.stderr", new_callable=io.StringIO) as errors:
            result = main(["--config", str(self.path), *arguments], queue_factory=lambda path: FakeQueue(self.database))
        return result, output.getvalue() + errors.getvalue(), prompts

    def test_contact_stores_a_canonical_route_after_one_confirmation(self):
        result, _, prompts = self.command("contact", "alice", "--to", "local:ses_example", "--direct")
        self.assertEqual(result, 0)
        self.assertEqual(len(prompts), 1)
        self.assertIn("wsl:ses_example", prompts[0])
        self.assertIn("without approval", prompts[0])
        self.assertEqual(self.courier().contacts()[0]["route"], {"target": "wsl:ses_example", "mode": "direct"})
        result, output, prompts = self.command("contact", "alice", "--to", "linux:ses_example")
        self.assertEqual((result, prompts), (1, []))
        self.assertIn("configured local node", output)
        self.command("contact", "alice", "--to", "wsl:ses_example", "--ask")
        self.assertEqual(self.courier().contacts()[0]["route"]["mode"], "ask")

    def test_approve_without_an_id_confirms_once_and_only_what_it_showed(self):
        self.courier().set_route("alice", "wsl:ses_example")
        routed, unrouted = self.receive(self.alice, "1"), self.receive(self.carol, "2")
        late = []
        result, output, prompts = self.command("approve", confirm=lambda: late.append(self.receive(self.alice, "3")))
        self.assertEqual(result, 0)
        shown = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual([(row["id"], row["to"]) for row in shown], [(routed["id"], "wsl:ses_example"), (unrouted["id"], None)])
        self.assertEqual({row["text"] for row in shown}, {"hello"})
        self.assertEqual(len(prompts), 1)
        self.assertIn("wsl:ses_example", prompts[0])
        queue = self.courier().queue
        self.assertEqual(queue.get(routed["id"])["state"], "pending")
        self.assertEqual(queue.get(unrouted["id"])["state"], "held")
        self.assertEqual(queue.get(late[0]["id"])["state"], "held")
        self.assertIn("--to", output)

    def test_approve_one_message_uses_its_route_or_an_explicit_destination(self):
        unrouted = self.receive(self.carol, "1")
        result, output, prompts = self.command("approve", unrouted["id"])
        self.assertEqual((result, prompts), (1, []))
        self.assertIn("--to", output)
        result, _, prompts = self.command("approve", unrouted["id"], "--to", "wsl:ses_example")
        self.assertEqual(result, 0)
        self.assertIn("wsl:ses_example", prompts[0])
        self.assertEqual(self.courier().queue.get(unrouted["id"])["target"], "wsl:ses_example")
        result, output, prompts = self.command("approve", "--to", "wsl:ses_example")
        self.assertEqual((result, prompts), (1, []))

    def test_the_worker_gets_desktop_notices(self):
        with patch("opencode_connect.courier.cli.Courier") as courier_class, \
                patch("sys.stdout", new_callable=io.StringIO):
            courier_class.return_value.contacts.return_value = []
            main(["--config", str(self.path), "contacts"], queue_factory=lambda path: FakeQueue(self.database))
        self.assertIs(courier_class.call_args.kwargs["notify"], desktop_notification)


class DesktopNoticeTests(unittest.TestCase):
    def test_the_notice_names_the_contact_never_the_text(self):
        with patch("opencode_connect.courier.notify.shutil.which", return_value="/usr/bin/notify-send"), \
                patch("opencode_connect.courier.notify.subprocess.run") as run:
            desktop_notification("alice")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/notify-send")
        self.assertIn("alice", " ".join(command))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 2)

    def test_no_desktop_or_a_failing_one_never_affects_delivery(self):
        with patch("opencode_connect.courier.notify.shutil.which", return_value=None), \
                patch("opencode_connect.courier.notify.subprocess.run") as run:
            desktop_notification("alice")
        run.assert_not_called()
        for failure in (subprocess.TimeoutExpired("notify-send", 2), OSError("No such file")):
            with self.subTest(failure=type(failure).__name__), \
                    patch("opencode_connect.courier.notify.shutil.which", return_value="/usr/bin/notify-send"), \
                    patch("opencode_connect.courier.notify.subprocess.run", side_effect=failure):
                desktop_notification("alice")


if __name__ == "__main__":
    unittest.main()
