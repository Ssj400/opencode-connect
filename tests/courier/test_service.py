"""The optional systemd user unit, with systemctl replaced."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from opencode_connect.core import BridgeError
from opencode_connect.courier.cli import main
from opencode_connect.courier.service import (LAUNCHER, SERVICE, install_service, render_unit,
                                              service_environment, systemctl, uninstall_service, unit_word)
from opencode_connect.courier.settings import write_config
from tests.courier.fakes import configuration


class ServiceTests(unittest.TestCase):
    """The systemd user unit, with systemctl replaced and a temporary XDG_CONFIG_HOME."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-service-")
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = self.home / "opencode-connect/courier.json"
        write_config(self.config, configuration())
        self.unit = self.home / "systemd/user" / SERVICE

    def systemctl(self, returncode=0, stderr=""):
        return patch("opencode_connect.courier.service.subprocess.run", return_value=subprocess.CompletedProcess(
            [], returncode, stdout="", stderr=stderr))

    def test_the_unit_runs_the_relay_with_absolute_paths_and_stops_cleanly(self):
        text = render_unit("/opt/python 3/bin/python3", "/srv/connect/courier.py", self.config,
                                   {"PATH": "/opt/opencode/bin:/usr/bin", "OPENCODE_CONNECT_LOCAL_NODE": "linux"})
        lines = text.splitlines()
        self.assertIn(f'ExecStart="/opt/python 3/bin/python3" "-B" "/srv/connect/courier.py" '
                      f'"--config" "{self.config}" "run" "--relay"', lines)
        self.assertIn('Environment="PATH=/opt/opencode/bin:/usr/bin"', lines)
        self.assertIn('Environment="OPENCODE_CONNECT_LOCAL_NODE=linux"', lines)
        for setting in ("Restart=on-failure", "RestartSec=5", "StartLimitIntervalSec=0", "KillMode=mixed",
                        "TimeoutStopSec=45", "WantedBy=default.target"):
            self.assertIn(setting, lines)
        with self.assertRaises(BridgeError):
            render_unit("python3", "/srv/connect/courier.py", self.config, {})

    def test_unit_words_cannot_expand_or_break_out(self):
        self.assertEqual(unit_word('/a "b" \\c 100% $HOME', command=True), r'"/a \"b\" \\c 100%% $$HOME"')
        self.assertEqual(unit_word("KEY=50% $x"), '"KEY=50%% $x"')
        for value in ("/a\nExecStartPre=/bin/false", "/a\tb"):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                unit_word(value)

    def test_the_environment_keeps_this_terminal_opencode_and_queue(self):
        environ = {"PATH": "/opt/opencode/bin:/usr/bin", "OPENCODE_CONNECT_LOCAL_STATE_HOME": "/srv/state",
                   "XDG_STATE_HOME": "", "HOME": "/home/example", "SECRET_TOKEN": "x"}
        with patch("opencode_connect.courier.service.shutil.which", return_value="/opt/opencode/bin/opencode") as which:
            environment, opencode = service_environment(environ)
        which.assert_called_once_with("opencode", path="/opt/opencode/bin:/usr/bin")
        self.assertEqual(opencode, "/opt/opencode/bin/opencode")
        self.assertEqual(environment, {"PATH": "/opt/opencode/bin:/usr/local/bin:/usr/bin:/bin",
                                       "OPENCODE_CONNECT_LOCAL_STATE_HOME": "/srv/state"})
        with patch("opencode_connect.courier.service.shutil.which", return_value=None):
            self.assertEqual(service_environment(environ)[0]["PATH"], "/usr/local/bin:/usr/bin:/bin")

    def test_install_writes_the_unit_once_then_enables_it(self):
        with self.systemctl() as run, \
                patch("opencode_connect.courier.service.shutil.which", return_value="/opt/opencode/bin/opencode"):
            path, opencode = install_service(self.config)
            self.assertEqual(path, self.unit)
            self.assertEqual(self.unit.stat().st_mode & 0o777, 0o600)
            environment = service_environment(os.environ)[0]
            self.assertEqual(self.unit.read_text(), render_unit(
                sys.executable, LAUNCHER, self.config, environment))
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", SERVICE]])
            install_service(self.config)  # Same unit: enabling again is harmless.

    def test_install_never_overwrites_a_different_unit(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Service]\nExecStart=/bin/true\n")
        with self.systemctl() as run, self.assertRaisesRegex(BridgeError, "not overwritten"):
            install_service(self.config)
        run.assert_not_called()
        self.assertEqual(self.unit.read_text(), "[Service]\nExecStart=/bin/true\n")

    def test_install_requires_a_readable_configuration(self):
        self.config.unlink()
        with self.systemctl() as run, self.assertRaises(BridgeError):
            install_service(self.config)
        run.assert_not_called()
        self.assertFalse(self.unit.exists())

    def test_uninstall_removes_only_its_own_unit(self):
        with self.systemctl() as run:
            install_service(self.config)
            run.reset_mock()
            self.assertEqual(uninstall_service(), self.unit)
        self.assertFalse(self.unit.exists())
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemctl", "--user", "disable", "--now", SERVICE], ["systemctl", "--user", "daemon-reload"]])
        self.unit.write_text("[Service]\nExecStart=/bin/true\n")
        with self.systemctl() as run, self.assertRaisesRegex(BridgeError, "left untouched"):
            uninstall_service()
        run.assert_not_called()
        self.assertTrue(self.unit.exists())

    def test_a_systemctl_failure_shows_its_reason(self):
        with self.systemctl(1, "Failed to connect to bus:\nNo medium found\n"), \
                self.assertRaisesRegex(BridgeError, "Failed to connect to bus: No medium found"):
            systemctl("daemon-reload")

    def test_status_and_logs_need_neither_a_terminal_nor_the_configuration(self):
        for action, program in (("status", "systemctl"), ("logs", "journalctl")):
            with self.subTest(action=action), self.systemctl(3) as run, \
                    patch("opencode_connect.courier.cli.read_config") as read, \
                    patch("sys.stdin.isatty", return_value=False):
                self.assertEqual(main(["service", action], queue_factory=Mock()), 3)
                self.assertEqual(run.call_args.args[0][0], program)
                self.assertIn(SERVICE, run.call_args.args[0])
                read.assert_not_called()
