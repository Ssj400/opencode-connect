import copy
import json
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bridge
import config
import transports


def fixture():
    return {"side": "linux", "peers": {
        "linux": {"side": "linux", "sessionID": "ses_local"},
        "wsl": {"side": "wsl", "sessionID": "ses_remote"}},
        "ssh": {"host": "192.0.2.10", "ssh_user": "Example Usér", "wsl_user": "example",
                "remote_opencode": "/opt/Example App/opencode", "remote_directory": "/srv/Example Project"}}


class ConfigTests(unittest.TestCase):
    def test_valid_unicode_spaces(self):
        self.assertEqual(config.validate_config(fixture()), fixture())

    def test_invalid_config_fails_before_transport(self):
        changes = [("side", "other"), ("peers", []), ("ssh", {})]
        with patch("transports.subprocess.run") as run:
            for field, value in changes:
                cfg = fixture(); cfg[field] = value
                with self.subTest(field=field), self.assertRaises(config.BridgeError):
                    config.validate_config(cfg)
            run.assert_not_called()

    def test_unsafe_transport_values(self):
        changes = [("host", "-oProxyCommand=bad"), ("host", "bad host"),
                   ("ssh_user", "name\nextra"), ("wsl_user", 'example & command'),
                   ("remote_opencode", "relative/path"), ("remote_directory", "/srv/../etc"),
                   ("remote_directory", "/"), ("remote_directory", "/srv/x\x00")]
        with patch("transports.subprocess.run") as run:
            for field, value in changes:
                cfg = fixture(); cfg["ssh"][field] = value
                with self.subTest(field=field), self.assertRaises(config.BridgeError):
                    bridge.Endpoint(SimpleNamespace(**cfg["ssh"]))
            run.assert_not_called()

    def test_invalid_peers(self):
        for field, value in (("side", "other"), ("sessionID", "ses_REPLACE_LINUX"),
                             ("sessionID", "../path")):
            cfg = fixture(); cfg["peers"]["linux"][field] = value
            with self.subTest(field=field), self.assertRaises(config.BridgeError):
                config.validate_config(cfg)
        cfg = fixture(); cfg["peers"]["duplicate"] = copy.deepcopy(cfg["peers"]["linux"])
        with self.assertRaises(config.BridgeError):
            config.validate_config(cfg)
        del cfg["peers"]["duplicate"]; del cfg["peers"]["wsl"]
        self.assertEqual(config.validate_config(cfg), cfg)

    def test_missing_config_is_not_inherited(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(config.BridgeError, "Missing routes.json"):
                config.load_config(Path(folder))
            (Path(folder) / "routes.json").write_text("invalid")
            with self.assertRaises(config.BridgeError):
                config.load_config(Path(folder))


class BridgeTests(unittest.TestCase):
    def test_check_small_read_only_requests(self):
        endpoint = Mock()
        endpoint.call.side_effect = [{"version": "2.0.0"}, {"data": []}]
        with patch("builtins.print"):
            bridge.check([("Linux", endpoint)])
        self.assertEqual(endpoint.call.call_args_list, [
            unittest.mock.call("get", "/api/info"),
            unittest.mock.call("get", "/api/session?limit=1")])

    def test_check_identifies_endpoint(self):
        endpoint = Mock(); endpoint.call.side_effect = config.BridgeError("Invalid JSON")
        with self.assertRaisesRegex(config.BridgeError, "WSL, GET /api/info"):
            bridge.check([("WSL", endpoint)])

    def test_response_decoding(self):
        self.assertIsNone(transports.decode_response(b""))
        self.assertEqual(transports.decode_response('{"text":"ñ"}'), {"text": "ñ"})
        with self.assertRaises(config.BridgeError):
            transports.decode_response("not json")
        # Decoding generic JSON must not interpret a harness-specific error schema.
        self.assertEqual(transports.decode_response('{"_tag":"Example"}'), {"_tag": "Example"})

    @patch("transports.subprocess.run")
    def test_timeout_does_not_retry(self, run):
        run.side_effect = subprocess.TimeoutExpired("opencode", 1)
        with self.assertRaises(config.BridgeError):
            transports.run_json(["opencode"], timeout=1)
        self.assertEqual(run.call_count, 1)

    @patch("transports.socket_path", return_value="/example/control.sock")
    @patch("transports.subprocess.run")
    def test_remote_payload_stays_out_of_shell(self, run, socket):
        args = SimpleNamespace(**fixture()["ssh"], local_opencode="opencode")
        run.return_value = subprocess.CompletedProcess([], 0, b'{"data":{}}', b"")
        text = 'Hello, Unicode: ñ 🚀 " & example.exe | %PATH%\n' * 1000
        bridge.Endpoint(args, remote=True).call("post", "/api/session/ses_remote/prompt", {"text": text})
        command = run.call_args.args[0]
        payload = json.loads(run.call_args.kwargs["input"])["argv"]
        self.assertNotIn(text, " ".join(command))
        self.assertNotIn(args.remote_opencode, command[-1])
        self.assertEqual(json.loads(payload[-1])["text"], text)
        self.assertLess(len(command[-1]), 8191)
        self.assertIn("BatchMode=yes", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)

    def test_socket_runtime_private(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"XDG_RUNTIME_DIR": folder}):
            path = transports.socket_directory()
            self.assertEqual(path, Path(folder) / "opencode-connect")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_socket_cache_fallback(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"XDG_RUNTIME_DIR": "", "XDG_CACHE_HOME": folder}):
            self.assertEqual(transports.socket_directory(), Path(folder) / "opencode-connect")

    def test_socket_length_and_identity(self):
        # Directory creation is tested separately, independent of TMPDIR length.
        with patch("transports.socket_directory", return_value=Path("/example")):
            first = transports.socket_path("example", "192.0.2.10")
            self.assertEqual(first, transports.socket_path("example", "192.0.2.10"))
            self.assertNotEqual(first, transports.socket_path("other", "192.0.2.10"))
        with patch("transports.socket_directory", return_value=Path("/" + "x" * 101)):
            with self.assertRaisesRegex(config.BridgeError, "too long"):
                transports.socket_path("example", "192.0.2.10")

    def test_socket_rejects_insecure_and_symlink(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); insecure = root / "insecure"; insecure.mkdir(mode=0o755)
            # Do not depend on the process umask inherited from other tests.
            insecure.chmod(0o755)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(insecure)}):
                with self.assertRaises(config.BridgeError):
                    transports.socket_path("example", "192.0.2.10")
            link = root / "link"; link.symlink_to(root, target_is_directory=True)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(link)}):
                with self.assertRaises(config.BridgeError):
                    transports.socket_path("example", "192.0.2.10")


if __name__ == "__main__":
    unittest.main()
