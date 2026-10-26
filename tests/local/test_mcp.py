"""Offline tests of the local MCP server for OpenCode sessions."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import uuid

from opencode_connect.core import BridgeError
from opencode_connect.local.mailbox import LocalMailbox, main, session, state_root
from opencode_connect.local.mcp_server import call_tool, handle


class LocalMcpTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"OPENCODE_CONNECT_LOCAL_NODE": "local"})
        environment.start()
        self.addCleanup(environment.stop)

    def params(self, name, **args):
        return {"name": name, "arguments": args, "_meta": {"ai.opencode/sessionID": "ses_SOURCE"}}

    def test_injected_identity_gives_another_harness_the_same_tools(self):
        session = str(uuid.UUID(int=1))
        claude = lambda params, node: "claude:" + session.upper()
        self.assertEqual(call_tool({"name": "whoami", "arguments": {}}, identity=claude),
                         {"address": "claude:" + session})
        with tempfile.TemporaryDirectory() as root:
            sent = call_tool({"name": "send", "arguments": {"to": "ses_TARGET", "text": "From Claude"}},
                             root=root, identity=claude)
            box = LocalMailbox(Path(root))
            try:
                job = box.claim()
            finally:
                box.db.close()
        self.assertEqual((job["id"], job["source"]), (sent["id"], "claude:" + session))
        adapter = Mock()
        call_tool({"name": "find_sessions", "arguments": {"query": "API"}}, adapter=adapter, identity=claude)
        adapter.find_sessions.assert_called_once_with("API", cursor=None, exclude=None)

    def test_identity_providers_cannot_claim_other_people_or_nodes(self):
        for address in ("person:alice", "wsl:ses_REMOTE", "not an address"):
            with self.subTest(address=address), self.assertRaises(BridgeError):
                call_tool({"name": "whoami", "arguments": {}}, identity=lambda params, node, value=address: value)

    def test_identity_uses_metadata_without_configuration(self):
        self.assertEqual(call_tool(self.params("whoami")), {"address": "local:ses_SOURCE"})
        with self.assertRaises(BridgeError):
            call_tool({"name": "whoami"})

    def test_search_excludes_caller(self):
        adapter = Mock()
        call_tool(self.params("find_sessions", query="API"), adapter=adapter)
        adapter.find_sessions.assert_called_once_with("API", cursor=None, exclude="ses_SOURCE")

    def test_send_queues_without_api_calls(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = Mock()
            result = call_tool(self.params("send", to="ses_TARGET", text="Question"),
                               root=root, adapter=adapter)
            self.assertEqual(result["state"], "pending")
            adapter.assert_not_called()
            self.assertEqual(len(call_tool(self.params("status"), root=root)), 1)

    def test_send_passes_reply_to_and_the_queue_checks_it(self):
        with tempfile.TemporaryDirectory() as root:
            box = LocalMailbox(Path(root))
            self.addCleanup(box.db.close)
            held = box.insert_inbound("person:alice", "Question", remote_key="key-1")
            box.approve_inbound(held["id"], "ses_SOURCE")
            box.mark(box.claim()["id"], "delivered", {"inbox_id": "inbox_EXAMPLE"})
            call_tool(self.params("send", to="person:alice", text="Answer", reply_to=held["id"]), root=root)
            self.assertEqual(box.claim("person:")["payload"]["reply_to"], held["id"])
            with self.assertRaisesRegex(BridgeError, "delivered to this session"):
                call_tool(self.params("send", to="person:bob", text="Answer", reply_to=held["id"]), root=root)

    def test_a_database_error_is_a_tool_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(LocalMailbox, "enqueue", side_effect=sqlite3.OperationalError("database is locked")):
            reply = handle({"id": 1, "method": "tools/call", "params": self.params(
                "send", to="ses_TARGET", text="Question")}, root=root)
        self.assertEqual(reply["result"], {"isError": True, "content": [{"type": "text", "text": "database is locked"}]})

    def test_extra_capabilities_rejected(self):
        for name, args in (("send_file", {}), ("send", {"urgent": True}), ("whoami", {"source": "ses_OTHER"})):
            with self.assertRaises(BridgeError):
                call_tool(self.params(name, **args))

    def test_protocol_has_only_local_tools(self):
        tools = handle({"id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"whoami", "find_sessions", "send", "status", "diagnose", "people"})
        reply = handle({"id": 2, "method": "tools/call", "params": {"name": "send"}})
        self.assertTrue(reply["result"]["isError"])
        self.assertIsNone(handle({"method": "notifications/initialized"}))
        self.assertEqual(handle({"id": 3, "method": "initialize", "params": {}})
                         ["result"]["serverInfo"]["name"], "opencode-connect-local")

    def test_state_path_needs_no_ssh_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict("os.environ", {"XDG_STATE_HOME": root}, clear=True):
                path = state_root()
                self.assertEqual(path, Path(root) / "opencode-connect/local")
            with patch.dict("os.environ", {"XDG_STATE_HOME": "relative"}, clear=True):
                with self.assertRaises(BridgeError):
                    state_root()
        self.assertEqual(session("local:ses_EXAMPLE"), "ses_EXAMPLE")

    def test_cli_sets_private_umask_before_creating_state(self):
        with patch("opencode_connect.local.mailbox.os.umask") as mask, \
                patch("sys.argv", ["local_bridge.py", "status"]), \
                patch("opencode_connect.local.mailbox.state_root") as root, \
                patch("opencode_connect.local.mailbox.LocalMailbox") as mailbox, \
                patch("opencode_connect.local.mailbox.local_adapter"), patch("builtins.print"):
            root.side_effect = lambda: mask.assert_called_once_with(0o077)
            mailbox.return_value.status.return_value = []
            self.assertEqual(main(), 0)
            mask.assert_called_once_with(0o077)
