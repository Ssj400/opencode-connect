import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from core import BridgeError
from opencode_adapter import OpenCodeAdapter, OpenCodeCli
from transports import LocalCommandTransport


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

    @patch("transports.subprocess.run")
    def test_local_transport_preserves_input_cwd_and_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")
        result = LocalCommandTransport().run(["example", "--json"], data="ñ", cwd="/example", timeout=5)
        self.assertEqual(result, {"ok": True})
        run.assert_called_once_with(["example", "--json"], input="ñ".encode(),
                                    capture_output=True, timeout=5, cwd="/example")


if __name__ == "__main__":
    unittest.main()
