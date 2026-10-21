"""Courier command line: configuration files, terminals, and confirmations."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from opencode_connect.config import ROOT
from opencode_connect.core import BridgeError
from opencode_connect.courier.app import Courier, RequestError
from opencode_connect.courier.cli import main, real_queue
from opencode_connect.courier.settings import (contact_url, interactive_confirmation, listen_address, person,
                                               read_config, write_config)
from tests.courier.fakes import FakeQueue, configuration


class CourierConfigurationTests(unittest.TestCase):
    def test_wildcards_require_explicit_flag(self):
        for address in (str(__import__("ipaddress").ip_address(0)) + ":8765", "[::]:8765"):
            with self.subTest(address=address), self.assertRaises(BridgeError):
                listen_address(address)
            self.assertEqual(listen_address(address, True)[1], 8765)
        for address in ("localhost:8765", "192.0.2.10:-1", "192.0.2.10:65536"):
            with self.subTest(address=address), self.assertRaises(BridgeError):
                listen_address(address)

    def test_contact_urls_and_names_are_strict(self):
        for url in ("file:///example", "http://user:password@192.0.2.10", "http://192.0.2.10/path",
                    "http://192.0.2.10#fragment", "http://192.0.2.10:0", "http://192.0.2.10\n"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                contact_url(url)
        for name in ("Bob", "alice\n", "a/b", "", "../alice"):
            with self.subTest(name=name), self.assertRaises(BridgeError):
                person(name)

    def test_config_permissions_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory(prefix="courier-config-") as folder:
            path = Path(folder) / "courier.json"
            write_config(path, configuration())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_config(path), configuration())
            path.chmod(0o644)
            with self.assertRaises(BridgeError):
                read_config(path)
            with self.assertRaises(BridgeError):
                write_config(path, configuration())
            path.chmod(0o600)
            link = Path(folder) / "linked.json"
            link.symlink_to(path)
            with self.assertRaises(BridgeError):
                read_config(link)

    def test_revoked_contact_cannot_be_resurrected_by_stale_config_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="courier-restart-") as folder:
            path = Path(folder) / "courier.json"
            queue = FakeQueue(Path(folder) / "queue.sqlite3")
            self.addCleanup(queue.db.close)
            write_config(path, configuration())
            app = Courier(queue, read_config(path), config_path=path)
            card = app.invite("bob")
            snapshot = read_config(path)
            self.assertEqual(snapshot["tokens"]["bob"], hashlib.sha256(card["token"].encode()).hexdigest())
            self.assertNotIn(card["token"], path.read_text())
            with patch.object(app, "sync_config", side_effect=OSError("Simulated interrupted snapshot write.")):
                with self.assertRaises(OSError):
                    app.untrust("bob")
            restarted = Courier(queue, snapshot, config_path=path)
            self.assertEqual(restarted.contacts(), [])
            with self.assertRaises(RequestError):
                restarted.authenticate("Bearer " + card["token"])
            self.assertEqual(read_config(path)["tokens"], {})

    def test_confirmation_requires_both_ttys_and_fresh_five_letter_code(self):
        for input_tty, output_tty in ((False, True), (True, False), (False, False)):
            with patch("sys.stdin.isatty", return_value=input_tty), patch("sys.stdout.isatty", return_value=output_tty):
                with self.assertRaises(BridgeError):
                    interactive_confirmation("Approve")
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
                patch("opencode_connect.courier.settings.secrets.choice", side_effect=list("ABCDE")), \
                patch("builtins.input", return_value="ABCDE"), \
                patch("builtins.print") as output:
            interactive_confirmation("Approve")
            self.assertIn("ABCDE", output.call_args.args[0])
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
                patch("builtins.input", return_value="incorrect"), patch("builtins.print"):
            with self.assertRaises(BridgeError):
                interactive_confirmation("Approve")

    def test_cli_noninteractive_approval_denied_before_config_or_queue(self):
        for arguments in (["approve", "example", "--to", "wsl:ses_example"], ["invite", "bob"],
                          ["trust", "bob", "--card", "{}"], ["service", "install"], ["service", "uninstall"],
                          ["service", "start"], ["service", "stop"]):
            # A temporary configuration home: even if a refusal regressed, no unit or
            # configuration could land in the real one.
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory(prefix="courier-home-") as home, \
                    patch.dict(os.environ, {"XDG_CONFIG_HOME": home}), patch("sys.stdin.isatty", return_value=False), \
                    patch("sys.stderr", new_callable=io.StringIO), \
                    patch("opencode_connect.courier.cli.read_config") as read, \
                    patch("opencode_connect.courier.service.subprocess.run") as run:
                self.assertEqual(main(arguments, queue_factory=Mock()), 1)
                read.assert_not_called()
                run.assert_not_called()
                self.assertEqual(list(Path(home).iterdir()), [])

    def test_real_queue_factory_uses_local_mailbox_and_unchanged_environment(self):
        with tempfile.TemporaryDirectory(prefix="courier-cli-") as folder:
            path = Path(folder) / "courier.json"
            import os
            from opencode_connect.local.mailbox import LocalMailbox
            with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "wsl",
                                         "OPENCODE_CONNECT_LOCAL_STATE_HOME": folder}):
                queue = real_queue(path)
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
            write_config(path, configuration())
            factory = lambda config_path: FakeQueue(database)
            def command(*arguments):
                with patch("opencode_connect.courier.cli.interactive_confirmation") as confirm, \
                        patch("sys.stdout", new_callable=io.StringIO) as output:
                    result = main(["--config", str(path), *arguments], queue_factory=factory)
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
                app = Courier(queue, read_config(path))
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
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/courier.py"), "--help"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("consent-gated", result.stdout)
