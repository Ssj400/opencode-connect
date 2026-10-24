"""The menu `courier.py` opens without arguments: it runs the same commands, chosen from lists."""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from opencode_connect.core import BridgeError
from opencode_connect.courier import menu
from opencode_connect.courier.app import Courier
from opencode_connect.courier.menu import run_menu
from opencode_connect.courier.settings import write_config
from opencode_connect.local import mailbox
from tests.courier.test_setup import VPN
from tests.courier.fakes import configuration

CLAUDE = "claude:" + str(uuid.UUID(int=4))
ROWS = [{"address": "local:ses_example", "label": "OpenCode: Review (/srv/example)"},
        {"address": CLAUDE, "label": "Claude Code: /srv/example, seen 2 min ago"}]
LINK = "http://192.0.2.10:8765#ABCD-EFGH-JKMN"


class MenuTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-menu-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "courier.json"
        self.factory = Mock()

    def menu(self, *answers, contacts=("alice", "bob"), rows=ROWS, connect=None, lost=None):
        """The menu with `answers` typed in order; then the input ends, as with Ctrl+D."""
        typed = list(answers)

        def ask(prompt=""):
            if not typed:
                raise EOFError
            return typed.pop(0)

        with patch("builtins.input", side_effect=ask), \
                patch("opencode_connect.courier.cli.main", return_value=0) as main, \
                patch("opencode_connect.courier.menu.contacts", return_value=list(contacts)), \
                patch("opencode_connect.courier.menu.destinations", return_value=list(rows)), \
                patch("opencode_connect.courier.menu.connect_project", side_effect=connect) as connected, \
                patch("opencode_connect.courier.menu.host.lost_address", return_value=lost), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = run_menu(self.path, self.factory)
        self.assertEqual(result, 0)
        self.assertEqual(typed, [], "Answers left unread")
        for call in main.call_args_list:
            self.assertEqual(call.args[0][:2], ["--config", str(self.path)])
            self.assertIs(call.kwargs["queue_factory"], self.factory)
        return [call.args[0][2:] for call in main.call_args_list], connected, output.getvalue()

    def test_each_choice_runs_the_same_command_as_the_terminal(self):
        commands, _, output = self.menu("1", "", "2", "bob", "", "2", "", "", "3", LINK, "", "3", "", "",
                                        "5", "1", "", "6", "", "0")
        self.assertEqual(commands, [["setup"], ["pair", "--for", "bob"], ["pair"], ["pair", LINK],
                                    ["unpair", "alice"], ["status"]])
        self.assertIn("not set up", output)

    def test_a_route_is_chosen_from_lists_or_typed(self):
        commands, _, output = self.menu("4", "2", "2", "2", "",
                                        "4", "1", "3", "wsl:ses_example", "1", "",
                                        "4", "1", "0", "", "4", "0", "", "0")
        self.assertEqual(commands, [["contact", "bob", "--to", CLAUDE, "--direct"],
                                    ["contact", "alice", "--to", "wsl:ses_example", "--ask"]])
        self.assertIn("Claude Code: /srv/example", output)
        self.assertIn(CLAUDE, output)

    def test_a_new_address_is_chosen_from_this_machines_and_keeps_the_port(self):
        write_config(self.path, {**configuration(), "listen": "127.0.0.1:9000"})
        vpn = {"interface": "xsred0", "address": VPN, "network": VPN + "/32", "kind": "VPN"}
        with patch("opencode_connect.courier.menu.host.interface_addresses", return_value=[vpn]), \
                patch("opencode_connect.courier.menu.host.on_wsl", return_value=False), \
                patch("opencode_connect.courier.setup.can_bind") as bind:
            commands, _, output = self.menu("7", "", "", "", lost="127.0.0.1")
        self.assertEqual(commands, [["address", "--listen", VPN + ":9000", "--url", "http://" + VPN + ":9000"]])
        bind.assert_called_once_with(VPN, 9000)
        self.assertIn("Warning: ", output)
        self.assertIn("127.0.0.1 is not an address of this machine now", output)

    def test_changing_the_address_needs_a_setup_and_an_address(self):
        commands, _, output = self.menu("7", "")
        self.assertIn("not set up yet: choose 1 first", output)
        write_config(self.path, configuration())
        with patch("opencode_connect.courier.menu.host.interface_addresses", return_value=[]):
            commands, _, output = self.menu("7", "")
        self.assertEqual(commands, [])
        self.assertIn("No LAN or VPN address found", output)

    def test_without_contacts_there_is_nothing_to_route_or_remove(self):
        commands, _, output = self.menu("4", "", "5", "", contacts=())
        self.assertEqual(commands, [])
        self.assertEqual(output.count("No contacts yet"), 2)

    def test_connecting_a_project_defaults_to_this_folder_and_reports_refusals(self):
        _, connected, output = self.menu("8", "", "", "8", "/srv/elsewhere", "",
                                         connect=[0, BridgeError("/srv/elsewhere is not a folder.")])
        self.assertEqual([call.args[0] for call in connected.call_args_list],
                         [Path(os.getcwd()), Path("/srv/elsewhere")])
        self.assertIn("is not a folder", output)

    def test_addresses_are_listed_and_wrong_choices_are_asked_again(self):
        commands, _, output = self.menu("9", "", "10", "x")
        self.assertEqual(commands, [])
        for row in ROWS:
            self.assertIn(row["address"], output)
        self.assertEqual(output.count("Type a number from 0 to 9."), 2)
        _, _, output = self.menu("9", "", rows=[])
        self.assertIn("No sessions found", output)


class MenuListTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-menu-lists-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_destinations_list_opencode_then_claude_sessions_and_survive_a_missing_opencode(self):
        adapter = Mock()
        adapter.recent_sessions.return_value = [{"id": "ses_example", "title": "", "directory": "/srv/example",
                                                 "updated": 1}]
        seen = [{"address": CLAUDE, "cwd": None, "seen": 0.0}]
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "wsl"}), \
                patch("opencode_connect.courier.menu.local_adapter", return_value=adapter), \
                patch("opencode_connect.courier.menu.claude_hook.sessions", return_value=seen):
            rows = menu.destinations()
            adapter.recent_sessions.side_effect = BridgeError("The local OpenCode service is unavailable.")
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                fallback = menu.destinations()
        self.assertEqual([row["address"] for row in rows], ["wsl:ses_example", CLAUDE])
        self.assertIn("untitled", rows[0]["label"])
        self.assertIn("unknown folder", rows[1]["label"])
        self.assertEqual([row["address"] for row in fallback], [CLAUDE])
        self.assertIn("could not be listed", output.getvalue())

    def test_contacts_come_from_the_queue_without_changing_it(self):
        path = self.root / "courier.json"
        self.assertEqual(menu.contacts(path), [])
        write_config(path, configuration())
        box = mailbox.LocalMailbox(self.root / "queue", node="wsl")
        self.addCleanup(box.db.close)
        (box.state / "mail.sqlite3").chmod(0o600)
        app = Courier(box, configuration(), config_path=path)
        with patch.object(app, "sync_config"):  # The JSON copy misses both: the queue decides.
            app.invite("carol")
            app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "B" * 43})
        before = (box.state / "mail.sqlite3").read_bytes()
        with patch("opencode_connect.courier.menu.local_queue_root", return_value=self.root / "queue"):
            self.assertEqual(menu.contacts(path), ["bob", "carol"])
        self.assertEqual((box.state / "mail.sqlite3").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
