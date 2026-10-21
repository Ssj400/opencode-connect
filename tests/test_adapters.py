import errno
import json
import socket
import subprocess
import threading
import unittest
from unittest.mock import Mock, call, patch

from opencode_connect.core import BridgeError
from opencode_connect.opencode_adapter import OpenCodeAdapter, OpenCodeCli, probe_api
from opencode_connect.transports import LocalCommandTransport, WindowsWslSshTransport, tcp_probe

SSH = {"host": "192.0.2.10", "ssh_user": "Example User", "wsl_user": "example",
       "remote_opencode": "/opt/opencode/bin/opencode", "remote_directory": "/srv/opencode-connect"}


class AdapterTests(unittest.TestCase):
    def test_cli_encodes_opencode_api_arguments_without_shell(self):
        transport = Mock()
        transport.run.return_value = {"data": {"id": "accepted"}}
        cli = OpenCodeCli(transport, "/opt/Example App/opencode")
        result = cli.call("post", "/api/example", {"text": "Unicode ñ 🚀"}, timeout=9)
        argv = transport.run.call_args.args[0]
        self.assertEqual(argv[:4], ["/opt/Example App/opencode", "api", "post", "/api/example"])
        self.assertEqual(json.loads(argv[-1]), {"text": "Unicode ñ 🚀"})
        self.assertEqual(transport.run.call_args.kwargs, {"timeout": 9})
        self.assertEqual(result, {"data": {"id": "accepted"}})

    def test_api_error_schema_is_interpreted_by_harness_adapter(self):
        transport = Mock()
        transport.run.return_value = {"_tag": "ExampleError", "message": "Unavailable"}
        with self.assertRaisesRegex(BridgeError, "API error: Unavailable"):
            OpenCodeCli(transport, "opencode").call("get", "/api/info")
        transport.run.assert_called_once()

    def test_send_maps_modes_ids_and_acceptance_without_waiting(self):
        for delivery in ("queue", "steer"):
            api = Mock()
            api.call.return_value = {"data": {"id": "accepted"}}
            result = OpenCodeAdapter(api).send_message("ses_example", message_id="example-id",
                                                      text="hello", delivery=delivery)
            self.assertEqual(result, "accepted")
            api.call.assert_called_once_with("post", "/api/session/ses_example/prompt", {
                "id": "example-id", "text": "hello", "delivery": delivery})

    def test_unsupported_delivery_has_no_api_effects(self):
        api = Mock()
        with self.assertRaises(BridgeError):
            OpenCodeAdapter(api).send_message("ses_example", message_id="example-id",
                                              text="hello", delivery="interrupt")
        api.call.assert_not_called()

    def test_create_session_preserves_ask_permissions_without_generation(self):
        api = Mock()
        api.call.return_value = {"data": {"id": "ses_example"}}
        result = OpenCodeAdapter(api).create_session(title="Example", directory="/srv/example")
        self.assertEqual(result, {"id": "ses_example"})
        api.call.assert_called_once_with("post", "/api/session", {
            "title": "Example", "location": {"directory": "/srv/example"},
            "permissions": [{"action": "*", "resource": "*", "effect": "ask"}]})

    @patch("opencode_connect.transports.subprocess.run")
    def test_local_transport_preserves_input_cwd_and_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")
        result = LocalCommandTransport().run(["example", "--json"], data="ñ", cwd="/example", timeout=5)
        self.assertEqual(result, {"ok": True})
        run.assert_called_once_with(["example", "--json"], input="ñ".encode(),
                                    capture_output=True, timeout=5, cwd="/example")

    def test_probe_api_makes_only_the_two_small_reads(self):
        api = Mock()
        api.call.side_effect = [{"version": "2.0.32"}, {"data": []}]
        self.assertEqual(probe_api(api), "2.0.32")
        self.assertEqual(api.call.call_args_list,
                         [call("get", "/api/info"), call("get", "/api/session?limit=1")])

    def test_probe_api_rejects_other_versions_and_shapes(self):
        for responses in ([{"version": "1.4.0"}], [{"other": 1}], [None],
                          [{"version": "2.0.0"}, {"data": "not a list"}]):
            api = Mock()
            api.call.side_effect = responses
            with self.subTest(responses=responses), self.assertRaises(BridgeError):
                probe_api(api)


class TransportProbeTests(unittest.TestCase):
    def test_tcp_probe_reads_the_banner_and_sends_nothing(self):
        server = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(server.close)
        received = []

        def serve():
            connection, _ = server.accept()
            with connection:
                connection.sendall(b"SSH-2.0-Example\r\n")
                connection.settimeout(5)
                received.append(connection.recv(64))

        thread = threading.Thread(target=serve)
        thread.start()
        result = tcp_probe("127.0.0.1", server.getsockname()[1], timeout=5)
        thread.join(5)
        self.assertEqual(result, (True, "open", "SSH-2.0-Example"))
        self.assertEqual(received, [b""])  # The client closed without sending a byte.

    def test_tcp_probe_reports_refused_ports(self):
        with socket.create_server(("127.0.0.1", 0)) as spare:
            port = spare.getsockname()[1]
        self.assertEqual(tcp_probe("127.0.0.1", port, timeout=5).reason, "refused")

    def test_tcp_probe_maps_network_failures_to_stable_reasons(self):
        cases = [(OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
                 (OSError(errno.ENETUNREACH, "Network is unreachable"), "unreachable"),
                 (TimeoutError("timed out"), "timeout"),
                 (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "unresolved"),
                 (OSError(errno.EACCES, "Permission denied"), "error")]
        for error, reason in cases:
            with self.subTest(reason=reason), \
                    patch("opencode_connect.transports.socket.create_connection", side_effect=error):
                result = tcp_probe("192.0.2.10", 22)
                self.assertEqual((result.ok, result.reason), (False, reason))
                self.assertTrue(result.detail)

    @patch("opencode_connect.transports.subprocess.run")
    def test_destination_follows_the_users_ssh_configuration(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, "user Example User\nhostname 192.0.2.20\nport 2222\n", "")
        self.assertEqual(WindowsWslSshTransport(SSH).destination(), ("192.0.2.20", 2222))
        self.assertEqual(run.call_args.args[0], ["ssh", "-G", "-l", "Example User", "192.0.2.10"])
        run.return_value = subprocess.CompletedProcess([], 255, "", "Bad configuration option")
        with self.assertRaisesRegex(BridgeError, "Bad configuration option"):
            WindowsWslSshTransport(SSH).destination()

    @patch("opencode_connect.transports.socket_path", return_value="/example/control.sock")
    @patch("opencode_connect.transports.subprocess.run")
    def test_master_check_only_queries_the_control_socket(self, run, socket_path):
        transport = WindowsWslSshTransport(SSH)
        run.return_value = subprocess.CompletedProcess([], 0, b"", b"Master running")
        self.assertTrue(transport.master_alive())
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["ssh", "-O", "check"])
        self.assertIn("ControlPath=/example/control.sock", command)
        self.assertNotIn("-M", command)
        run.return_value = subprocess.CompletedProcess([], 255, b"", b"No such file or directory")
        self.assertFalse(transport.master_alive())
        for error in (OSError("ssh is missing"), subprocess.TimeoutExpired("ssh", 10)):
            run.side_effect = error
            self.assertFalse(transport.master_alive())


if __name__ == "__main__":
    unittest.main()
