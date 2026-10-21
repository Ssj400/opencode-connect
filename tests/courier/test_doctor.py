"""The read-only courier doctor."""

import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opencode_connect.core import BridgeError
from opencode_connect.courier.app import RETENTION, Courier
from opencode_connect.courier.cli import main
from opencode_connect.courier.doctor import doctor, local_queue_root
from opencode_connect.courier.settings import SOCKET_TIMEOUT, write_config
from opencode_connect.local import mailbox
from tests.courier.fakes import configuration


class CourierDoctorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="courier-doctor-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "courier.json"
        self.queue_root = self.root / "queue"
        self.config = {**configuration(), "listen": "127.0.0.1:8765"}
        write_config(self.path, self.config)
        self.box = mailbox.LocalMailbox(self.queue_root, node="wsl")
        self.addCleanup(self.box.db.close)
        (self.box.state / "mail.sqlite3").chmod(0o600)
        self.app = Courier(self.box, self.config, config_path=self.path, clock=lambda: 1000)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_offline_doctor_is_readonly_and_never_opens_a_worker_or_socket(self):
        before = self.snapshot()
        with patch("opencode_connect.courier.cli.real_queue") as factory, \
                patch("opencode_connect.courier.worker.CourierServer") as server, \
                patch("opencode_connect.courier.doctor.http_json") as http, \
                patch("opencode_connect.courier.doctor.socket.create_connection") as tcp:
            result = doctor(self.path, root=self.queue_root, network=False)
        self.assertFalse(any(row["status"] == "FAIL" for row in result))
        for effect in (factory, server, http, tcp):
            effect.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_missing_config_and_queue_do_not_create_directories(self):
        missing = self.root / "not-created/courier.json"
        result = doctor(missing, root=self.root / "new-queue", network=False)
        self.assertEqual(result[0]["status"], "FAIL")
        self.assertFalse(missing.parent.exists())
        with patch("opencode_connect.courier.doctor.local_queue_root", return_value=self.root / "new-queue"), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("opencode_connect.courier.cli.real_queue") as factory:
            self.assertEqual(main(["--config", str(self.path), "doctor", "--offline"]), 0)
        self.assertFalse((self.root / "new-queue").exists())
        factory.assert_not_called()

    def test_doctor_reports_held_expiry_due_and_retry_without_changing_rows(self):
        card = self.app.invite("bob")
        self.app.trust("bob", {**card, "person": "bob"})
        inbound = self.app.receive("Bearer " + card["token"], {"text": "held", "idempotency_key": "expiry"})
        outgoing = self.box.enqueue("wsl:ses_example", "person:bob", "outgoing")
        with patch("opencode_connect.courier.doctor.http_json", side_effect=BridgeError("Offline.")):
            self.app.send_once()
        before = self.snapshot()
        result = doctor(self.path, root=self.queue_root, network=False, clock=lambda: 1000 + RETENTION + 1)
        summary = next(row for row in result if row["area"] == "Courier state")
        self.assertEqual((summary["held"], summary["expiry_due"], summary["retry_due"]), (1, 1, 1))
        self.assertEqual(self.box.get(inbound["id"])["state"], "held")
        self.assertEqual(self.box.get(outgoing["id"])["state"], "pending")
        self.assertEqual(self.snapshot(), before)

    def test_doctor_checks_listener_and_contact_identity_without_echoing_credentials(self):
        token = "X" * 43
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": token})
        with patch("opencode_connect.courier.doctor.socket.create_connection") as tcp, \
                patch("opencode_connect.courier.doctor.http_json", return_value={
                "person": "bob", "protocol": 1}) as http:
            result = doctor(self.path, root=self.queue_root)
        self.assertTrue(all(row["status"] == "OK" for row in result))
        tcp.assert_called_once_with(("127.0.0.1", 8765), timeout=SOCKET_TIMEOUT)
        self.assertEqual(http.call_args.kwargs["headers"], {"Authorization": "Bearer " + token})
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn("192.0.2.20", json.dumps(result))

    def test_doctor_distinguishes_bad_contact_and_inactive_listener(self):
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "X" * 43})
        for response in ({"person": "carol", "protocol": 1}, BridgeError("Private remote error.")):
            with self.subTest(response_type=type(response).__name__), \
                    patch("opencode_connect.courier.doctor.socket.create_connection", side_effect=ConnectionRefusedError), \
                    patch("opencode_connect.courier.doctor.http_json", **({"side_effect": response} if isinstance(response, Exception)
                                                 else {"return_value": response})):
                result = doctor(self.path, root=self.queue_root)
            self.assertEqual(next(row for row in result if row["area"] == "Listener")["status"], "FAIL")
            self.assertEqual(next(row for row in result if row["area"] == "Contact")["status"], "FAIL")
            self.assertNotIn("Private remote error", json.dumps(result))

    def test_insecure_state_stops_network_probes(self):
        (self.box.state / "mail.sqlite3").chmod(0o644)
        with patch("opencode_connect.courier.doctor.socket.create_connection") as tcp, \
                patch("opencode_connect.courier.doctor.http_json") as http:
            result = doctor(self.path, root=self.queue_root)
        self.assertEqual(result[-1]["status"], "FAIL")
        tcp.assert_not_called()
        http.assert_not_called()

    def test_stale_json_does_not_probe_revoked_contact(self):
        self.app.trust("bob", {"person": "bob", "url": "http://192.0.2.20:8765", "token": "X" * 43})
        with patch.object(self.app, "sync_config"):
            self.app.untrust("bob")
        before = self.snapshot()
        with patch("opencode_connect.courier.doctor.socket.create_connection"), \
                patch("opencode_connect.courier.doctor.http_json") as http:
            result = doctor(self.path, root=self.queue_root)
        self.assertTrue(any(row["area"] == "Snapshot" for row in result))
        http.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_readonly_state_path_matches_the_real_factory(self):
        import os
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_STATE_HOME": str(self.root)}):
            self.assertEqual(local_queue_root(), self.root / "opencode-connect/local")
            self.assertFalse(local_queue_root().exists())


if __name__ == "__main__":
    unittest.main()
