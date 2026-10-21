"""Offline canonical local identity through separate MCP stdio clients and the CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from opencode_connect.core import BridgeError, relay_once
from opencode_connect.local.mailbox import LocalMailbox, deliver_local, local_relay_once
from opencode_connect.local.mcp_server import call_tool
from opencode_connect.opencode_adapter import OpenCodeAdapter
from tests.local.test_identity import tool_params


class LocalIdentityMcpTests(unittest.TestCase):
    def test_separate_stdio_clients_and_cli_share_configured_contract(self):
        with tempfile.TemporaryDirectory(prefix="local-stdio-") as root:
            environment = {**os.environ, "OPENCODE_CONNECT_LOCAL_STATE_HOME": root,
                           "OPENCODE_CONNECT_LOCAL_NODE": "wsl"}
            checkout = Path(__file__).resolve().parents[2] / "bin"

            def invoke(params):
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
                process = subprocess.run(
                    [sys.executable, "-B", str(checkout / "local_mcp_server.py")],
                    input=json.dumps(request) + "\n", capture_output=True, text=True,
                    env=environment, timeout=10, check=True)
                reply = json.loads(process.stdout)["result"]
                self.assertNotIn("isError", reply)
                return json.loads(reply["content"][0]["text"])

            identity = invoke(tool_params("whoami"))
            self.assertEqual(identity["address"], "wsl:ses_ALPHA")
            first = invoke(tool_params("send", to="local:ses_BETA", text="Synthetic question"))
            second = invoke(tool_params("send", source="ses_BETA", to=identity["address"],
                                        text="Synthetic explicit reply"))
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(first["state"], "pending")
            self.assertEqual(second["state"], "pending")
            diagnostics = invoke(tool_params("diagnose"))
            cli = subprocess.run([sys.executable, "-B", str(checkout / "local_bridge.py"), "diagnose"],
                                 capture_output=True, text=True, env=environment, timeout=10, check=True)
            self.assertEqual(json.loads(cli.stdout), diagnostics)
            self.assertTrue(diagnostics["compatible"])
            self.assertEqual(diagnostics["stored"]["canonical_node"], "wsl")

    def test_whoami_uses_canonical_node_without_database_or_api(self):
        adapter = Mock()
        with patch("opencode_connect.local.mcp_server.LocalMailbox", side_effect=AssertionError("No queue access")), \
                patch("opencode_connect.local.mcp_server.local_adapter", side_effect=AssertionError("No API access")), \
                patch("opencode_connect.local.mcp_server.state_root", side_effect=AssertionError("No state access")):
            self.assertEqual(call_tool(tool_params("whoami"), adapter=adapter, node="linux"),
                             {"address": "linux:ses_ALPHA"})
            with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "wsl"}):
                self.assertEqual(call_tool(tool_params("whoami")), {"address": "wsl:ses_ALPHA"})
        self.assertEqual(adapter.mock_calls, [])

    def test_source_override_is_never_allowed(self):
        for name in ("whoami", "send", "diagnose"):
            with self.subTest(name=name), self.assertRaises(BridgeError):
                call_tool(tool_params(name, source_address="linux:ses_OTHER"), node="linux")
            params = tool_params(name)
            params["arguments"]["source"] = "ses_OTHER"
            with self.subTest(name=name, argument="source"), self.assertRaises(BridgeError):
                call_tool(params, node="linux")

    def test_diagnose_reads_mismatched_queue_without_api_or_private_content(self):
        with tempfile.TemporaryDirectory(prefix="local-diagnose-") as root:
            box = LocalMailbox(root, node="linux")
            try:
                box.enqueue("ses_ALPHA", "ses_BETA", "PRIVATE_SYNTHETIC_TEXT")
            finally:
                box.db.close()
            adapter = Mock()
            with patch("opencode_connect.local.mcp_server.local_adapter", side_effect=AssertionError("No API access")):
                result = call_tool(tool_params("diagnose"), root=root, adapter=adapter, node="wsl")
            self.assertFalse(result["compatible"])
            self.assertEqual(result["stored"]["canonical_node"], "linux")
            self.assertEqual(result["configured"]["canonical_node"], "wsl")
            self.assertEqual(adapter.mock_calls, [])
            serialized = json.dumps(result)
            for private in ("ses_ALPHA", "ses_BETA", "PRIVATE_SYNTHETIC_TEXT"):
                self.assertNotIn(private, serialized)

    def test_explicit_two_way_local_delivery_has_canonical_reply_addresses_and_no_ssh(self):
        with tempfile.TemporaryDirectory(prefix="local-roundtrip-") as root:
            box = LocalMailbox(root, node="linux")
            self.addCleanup(box.db.close)
            api = Mock()

            def response(method, path, *args):
                if path == "/api/info":
                    return {"version": "2.0.0"}
                if method == "get":
                    return {"data": {"id": path.rsplit("/", 1)[1]}}
                return {"data": {"id": "inbox_SYNTHETIC"}}

            api.call.side_effect = response
            adapter = OpenCodeAdapter(api)
            with patch("opencode_connect.transports.WindowsWslSshTransport.run",
                       side_effect=AssertionError("Local messaging must not use SSH")) as ssh:
                first = call_tool(tool_params("send", to="local:ses_BETA", text="Authorized question"),
                                  root=root, adapter=adapter, node="linux")
                api.call.assert_not_called()
                self.assertEqual(local_relay_once(box, adapter)["state"], "delivered")
                first_body = api.call.call_args.args[2]
                self.assertEqual(first_body["id"], first["id"])
                self.assertIn("Reply address: linux:ses_ALPHA", first_body["text"])
                self.assertEqual(api.call.call_args.args[1], "/api/session/ses_BETA/prompt")
                self.assertIsNone(box.claim())
                api.call.reset_mock()
                reply = call_tool(tool_params("send", source="ses_BETA", to="linux:ses_ALPHA",
                                              text="Explicit authorized reply"),
                                  root=root, adapter=adapter, node="linux")
                api.call.assert_not_called()
                outcome = relay_once(box, lambda job: deliver_local(job, adapter, node=box.node))
                self.assertEqual(outcome["state"], "delivered")
                reply_body = api.call.call_args.args[2]
                self.assertEqual(reply_body["id"], reply["id"])
                self.assertIn("Reply address: linux:ses_BETA", reply_body["text"])
                self.assertEqual(api.call.call_args.args[1], "/api/session/ses_ALPHA/prompt")
                self.assertIsNone(box.claim())
                ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
