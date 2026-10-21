import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from opencode_connect.bridge import bridge
from opencode_connect.bridge import collab
from opencode_connect import config
from opencode_connect.bridge import deploy
from opencode_connect import transports


CLAUDE_ADDRESS = "claude:" + str(uuid.UUID(int=1))


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
        with patch("opencode_connect.transports.subprocess.run") as run:
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
        with patch("opencode_connect.transports.subprocess.run") as run:
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
    def test_command_failure_preserves_output_and_uses_stdout_without_ssh_warning(self):
        stdout = b'{"_tag":"BridgeError","message":"Insecure private directory: /srv/example"}'
        stderr = b'** WARNING: connection is not using a post-quantum key exchange algorithm.\n'
        with patch("opencode_connect.transports.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout=stdout, stderr=stderr)) as run:
            with self.assertRaises(transports.CommandError) as caught:
                transports.run_json(["example"])
        error = caught.exception
        self.assertEqual((error.returncode, error.stdout, error.stderr), (1, stdout, stderr))
        self.assertEqual(str(error), stdout.decode())
        run.assert_called_once()

    def test_command_failure_filters_stderr_and_bounds_stdout_fallback(self):
        for stdout, stderr, expected in (
                (b"not JSON", b"** advisory\n Permission denied.\n", "Permission denied."),
                (b"x" * 1000, b"** advisory\n", "x" * 300),
                (b"", b"** advisory\n", "Command exited with status 7.")):
            with self.subTest(expected=expected), patch("opencode_connect.transports.subprocess.run", return_value=
                    SimpleNamespace(returncode=7, stdout=stdout, stderr=stderr)):
                with self.assertRaises(transports.CommandError) as caught:
                    transports.run_json(["example"])
                self.assertEqual(str(caught.exception), expected)

    def test_command_timeout_remains_a_bridge_error_without_retry(self):
        with patch("opencode_connect.transports.subprocess.run", side_effect=subprocess.TimeoutExpired(
                ["example"], 3)) as run:
            with self.assertRaises(config.BridgeError) as caught:
                transports.run_json(["example"], timeout=3)
        self.assertNotIsInstance(caught.exception, transports.CommandError)
        self.assertEqual(str(caught.exception), "Request timed out; the message will not be retried.")
        run.assert_called_once()

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

    @patch("opencode_connect.transports.subprocess.run")
    def test_timeout_does_not_retry(self, run):
        run.side_effect = subprocess.TimeoutExpired("opencode", 1)
        with self.assertRaises(config.BridgeError):
            transports.run_json(["opencode"], timeout=1)
        self.assertEqual(run.call_count, 1)

    @patch("opencode_connect.transports.socket_path", return_value="/example/control.sock")
    @patch("opencode_connect.transports.subprocess.run")
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
        with patch("opencode_connect.transports.socket_directory", return_value=Path("/example")):
            first = transports.socket_path("example", "192.0.2.10")
            self.assertEqual(first, transports.socket_path("example", "192.0.2.10"))
            self.assertNotEqual(first, transports.socket_path("other", "192.0.2.10"))
        with patch("opencode_connect.transports.socket_directory", return_value=Path("/" + "x" * 101)):
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


class DoctorTests(unittest.TestCase):
    """Doctor wiring with every effect replaced; each probe is tested in its own layer."""

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="doctor-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "routes.json").write_text(json.dumps(fixture()))
        self.files = ("example.py", "docs/example.md")
        for name in self.files:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(f"content of {name}")
        hashes = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in self.files}
        self.inspection = {"python": [3, 12, 3], "installed": True, "files": dict(hashes),
                           "manifest": dict(hashes), "state_private": True,
                           "config": {"side": "wsl", "peers": fixture()["peers"]},
                           "queue": {"delivered": 2}, "queue_error": None}
        self.remote_api, self.master = "2.0.26", True
        self.probe = transports.Probe(True, "open", "SSH-2.0-Example")
        self.requests, self.mocks = [], {}
        for target, kwargs in (
                ("opencode_connect.bridge.deploy.FILES", {"new": self.files}),
                ("opencode_connect.bridge.bridge.tcp_probe", {"side_effect": lambda host, port: self.probe}),
                ("opencode_connect.transports.WindowsWslSshTransport.destination", {"return_value": ("192.0.2.10", 22)}),
                ("opencode_connect.transports.WindowsWslSshTransport.master_alive", {"side_effect": lambda: self.master}),
                ("opencode_connect.bridge.bridge.probe_api", {"side_effect": self.api}),
                ("opencode_connect.bridge.bridge.Endpoint.remote_exec", {"side_effect": self.execute})):
            patcher = patch(target, **kwargs)
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)

    def api(self, endpoint):
        result = self.remote_api if endpoint.remote else "2.0.32"
        if isinstance(result, Exception):
            raise result
        return result

    def execute(self, argv, timeout=30, cwd=None, data=None):
        self.requests.append((argv, json.loads(data)))
        return self.inspection

    def finding(self, findings, area, status=None):
        matches = [item for item in findings if item[1] == area and status in (None, item[0])]
        self.assertTrue(matches, f"no {status or 'any'} finding for {area}: {findings}")
        return matches[0]

    def area_summaries(self, findings, area, status):
        return [summary for state, name, summary, _ in findings if name == area and state == status]

    def test_healthy_pair_reports_every_layer_without_failures(self):
        findings = bridge.diagnose(self.root)
        self.assertEqual([area for _, area, _, _ in findings], [
            "Configuration", "Local OpenCode", "Network", "SSH connection", "Remote OpenCode",
            "Remote installation", "Relay", "Queues"])
        self.assertNotIn(bridge.FAIL, [status for status, *_ in findings])
        self.assertEqual(self.finding(findings, "Local OpenCode", bridge.OK)[2], "OpenCode 2.0.32")
        self.assertIn("2 files match", self.finding(findings, "Remote installation", bridge.OK)[2])
        self.assertEqual(self.finding(findings, "Relay")[0], bridge.WARN)
        self.assertEqual(self.finding(findings, "Queues", bridge.OK)[2], "linux: empty | wsl: 2 delivered")
        (argv, request), = self.requests
        self.assertEqual(argv, ["python3", "-I", "-c", deploy.inspection_script()])
        self.assertEqual(request, {"root": "/srv/Example Project", "files": list(self.files)})

    def test_remote_inspection_failure_reports_stdout_instead_of_ssh_advisory(self):
        self.mocks["opencode_connect.bridge.bridge.Endpoint.remote_exec"].side_effect = lambda *args, **kwargs: transports.run_json(
            ["example"])
        with patch("opencode_connect.transports.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout=b"Remote inspection failed: private directory is insecure.",
                stderr=b"** WARNING: post-quantum key exchange unavailable.\n")) as run:
            summary = self.finding(bridge.diagnose(self.root), "Remote installation", bridge.FAIL)[2]
        self.assertIn("private directory is insecure", summary)
        self.assertNotIn("post-quantum", summary)
        run.assert_called_once()

    def test_unreachable_machine_skips_remote_layers_but_reports_delivery(self):
        self.probe = transports.Probe(False, "unreachable", "No route to host")
        findings = bridge.diagnose(self.root)
        status, _, summary, hint = self.finding(findings, "Network")
        self.assertEqual((status, summary), (bridge.FAIL, "192.0.2.10:22: No route to host"))
        self.assertIn("asleep", hint)
        for area in ("SSH connection", "Remote OpenCode", "Remote installation"):
            self.assertEqual(self.finding(findings, area)[0], bridge.SKIP)
        self.assertIn("wsl: not checked", self.finding(findings, "Queues")[2])
        self.assertEqual(self.requests, [])
        self.mocks["opencode_connect.transports.WindowsWslSshTransport.master_alive"].assert_not_called()

    def test_closed_connection_points_to_connect_and_hides_ssh_advisories(self):
        self.master = False
        self.remote_api = config.BridgeError(
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            "** This session may be vulnerable to \"store now, decrypt later\" attacks.\n"
            "Example User@192.0.2.10: Permission denied (publickey,password).")
        findings = bridge.diagnose(self.root)
        status, _, summary, hint = self.finding(findings, "SSH connection")
        self.assertEqual((status, hint), (bridge.FAIL, bridge.CONNECT_HINT))
        self.assertNotIn("post-quantum", summary)
        self.assertEqual(self.finding(findings, "Remote OpenCode")[0], bridge.SKIP)
        self.assertEqual(self.requests, [])

    def test_key_authentication_without_a_reusable_connection_is_fine(self):
        self.master = False
        summary = self.finding(bridge.diagnose(self.root), "SSH connection", bridge.OK)[2]
        self.assertIn("non-interactive authentication works", summary)

    def test_host_key_problems_never_suggest_disabling_verification(self):
        self.master = False
        self.remote_api = config.BridgeError(
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            "Host key verification failed.")
        _, _, summary, hint = self.finding(bridge.diagnose(self.root), "SSH connection", bridge.FAIL)
        self.assertEqual(summary, "Host key verification failed.")
        self.assertIn("never disable host key checking", hint)

    def test_ssh_advisories_are_dropped_from_problems(self):
        error = ("** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
                 "** The server may need to be upgraded. See https://openssh.com/pq.html\n"
                 "Example User@192.0.2.10: Permission denied (publickey).\n")
        self.assertEqual(bridge.ssh_problem(error), "Example User@192.0.2.10: Permission denied (publickey).")
        self.assertEqual(bridge.ssh_problem("** WARNING only\n"), "unknown error")

    def test_remote_command_failures_still_inspect_the_installation(self):
        cases = (("/home/example/wsl-tools/opencode: No such file or directory", "command -v opencode"),
                 ("Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND", "wsl.exe -u example -- true"))
        for problem, advice in cases:
            self.remote_api = config.BridgeError(problem)
            self.requests.clear()
            findings = bridge.diagnose(self.root)
            with self.subTest(problem=problem):
                self.assertIn(advice, self.finding(findings, "Remote OpenCode", bridge.FAIL)[3])
                self.assertEqual(self.finding(findings, "Remote installation")[0], bridge.OK)
                self.assertEqual(len(self.requests), 1)

    def test_outdated_and_remotely_edited_files_are_distinguished(self):
        files, manifest = self.inspection["files"], self.inspection["manifest"]
        files["example.py"] = manifest["example.py"] = "0" * 64
        files["docs/example.md"] = "1" * 64
        findings = bridge.diagnose(self.root)
        self.assertEqual(self.area_summaries(findings, "Remote installation", bridge.WARN), [
            "edited in WSL since the last deployment: docs/example.md",
            "not up to date with this checkout: example.py"])
        self.assertNotIn(bridge.FAIL, [status for status, *_ in findings])
        files["docs/example.md"] = manifest["docs/example.md"]
        files["example.py"] = None
        self.assertEqual(self.area_summaries(bridge.diagnose(self.root), "Remote installation", bridge.WARN),
                         ["not up to date with this checkout: example.py"])

    def test_old_python_insecure_state_and_wrong_side_fail(self):
        self.inspection.update(python=[3, 11, 9], state_private=False, config={"side": "linux", "peers": {}})
        self.assertEqual(self.area_summaries(bridge.diagnose(self.root), "Remote installation", bridge.FAIL), [
            "WSL has Python 3.11.9; 3.12+ is required",
            ".state in WSL is not private (0700, owned by the WSL user)",
            "routes.json in WSL is invalid or does not declare side=wsl"])

    def test_missing_installation_configuration_or_matching_shortcuts(self):
        self.inspection["installed"] = False
        summary = self.finding(bridge.diagnose(self.root), "Remote installation", bridge.FAIL)[2]
        self.assertEqual(summary, "/srv/Example Project does not exist in WSL")
        self.inspection.update(installed=True, config=None)
        summary = self.finding(bridge.diagnose(self.root), "Remote installation", bridge.FAIL)[2]
        self.assertEqual(summary, "routes.json is missing in WSL")
        self.inspection["config"] = {"side": "wsl", "peers": {}}
        summary = self.finding(bridge.diagnose(self.root), "Remote installation", bridge.WARN)[2]
        self.assertEqual(summary, "shortcuts differ between the two machines")

    def test_waiting_and_ambiguous_messages_are_flagged(self):
        box = collab.Mailbox(self.root)
        self.addCleanup(box.db.close)
        box.enqueue("ses_local", "wsl", "hello")
        self.inspection["queue"] = {"pending": 2, "uncertain": 1, "delivered": 4}
        findings = bridge.diagnose(self.root)
        self.assertEqual(self.finding(findings, "Relay", bridge.WARN)[2], "not running; 3 message(s) waiting")
        status, _, summary, hint = self.finding(findings, "Queues")
        self.assertEqual((status, summary),
                         (bridge.WARN, "linux: 1 pending | wsl: 2 pending, 1 uncertain, 4 delivered"))
        self.assertIn("never resend", hint)

    def test_failed_messages_are_reported_as_never_sent(self):
        self.inspection["queue"] = {"failed": 1, "delivered": 3}
        status, _, summary, hint = self.finding(bridge.diagnose(self.root), "Queues")
        self.assertEqual((status, summary), (bridge.WARN, "linux: empty | wsl: 1 failed, 3 delivered"))
        self.assertIn("never sent", hint)

    def test_running_relay_and_remote_without_mailbox(self):
        self.inspection["queue"] = None
        with patch("opencode_connect.bridge.collab.relay_status", return_value={"running": True, "pid": 4321, "stale": False}):
            findings = bridge.diagnose(self.root)
        self.assertEqual(self.finding(findings, "Relay", bridge.OK)[2], "running (pid 4321)")
        self.assertEqual(self.finding(findings, "Queues", bridge.OK)[2], "linux: empty | wsl: no mailbox yet")

    def test_invalid_configuration_stops_before_any_probe(self):
        (self.root / "routes.json").write_text("{}")
        findings = bridge.diagnose(self.root)
        self.assertEqual([(status, area) for status, area, *_ in findings], [(bridge.FAIL, "Configuration")])
        self.mocks["opencode_connect.bridge.bridge.probe_api"].assert_not_called()
        self.mocks["opencode_connect.bridge.bridge.tcp_probe"].assert_not_called()

    def test_doctor_sends_nothing_and_creates_no_state(self):
        with patch("opencode_connect.bridge.collab.Mailbox", side_effect=AssertionError("no writable mailbox")), \
                patch("opencode_connect.bridge.collab.relay", side_effect=AssertionError("no relay")), \
                patch("opencode_connect.bridge.collab.remote_rpc", side_effect=AssertionError("no mailbox RPC")):
            findings = bridge.diagnose(self.root)
        self.assertNotIn(bridge.FAIL, [status for status, *_ in findings])
        self.assertFalse((self.root / ".state").exists())

    def test_cli_prints_hints_and_fails_only_on_failures(self):
        cases = (([(bridge.OK, "Configuration", "valid", None),
                   (bridge.WARN, "Relay", "not running", "Start it.")], 0),
                 ([(bridge.FAIL, "Network", "192.0.2.10:22: timed out", "Wake it.")], 1))
        for findings, code in cases:
            output = io.StringIO()
            with self.subTest(code=code), patch("opencode_connect.bridge.bridge.diagnose", return_value=findings), \
                    patch("sys.argv", ["bridge.py", "doctor"]), contextlib.redirect_stdout(output):
                self.assertEqual(bridge.main(), code)
            self.assertIn("-> ", output.getvalue())
            self.assertIn("/mcps", output.getvalue())


class LocalDoctorTests(unittest.TestCase):
    """`doctor --local`: this machine's local queue and Claude Code hooks, read-only."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "local",
                                              "OPENCODE_CONNECT_LOCAL_STATE_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

    def mailbox(self, node=None):
        from opencode_connect.local import mailbox as local_bridge
        box = local_bridge.LocalMailbox(local_bridge.state_root(), node=node)
        self.addCleanup(box.db.close)
        return box

    def summaries(self, findings):
        return [(status, area, summary) for status, area, summary, _ in findings]

    def test_nothing_set_up_is_skipped_and_nothing_is_created(self):
        findings = bridge.diagnose_local()
        self.assertEqual([(status, area) for status, area, *_ in findings],
                         [(bridge.SKIP, "Local queue"), (bridge.SKIP, "Claude Code")])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_counts_messages_per_consumer_without_changing_the_queue(self):
        box = self.mailbox()
        box.enqueue("ses_SOURCE", "ses_TARGET", "For OpenCode")
        box.enqueue("ses_SOURCE", CLAUDE_ADDRESS, "For Claude")
        box.enqueue("ses_SOURCE", "person:bob", "For Bob")
        box.insert_inbound("person:alice", "From Alice", remote_key="key-1")
        box.claim()
        database = box.state / "mail.sqlite3"
        before = database.read_bytes()
        with patch("opencode_connect.local.mailbox.LocalMailbox", side_effect=AssertionError("no writable mailbox")):
            findings = bridge.diagnose_local()
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(self.summaries(findings), [
            (bridge.OK, "Local queue", "node local; 2 pending, 1 sending, 1 held"),
            (bridge.WARN, "Local messages", "1 sending or uncertain"),
            (bridge.OK, "Local messages", "1 waiting for Claude Code sessions"),
            (bridge.OK, "Local messages", "1 waiting for other people"),
            (bridge.WARN, "Local messages", "1 from other people held for approval"),
            (bridge.SKIP, "Claude Code", "no session has used the hooks yet")])

    def test_a_queue_bound_to_another_node_fails_and_version_one_needs_migration(self):
        self.mailbox(node="other").claim()
        status, area, summary, hint = bridge.diagnose_local()[0]
        self.assertEqual((status, area), (bridge.FAIL, "Local queue"))
        self.assertIn("bound to node other", summary)
        self.assertIn("OPENCODE_CONNECT_LOCAL_NODE", hint)
        box = self.mailbox(node="other")
        with box.transaction():
            box.db.execute("UPDATE local_contract SET schema_version=1, canonical_node='local'")
        status, area, summary, hint = bridge.diagnose_local()[0]
        self.assertEqual((status, area), (bridge.WARN, "Local queue"))
        self.assertIn("local_bridge.py migrate", hint)
        box.db.execute("INSERT INTO messages VALUES ('msg_old', 0, 'local:ses_A', 'local:ses_B', "
                       "'queue', 'message', '{}', 'delivered', NULL)")
        box.db.execute("DROP TABLE local_contract")
        box.db.commit()
        self.assertEqual(self.summaries(bridge.diagnose_local())[0][:2], (bridge.FAIL, "Local queue"))
        self.assertIn("unbound but already holding messages", bridge.diagnose_local()[0][2])

    def test_stale_identity_stamps_point_at_a_server_that_never_reads_them(self):
        from opencode_connect.claude import hook as claude_hook
        claude_hook.record("toolu_FRESH", CLAUDE_ADDRESS)
        self.assertEqual(self.summaries(bridge.diagnose_local())[-1],
                         (bridge.OK, "Claude Code", "hooks have run; no stale identity stamps"))
        stamp = claude_hook.spool() / "toolu_FRESH.stamp"
        past = stamp.lstat().st_mtime - claude_hook.STAMP_TTL - 1
        os.utime(stamp, (past, past))
        status, area, summary, hint = bridge.diagnose_local()[-1]
        self.assertEqual((status, area, summary), (bridge.WARN, "Claude Code", "1 identity stamp(s) never consumed"))
        self.assertIn("same state home", hint)

    def test_cli_local_flag_runs_only_the_local_checks(self):
        cases = (([(bridge.OK, "Local queue", "node local; empty", None)], 0),
                 ([(bridge.FAIL, "Local queue", "bound to node other", "Use the same node.")], 1))
        for findings, code in cases:
            output = io.StringIO()
            with self.subTest(code=code), \
                    patch("opencode_connect.bridge.bridge.diagnose_local", return_value=findings), \
                    patch("opencode_connect.bridge.bridge.diagnose", side_effect=AssertionError("cross-machine checks")), \
                    patch("sys.argv", ["bridge.py", "doctor", "--local"]), contextlib.redirect_stdout(output):
                self.assertEqual(bridge.main(), code)
            self.assertIn("/mcp", output.getvalue())
        with patch("sys.argv", ["bridge.py", "check", "--local"]), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            bridge.main()


class InspectionTests(unittest.TestCase):
    """The probe that doctor runs in WSL, executed standalone like the deployment receiver."""

    def inspect(self, root, files):
        result = subprocess.run([sys.executable, "-I", "-c", deploy.inspection_script()], timeout=30,
                                input=json.dumps({"root": str(root), "files": files}),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_inspection_reports_the_tree_without_changing_it(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "remote"
            root.mkdir()
            cfg = {**fixture(), "side": "wsl"}
            (root / "routes.json").write_text(json.dumps(cfg))
            (root / "example.py").write_text("example")
            box = collab.Mailbox(root)
            box.enqueue("ses_remote", "linux", "hello")
            box.db.close()
            (root / ".state" / "deployment.json").write_text(json.dumps({"example.py": "recorded"}))
            snapshot = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = self.inspect(root, ["example.py", "missing.py"])
            self.assertEqual({path: path.read_bytes() for path in root.rglob("*") if path.is_file()}, snapshot)
        self.assertEqual(report["python"][:2], list(sys.version_info[:2]))
        self.assertEqual(report["files"], {"example.py": hashlib.sha256(b"example").hexdigest(),
                                           "missing.py": None})
        self.assertEqual(report["manifest"], {"example.py": "recorded"})
        self.assertTrue(report["state_private"])
        self.assertEqual(report["queue"], {"pending": 1})
        self.assertEqual(report["config"], {"side": "wsl", "peers": cfg["peers"]})

    def test_inspection_of_insecure_or_missing_trees(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "remote"
            (root / ".state").mkdir(parents=True)
            (root / ".state").chmod(0o755)
            report = self.inspect(root, [])
            self.assertEqual((report["state_private"], report["queue"], report["config"]), (False, {}, None))
            missing = self.inspect(Path(folder) / "absent", ["example.py"])
            self.assertFalse(missing["installed"])
            self.assertFalse((Path(folder) / "absent").exists())


if __name__ == "__main__":
    unittest.main()
