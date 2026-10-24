"""Connecting a project's OpenCode and Claude Code sessions to the local queue."""

import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from opencode_connect.config import ROOT
from opencode_connect.core import BridgeError
from opencode_connect.courier import connect
from opencode_connect.courier.connect import claude_settings, connect_project


class FakeHarnesses:
    """`subprocess.run` for opencode, claude and git, with what each project already has."""

    def __init__(self, opencode=False, claude=False, ignored=False, fail=None):
        self.opencode, self.claude, self.ignored, self.fail = opencode, claude, ignored, fail
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs.get("cwd")))
        if command[:2] == ["git", "-C"]:
            return subprocess.CompletedProcess(command, 0 if self.ignored else 1, "", "")
        if command[:3] == ["opencode", "mcp", "list"]:
            output = "✓ local_bridge  connected\n" if self.opencode else "No MCP servers configured\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:3] == ["claude", "mcp", "get"]:
            return subprocess.CompletedProcess(command, 0 if self.claude else 1, "", "")
        if self.fail == command[0]:
            return subprocess.CompletedProcess(command, 1, "", "Server local_bridge already exists\x1b[31m")
        return subprocess.CompletedProcess(command, 0, "", "")

    def changes(self):
        return [command for command, _ in self.calls if command[2:3] == ["add"]]


class ConnectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="connect-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.project = self.root / "project"
        (self.project / ".git/info").mkdir(parents=True)
        self.settings = self.project / ".claude/settings.local.json"
        environment = patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def connect(self, system, *, confirm=None, which=("opencode", "claude", "git"), relay="the courier's service"):
        with patch("opencode_connect.courier.connect.subprocess.run", system), \
                patch("opencode_connect.courier.connect.relay_status", return_value=relay), \
                patch("opencode_connect.courier.connect.shutil.which",
                      side_effect=lambda name: f"/usr/bin/{name}" if name in which else None), \
                patch("opencode_connect.courier.connect.interactive_confirmation", side_effect=confirm) as code, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = connect_project(self.project)
        return result, output.getvalue(), code

    def test_a_new_project_gets_both_servers_the_hooks_and_a_local_git_exclude_after_one_code(self):
        system = FakeHarnesses()
        result, output, code = self.connect(system)
        self.assertEqual(result, 0)
        code.assert_called_once()
        self.assertEqual([(command[:4], cwd) for command, cwd in system.calls if command[2:3] == ["add"]],
                         [(["opencode", "mcp", "add", "local_bridge"], self.project),
                          (["claude", "mcp", "add", "opencode_connect"], self.project)])
        self.assertEqual(json.loads(self.settings.read_text()), claude_settings())
        self.assertIn(".claude/settings.local.json", (self.project / ".git/info/exclude").read_text())
        self.assertIn("new Claude Code session", output)
        # A second run finds everything in place and asks for nothing.
        system = FakeHarnesses(opencode=True, claude=True, ignored=True)
        result, output, code = self.connect(system)
        self.assertEqual((result, system.changes()), (0, []))
        code.assert_not_called()
        self.assertIn("already connected", output)
        self.assertEqual((self.project / ".git/info/exclude").read_text().count("settings.local.json"), 1)

    def test_existing_settings_are_kept_and_hooks_are_not_duplicated(self):
        self.settings.parent.mkdir()
        mine = {"model": "opus", "permissions": {"allow": ["Bash(git *)"], "deny": ["Bash(rm *)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
                          "UserPromptSubmit": [{"hooks": [{"type": "command",
                                                           "command": "python3 /old/checkout/bin/claude_hook.py prompt"}]}]}}
        self.settings.write_text(json.dumps(mine))
        self.settings.chmod(0o640)
        self.connect(FakeHarnesses(opencode=True, claude=True, ignored=True))
        merged = json.loads(self.settings.read_text())
        self.assertEqual((merged["model"], merged["permissions"]["allow"]), ("opus", ["Bash(git *)"]))
        self.assertEqual(merged["permissions"]["deny"][0], "Bash(rm *)")
        self.assertLessEqual(set(claude_settings()["permissions"]["deny"]), set(merged["permissions"]["deny"]))
        commands = [hook["command"] for groups in merged["hooks"].values() for group in groups for hook in group["hooks"]]
        self.assertIn("notify-send done", commands)
        self.assertEqual(sum(command.endswith("claude_hook.py prompt") for command in commands), 1)
        self.assertEqual(sum(command.endswith("claude_hook.py wake") for command in commands), 1)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)

    def test_unreadable_settings_stop_everything_before_the_code(self):
        self.settings.parent.mkdir()
        for content in ("{not json", "[]", '{"hooks": []}', '{"permissions": {"deny": "all"}}'):
            with self.subTest(content=content):
                self.settings.write_text(content)
                system = FakeHarnesses()
                with self.assertRaises(BridgeError):
                    self.connect(system)
                self.assertEqual((system.changes(), self.settings.read_text()), ([], content))
        self.settings.unlink()
        self.settings.symlink_to(self.root / "elsewhere.json")
        with self.assertRaisesRegex(BridgeError, "symbolic link"):
            self.connect(FakeHarnesses())
        self.assertFalse((self.root / "elsewhere.json").exists())

    def test_without_the_code_nothing_runs_or_is_written(self):
        system = FakeHarnesses()
        with self.assertRaises(BridgeError):
            self.connect(system, confirm=BridgeError("Confirmation did not match; nothing was changed."))
        self.assertEqual(system.changes(), [])
        self.assertFalse(self.settings.exists())
        self.assertFalse((self.project / ".git/info/exclude").exists())

    def test_the_queue_variables_reach_the_servers_and_the_hooks(self):
        os.environ.update({"OPENCODE_CONNECT_LOCAL_NODE": "wsl", "OPENCODE_CONNECT_LOCAL_STATE_HOME": "/srv/state"})
        system = FakeHarnesses(ignored=True)
        self.connect(system)
        opencode, claude = system.changes()
        self.assertEqual(opencode[3:7], ["--env", "OPENCODE_CONNECT_LOCAL_NODE=wsl",
                                         "--env", "OPENCODE_CONNECT_LOCAL_STATE_HOME=/srv/state"])
        self.assertIn("-e", claude)
        command = json.loads(self.settings.read_text())["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(command)[:3], ["env", "OPENCODE_CONNECT_LOCAL_NODE=wsl",
                                                    "OPENCODE_CONNECT_LOCAL_STATE_HOME=/srv/state"])
        self.assertIn("Read(/srv/state/opencode-connect/**)", json.loads(self.settings.read_text())["permissions"]["deny"])

    def test_a_missing_harness_is_skipped_and_a_failed_command_stops(self):
        system = FakeHarnesses(ignored=True)
        result, output, _ = self.connect(system, which=("claude", "git"))
        self.assertEqual([command[0] for command in system.changes()], ["claude"])
        self.assertIn("OpenCode is not installed", output)
        self.settings.unlink()
        system = FakeHarnesses(ignored=True, fail="opencode")
        with self.assertRaisesRegex(BridgeError, "already exists") as caught:
            self.connect(system)
        self.assertNotIn("\x1b", str(caught.exception))
        self.assertEqual([command[0] for command in system.changes()], ["opencode"])
        self.assertFalse(self.settings.exists())

    def test_it_says_what_delivers_to_opencode_sessions_or_how_to_start_it(self):
        _, output, _ = self.connect(FakeHarnesses(opencode=True, claude=True, ignored=True))
        self.assertIn("delivered by the courier's service", output)
        os.environ["OPENCODE_CONNECT_LOCAL_NODE"] = "wsl"
        _, output, _ = self.connect(FakeHarnesses(opencode=True, claude=True, ignored=True), relay=None)
        self.assertIn(f"env OPENCODE_CONNECT_LOCAL_NODE=wsl python3 {ROOT}/bin/local_bridge.py relay", output)
        self.assertIn("model quota", output)

    def test_the_relay_is_found_as_a_service_or_a_process_without_changing_anything(self):
        unit = self.root / "unit.service"
        unit.write_text("[Unit]\n")
        with patch("opencode_connect.courier.connect.unit_path", return_value=unit), \
                patch("opencode_connect.courier.connect.command_output", return_value="active") as systemctl, \
                patch("opencode_connect.courier.connect.separate_relays", return_value=[]), \
                patch("opencode_connect.courier.connect.courier_relays", return_value=[]):
            self.assertEqual(connect.relay_status(), "the courier's service")
            systemctl.assert_called_once_with(["systemctl", "--user", "is-active", "opencode-connect-courier.service"])
            systemctl.return_value = "inactive"
            self.assertIsNone(connect.relay_status())
        with patch("opencode_connect.courier.connect.unit_path", return_value=self.root / "missing.service"), \
                patch("opencode_connect.courier.connect.command_output") as systemctl, \
                patch("opencode_connect.courier.connect.separate_relays", return_value=[]), \
                patch("opencode_connect.courier.connect.courier_relays", return_value=[4242]):
            self.assertEqual(connect.relay_status(), "courier.py run --relay")
        systemctl.assert_not_called()

    def test_the_generated_settings_are_the_documented_example(self):
        example = (ROOT / "examples/claude-settings.local.json.example").read_text()
        self.assertEqual(claude_settings(), json.loads(example.replace("/srv/opencode-connect", str(ROOT))))

    def test_a_folder_that_is_not_a_project_is_refused(self):
        with self.assertRaises(BridgeError):
            connect.connect_project(self.root / "missing")


if __name__ == "__main__":
    unittest.main()
