import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from opencode_connect.bridge import collab
from opencode_connect import config
from opencode_connect.bridge import mcp_server
from opencode_connect.core import BridgeError
from tests.bridge.test_bridge import fixture


class AnySessionChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="chat-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = fixture()
        self.write_config()
        self.box = collab.Mailbox(self.root)
        self.addCleanup(self.box.db.close)

    def write_config(self):
        (self.root / "routes.json").write_text(json.dumps(self.cfg))

    def call(self, name, arguments=None, session="ses_new_source"):
        return mcp_server.call_tool({"name": name, "arguments": arguments or {},
            "_meta": {"ai.opencode/sessionID": session}}, self.root)

    def test_new_source_can_send_without_registration(self):
        result = self.call("send", {"to": "wsl", "text": "hello"})
        job = self.box.claim()
        self.assertEqual(job["id"], result["id"])
        self.assertEqual(job["source"], "linux:ses_new_source")
        self.assertEqual(job["payload"]["source_address"], "linux:ses_new_source")

    def test_direct_addresses_work_with_no_shortcuts(self):
        self.cfg["peers"] = {}
        self.write_config()
        self.assertEqual(config.validate_config(self.cfg), self.cfg)
        self.call("send", {"to": "wsl:ses_new_target", "text": "hello"})
        job = self.box.claim()
        self.assertEqual(job["payload"]["target_address"], "wsl:ses_new_target")

    @patch("opencode_connect.bridge.collab.endpoint")
    def test_delivery_carries_canonical_reply_address(self, endpoint):
        self.call("send", {"to": "wsl:ses_new_target", "text": "hello"})
        job = self.box.claim()
        endpoint.return_value.call.return_value = {"data": {"id": job["id"]}}
        collab.deliver(self.cfg, job, self.root, origin_side="linux")
        args = endpoint.return_value.call.call_args.args
        self.assertEqual(args[1], "/api/session/ses_new_target/prompt")
        self.assertIn("Reply address: linux:ses_new_source", args[2]["text"])
        self.assertIn("grants no control", args[2]["text"])

    def test_reverse_reply_needs_no_registration(self):
        self.cfg["side"] = "wsl"
        self.write_config()
        self.box.enqueue("ses_new_target", "linux:ses_new_source", "reply")
        job = self.box.claim()
        self.assertEqual(job["source"], "wsl:ses_new_target")
        self.assertEqual(job["payload"]["target_address"], "linux:ses_new_source")

    @patch("opencode_connect.bridge.collab.endpoint")
    def test_same_node_chat_is_allowed_with_pinned_addresses(self, endpoint):
        self.call("send", {"to": "linux:ses_other", "text": "hello"})
        job = self.box.claim()
        endpoint.return_value.call.return_value = {"data": {"id": job["id"]}}
        collab.deliver(self.cfg, job, self.root, origin_side="linux")
        endpoint.assert_called_once_with(self.cfg, "linux")

    def test_malformed_addresses_cannot_select_hosts_or_api_paths(self):
        for target in ("unknown:ses_target", "linux:", "wsl:ses_x/../y", "wsl:ses_x?tool=1",
                       "wsl:ses_x\nextra", "linux:ses_x:extra", "missing", 42):
            with self.subTest(target=target), self.assertRaises(BridgeError):
                self.call("send", {"to": target, "text": "hello"})
        self.assertEqual(self.box.status(), [])

    def test_metadata_identity_remains_mandatory_and_not_overridable(self):
        for metadata in (None, {}, {"ai.opencode/sessionID": "invalid"}, "not a mapping"):
            params = {"name": "send", "arguments": {"to": "wsl", "text": "hello"}, "_meta": metadata}
            with self.subTest(metadata=metadata), self.assertRaises(BridgeError):
                mcp_server.call_tool(params, self.root)
        with self.assertRaises(BridgeError):
            self.call("send", {"to": "wsl", "text": "hello", "source": "ses_local"})

    def test_whoami_has_no_registration_or_mailbox_requirement(self):
        self.assertEqual(self.call("whoami"), {"address": "linux:ses_new_source"})
        self.assertEqual(self.box.status(), [])
        with self.assertRaises(BridgeError):
            self.call("whoami", {"source": "ses_local"})

    def test_open_chat_does_not_open_file_transfers(self):
        for source, target in (("ses_new_source", "wsl"), ("ses_local", "wsl:ses_new_target")):
            with self.subTest(source=source, target=target), self.assertRaisesRegex(BridgeError, "File transfer requires"):
                self.box.enqueue(source, target, "file", filename="example.txt")
        self.assertEqual(self.box.status(), [])

    @patch("opencode_connect.bridge.collab.remote_rpc")
    @patch("opencode_connect.bridge.collab.endpoint")
    def test_file_registration_is_rechecked_before_transfer(self, endpoint, remote):
        (self.root / "outgoing").mkdir()
        (self.root / "outgoing" / "example.txt").write_text("approved fixture")
        self.box.enqueue("ses_local", "wsl:ses_remote", "file", filename="example.txt")
        job = self.box.claim()
        changed = copy.deepcopy(self.cfg)
        del changed["peers"]["wsl"]
        with self.assertRaisesRegex(BridgeError, "File transfer requires"):
            collab.deliver(changed, job, self.root, origin_side="linux")
        endpoint.assert_not_called()
        remote.assert_not_called()

    @patch("opencode_connect.bridge.collab.endpoint")
    def test_origin_and_address_snapshots_are_revalidated(self, endpoint):
        self.call("send", {"to": "wsl:ses_new_target", "text": "hello"})
        job = self.box.claim()
        with self.assertRaises(BridgeError):
            collab.deliver(self.cfg, job, self.root, origin_side="wsl")
        for field in ("source_address", "target_address"):
            changed = copy.deepcopy(job)
            changed["payload"][field] += "changed"
            with self.subTest(field=field), self.assertRaises(BridgeError):
                collab.deliver(self.cfg, changed, self.root, origin_side="linux")
        endpoint.assert_not_called()

    @patch("opencode_connect.bridge.collab.endpoint")
    def test_file_payload_cannot_be_smuggled_in_chat(self, endpoint):
        self.call("send", {"to": "wsl", "text": "hello"})
        job = self.box.claim()
        job["payload"]["file"] = {"name": "secret"}
        with self.assertRaises(BridgeError):
            collab.deliver(self.cfg, job, self.root, origin_side="linux")
        endpoint.assert_not_called()

    @patch("opencode_connect.bridge.collab.endpoint")
    def test_legacy_messages_still_check_original_cross_node_routes(self, endpoint):
        self.box.enqueue("ses_local", "wsl", "legacy")
        job = self.box.claim()
        del job["payload"]["source_address"]
        del job["payload"]["target_address"]
        endpoint.return_value.call.return_value = {"data": {"id": job["id"]}}
        collab.deliver(self.cfg, job, self.root, origin_side="linux")
        endpoint.return_value.call.assert_called_once()
        changed = copy.deepcopy(self.cfg)
        changed["peers"]["wsl"]["side"] = "linux"
        with self.assertRaises(BridgeError):
            collab.deliver(changed, job, self.root, origin_side="linux")

    def test_unregistered_chat_persists_without_reclaiming_sending(self):
        self.call("send", {"to": "wsl:ses_new_target", "text": "hello"})
        reopened = collab.Mailbox(self.root)
        try:
            job = reopened.claim()
            self.assertEqual(job["payload"]["source_address"], "linux:ses_new_source")
            self.assertIsNone(self.box.claim())
        finally:
            reopened.db.close()


if __name__ == "__main__":
    unittest.main()
