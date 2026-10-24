"""`status`: who this courier is, whether it runs, and what waits, read without changing anything."""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import RETENTION, Courier
from opencode_connect.courier.cli import main
from opencode_connect.courier.settings import write_config
from opencode_connect.courier.status import courier_relays, separate_relays, status
from opencode_connect.local import mailbox
from tests.courier.fakes import configuration
from tests.courier.test_setup import system


class StatusTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-status-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "courier.json"
        self.queue_root = self.root / "queue"
        self.unit = self.root / "unit.service"
        self.unit.write_text("[Unit]\n")
        self.config = {**configuration(), "listen": "127.0.0.1:8765"}
        write_config(self.path, self.config)
        self.box = mailbox.LocalMailbox(self.queue_root, node="wsl")
        self.addCleanup(self.box.db.close)
        (self.box.state / "mail.sqlite3").chmod(0o600)
        self.now = 1000.0
        self.app = Courier(self.box, self.config, config_path=self.path, clock=lambda: self.now)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def status(self, *, unit=True, answers=True, proc=None, wsl=False):
        if not unit:
            self.unit.unlink()
        with patch("opencode_connect.courier.status.unit_path", return_value=self.unit), \
                patch("opencode_connect.courier.host.subprocess.run", system(manager="active")) as run, \
                patch("opencode_connect.courier.doctor.socket.create_connection",
                      **({} if answers else {"side_effect": ConnectionRefusedError})):
            code, lines = status(self.path, root=self.queue_root, clock=lambda: self.now,
                                 proc=proc or self.root / "no-proc", wsl=wsl)
        return code, "\n".join(lines), run

    def test_status_describes_each_contact_and_what_waits_without_changing_anything(self):
        bob = self.app.invite("bob")
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43})
        self.app.set_route("bob", "wsl:ses_example", "direct")
        carol = self.app.invite("carol")
        self.app.trust("dave", {"person": "dave", "url": "http://192.0.2.30:8765", "token": "D" * 43})
        self.app.receive("Bearer " + carol["token"], {"text": "hello", "idempotency_key": "one"})
        self.box.enqueue("wsl:ses_example", "person:bob", "draft one")
        with patch("opencode_connect.courier.app.http_json", return_value={"id": "msg_" + "3" * 32, "state": "held"}):
            self.app.send_once()
        self.now += 60
        self.box.enqueue("wsl:ses_example", "person:bob", "draft two")
        with patch("opencode_connect.courier.app.http_json", side_effect=BridgeError("Connection refused.")):
            self.app.send_once()
        with patch("opencode_connect.courier.pairing.KDF_ROUNDS", 1000):
            self.app.offer("erin")
        self.now += 240
        before = self.snapshot()
        code, text, _ = self.status(wsl=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(code, 0)
        for line in ("alice: listening on 127.0.0.1:8765, reachable at http://192.0.2.10:8765.",
                     "Service: active.", "Listener: answering on 127.0.0.1:8765.",
                     "  bob: both ways; messages go to wsl:ses_example without approval; "
                     "the last attempt failed 4 min ago: Connection refused.",
                     "    1 message(s) to bob wait to be sent.", "    1 message(s) to bob wait for bob's approval.",
                     "  carol: carol can write to you, but you cannot write back: pair again; "
                     "messages are held with no destination; last contact 5 min ago.",
                     "    1 message(s) from carol wait for your approval.",
                     "  dave: you can write to dave, but dave cannot write to you: pair again; no contact yet.",
                     "1 message(s) wait for your approval: `python3 bin/courier.py approve` shows them first.",
                     "On WSL, this courier listens only while a WSL session is open; "
                     "contacts' couriers keep retrying until then."):
            self.assertIn(line, text.splitlines())
        self.assertRegex(text, r"Pairing: an offer for erin is open until \d\d:\d\d\.")
        self.assertNotIn(bob["token"], text)
        self.assertNotIn("B" * 43, text)
        self.assertNotIn("draft", text)

    def test_lapsed_offers_and_expired_messages_no_longer_count(self):
        carol = self.app.invite("carol")
        self.app.receive("Bearer " + carol["token"], {"text": "hello", "idempotency_key": "one"})
        with patch("opencode_connect.courier.pairing.KDF_ROUNDS", 1000):
            self.app.offer()
        self.now += RETENTION + 1
        _, text, _ = self.status()
        self.assertNotIn("wait for your approval", text)
        self.assertNotIn("Pairing", text)

    def test_the_queue_is_authoritative_and_controls_never_reach_the_terminal(self):
        with patch.object(self.app, "sync_config"):
            self.app.invite("bob")  # The JSON copy misses bob.
        with self.app.transaction():
            self.app.db.execute("INSERT INTO courier_peers(contact, last_error, last_error_at) VALUES('bob', ?, ?)",
                                ("refused\x1b]0;title\x07", self.now))
        _, text, _ = self.status()
        self.assertIn("  bob: bob can write to you, but you cannot write back: pair again; messages are held with "
                      "no destination; the last attempt failed under a minute ago: refused.", text.splitlines())
        self.assertNotIn("\x1b", text)

    def test_a_stopped_service_and_listener_say_what_to_run(self):
        code, text, run = self.status(unit=False, answers=False)
        self.assertEqual(code, 0)
        self.assertIn("Service: not installed; `python3 bin/courier.py service install` starts the courier "
                      "at each login.", text)
        self.assertIn("Listener: nothing answers on 127.0.0.1:8765", text)
        self.assertEqual(run.calls, [])  # No unit: systemd is not asked.
        self.assertIn("No contacts yet: `python3 bin/courier.py pair`", text)

    def test_unset_unreadable_or_unstarted_state_is_reported_not_repaired(self):
        missing = self.root / "missing/courier.json"
        code, lines = status(missing, root=self.queue_root)
        self.assertEqual(code, 1)
        self.assertIn("setup", lines[0])
        self.assertFalse(missing.parent.exists())
        fresh = self.root / "fresh"
        with patch("opencode_connect.courier.status.unit_path", return_value=self.root / "no-unit"), \
                patch("opencode_connect.courier.doctor.socket.create_connection"):
            code, lines = status(self.path, root=fresh, proc=self.root / "no-proc", wsl=False)
        self.assertIn("The courier has not run yet.", lines)
        self.assertFalse(fresh.exists())
        (self.box.state / "mail.sqlite3").chmod(0o644)
        code, lines = status(self.path, root=self.queue_root)
        self.assertEqual(code, 1)
        self.assertIn("doctor --offline", lines[-1])

    def test_a_separate_relay_of_this_user_is_found_from_proc(self):
        proc = self.root / "proc"
        for pid, argv in (("4242", [b"python3", b"/srv/example/bin/local_bridge.py", b"relay"]),
                          ("4243", [b"python3", b"/srv/example/bin/local_bridge.py", b"inbox"]),
                          ("4244", [b"python3", b"/srv/example/bin/collab.py", b"relay"]), ("self", [b"relay"]),
                          ("4246", [b"python3", b"-B", b"/srv/example/bin/courier.py", b"--config", b"c.json",
                                    b"run", b"--relay"]),
                          ("4247", [b"python3", b"/srv/example/bin/courier.py", b"run"])):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
        (proc / "4245").mkdir()  # A process that ended while it was read.
        self.assertEqual(separate_relays(proc), [4242])
        self.assertEqual(courier_relays(proc), [4246])
        with patch("opencode_connect.courier.status.os.getuid", return_value=os.getuid() + 1):
            self.assertEqual(separate_relays(proc), [])  # Another user's relay uses another queue.
        self.assertEqual(separate_relays(self.root / "missing"), [])
        _, text, _ = self.status(proc=proc)
        self.assertIn("A separate local relay runs (PID 4242)", text)

    def test_the_cli_prints_status_without_opening_the_queue_for_writing(self):
        with patch("opencode_connect.courier.status.local_queue_root", return_value=self.queue_root), \
                patch("opencode_connect.courier.status.unit_path", return_value=self.root / "no-unit"), \
                patch("opencode_connect.courier.status.separate_relays", return_value=[]), \
                patch("opencode_connect.courier.doctor.socket.create_connection"), \
                patch("opencode_connect.courier.cli.real_queue") as factory, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--config", str(self.path), "status"]), 0)
        factory.assert_not_called()
        self.assertIn("alice: listening on", output.getvalue())


if __name__ == "__main__":
    unittest.main()
