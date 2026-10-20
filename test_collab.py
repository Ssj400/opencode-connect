import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bridge import BridgeError
import collab
import mcp_server


class CollabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="connect-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {"side": "linux", "peers": {
            "linux": {"side": "linux", "sessionID": "ses_local"},
            "wsl": {"side": "wsl", "sessionID": "ses_remote"}}, "ssh": {"host": "192.0.2.10", "ssh_user": "Example User",
                    "wsl_user": "example", "remote_opencode": "/opt/opencode/bin/opencode",
                    "remote_directory": "/srv/opencode-connect"}}
        (self.root / "routes.json").write_text(json.dumps(self.cfg))
        self.box = collab.Mailbox(self.root)
        self.addCleanup(self.box.db.close)

    def send(self, text="hola", **kwargs):
        return self.box.enqueue("ses_local", "wsl", text, **kwargs)

    def test_queue_and_steer_priority(self):
        normal = self.send()
        urgent = self.send(urgent=True)
        self.assertEqual(normal["delivery"], "queue")
        self.assertEqual(self.box.claim()["id"], urgent["id"])
        self.assertEqual(self.box.claim()["id"], normal["id"])
        self.assertIsNone(self.box.claim())

    def test_no_reclaim_or_automatic_retry(self):
        msg = self.send()
        self.box.claim()
        self.assertIsNone(self.box.claim())
        self.box.mark(msg["id"], "uncertain")
        self.assertIsNone(self.box.claim())
        with self.assertRaises(BridgeError):
            self.box.mark(msg["id"], "pending")

    def test_pending_survives_reopening_database(self):
        msg = self.send()
        reopened = collab.Mailbox(self.root)
        try:
            self.assertEqual(reopened.claim()["id"], msg["id"])
            # El cambio de estado es persistente y visible a otro cliente.
            self.assertIsNone(self.box.claim())
        finally:
            reopened.db.close()

    def test_sending_survives_restart_without_duplicate(self):
        msg = self.send()
        self.box.claim()
        reopened = collab.Mailbox(self.root)
        try:
            self.assertIsNone(reopened.claim())
            self.assertEqual(reopened.status()[0]["state"], "sending")
            reopened.mark(msg["id"], "uncertain", "Revisión manual tras caída")
            self.assertIsNone(self.box.claim())
        finally:
            reopened.db.close()

    def test_validate_peers_and_size(self):
        for source, target, text in [("ses_unknown", "wsl", "x"),
                                     ("ses_local", "linux", "x"),
                                     ("ses_local", "missing", "x"),
                                     ("ses_local", "wsl", "x" * 12001)]:
            with self.assertRaises(BridgeError):
                self.box.enqueue(source, target, text)

    def test_rate_limit(self):
        for _ in range(30):
            self.send()
        with self.assertRaises(BridgeError):
            self.send()

    def test_cancel_is_only_pending_not_execution(self):
        msg = self.send()
        self.assertEqual(self.box.cancel_pending(msg["id"])["state"], "cancelled")
        msg = self.send()
        self.box.claim()
        with self.assertRaises(BridgeError):
            self.box.cancel_pending(msg["id"])

    def test_file_integrity_no_overwrite_or_traversal(self):
        data = b"hola\x00\xff"
        payload = {"name": "example.bin", "data": base64.b64encode(data).decode(),
                   "sha256": hashlib.sha256(data).hexdigest()}
        mid = "msg_" + "a" * 32
        result = collab.receive_file(self.root, mid, payload)
        self.assertEqual(Path(result["path"]).read_bytes(), data)
        with self.assertRaises(BridgeError):
            collab.receive_file(self.root, mid, payload)
        for name in ("../escape", "/tmp/escape", ".env", "id_rsa"):
            with self.assertRaises(BridgeError):
                collab.receive_file(self.root, mid, {**payload, "name": name})
        with self.assertRaises(BridgeError):
            collab.receive_file(self.root, mid, {**payload, "sha256": "bad"})

    def test_only_staged_files_no_symlink(self):
        (self.root / "outgoing").mkdir()
        (self.root / "outside.txt").write_text("private")
        (self.root / "outgoing" / "link.txt").symlink_to(self.root / "outside.txt")
        with self.assertRaises(BridgeError):
            self.send(filename="link.txt")
        (self.root / "outgoing" / "test.txt").write_text("shared")
        self.send(filename="test.txt")
        self.assertEqual(self.box.claim()["kind"], "file")

    def test_oversized_file_is_rejected_before_enqueue(self):
        (self.root / "outgoing").mkdir()
        with (self.root / "outgoing" / "large.bin").open("wb") as file:
            file.truncate(collab.MAX_FILE + 1)
        with self.assertRaises(BridgeError):
            self.send(filename="large.bin")
        self.assertEqual(self.box.status(), [])

    def test_receive_rejects_symlink_destination(self):
        (self.root / "elsewhere").mkdir()
        (self.root / "inbox").symlink_to(self.root / "elsewhere")
        data = b"test"
        payload = {"name": "test.txt", "data": base64.b64encode(data).decode(),
                   "sha256": hashlib.sha256(data).hexdigest()}
        with self.assertRaises(BridgeError):
            collab.receive_file(self.root, "msg_" + "a" * 32, payload)
        self.assertEqual(list((self.root / "elsewhere").iterdir()), [])

    @patch("collab.endpoint")
    def test_delivery_stable_id_queue_and_no_interrupt(self, make):
        msg = self.send()
        job = self.box.claim()
        make.return_value.call.return_value = {"data": {"id": msg["id"]}}
        collab.deliver(self.cfg, job, self.root)
        args = make.return_value.call.call_args.args
        self.assertEqual(args[1], "/api/session/ses_remote/prompt")
        self.assertEqual(args[2]["id"], msg["id"])
        self.assertEqual(args[2]["delivery"], "queue")
        self.assertEqual(make.return_value.call.call_count, 1)
        job["payload"]["target_id"] = "ses_changed"
        with self.assertRaises(BridgeError):
            collab.deliver(self.cfg, job, self.root)

    @patch("collab.endpoint")
    def test_urgent_delivery_uses_steer_not_interrupt(self, make):
        msg = self.send(urgent=True)
        make.return_value.call.return_value = {"data": {"id": msg["id"]}}
        collab.deliver(self.cfg, self.box.claim(), self.root)
        calls = make.return_value.call.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], "/api/session/ses_remote/prompt")
        self.assertEqual(calls[0].args[2]["delivery"], "steer")

    @patch("collab.endpoint")
    @patch("collab.remote_rpc")
    def test_file_delivery_verifies_transfer_before_notification(self, remote, make):
        (self.root / "outgoing").mkdir()
        (self.root / "outgoing" / "test.txt").write_text("shared")
        msg = self.send(filename="test.txt")
        job = self.box.claim()
        remote.return_value = {"path": "/remote/inbox/test.txt", "sha256": job["payload"]["file"]["sha256"]}
        make.return_value.call.return_value = {"data": {"id": msg["id"]}}
        collab.deliver(self.cfg, job, self.root)
        self.assertEqual(remote.call_args.args[1]["op"], "receive_file")
        self.assertIn("/remote/inbox/test.txt", make.return_value.call.call_args.args[2]["text"])

    @patch("collab.endpoint")
    @patch("collab.remote_rpc", side_effect=BridgeError("SSH desconectado"))
    def test_failed_transfer_does_not_notify_or_interrupt(self, remote, make):
        (self.root / "outgoing").mkdir()
        (self.root / "outgoing" / "test.txt").write_text("shared")
        self.send(filename="test.txt")
        with self.assertRaises(BridgeError):
            collab.deliver(self.cfg, self.box.claim(), self.root)
        make.assert_not_called()

    @patch("collab.remote_rpc", return_value=None)
    @patch("collab.deliver", side_effect=BridgeError("ambiguous POST"))
    def test_relay_marks_uncertain_without_resend(self, deliver, remote):
        self.send()
        collab.relay_cycle(self.cfg, self.root)
        collab.relay_cycle(self.cfg, self.root)
        self.assertEqual(deliver.call_count, 1)
        self.assertEqual(self.box.status()[0]["state"], "uncertain")

    def test_mcp_metadata_required_and_no_reply_loop(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "send", "arguments": {"to": "wsl", "text": "hola"}}}
        self.assertTrue(mcp_server.handle(request, self.root)["result"]["isError"])
        request["params"]["_meta"] = {"ai.opencode/sessionID": "ses_local"}
        result = mcp_server.handle(request, self.root)
        self.assertNotIn("isError", result["result"])
        self.assertEqual(len(self.box.status()), 1)
        self.assertNotIn("interrupt", json.dumps(mcp_server.TOOLS))

    def test_mcp_handshake(self):
        request = {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        result = mcp_server.handle(request, self.root)["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertIsNone(mcp_server.handle({"method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
