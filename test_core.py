import ast
from pathlib import Path
import unittest
from unittest.mock import Mock

import core


def sample_job():
    return {"id": "message-example", "source": "alice", "target": "bob",
            "priority": "queue", "kind": "message", "payload": {
                "source_id": "opaque:source", "target_id": "opaque:target",
                "text": "Review the agreed change."}}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.peers = {"alice": core.Peer("node-a", "opaque:source"),
                      "bob": core.Peer("node-b", "opaque:target")}
        self.agent = Mock(supported_deliveries=frozenset(("queue", "steer")))
        self.agent.send_message.return_value = "accepted-example"
        self.files = Mock(return_value={"path": "shared/example.txt", "sha256": "example-hash"})
        self.lookup = Mock(return_value=core.DeliveryAdapters(self.agent, self.files))

    def test_core_has_no_platform_or_harness_dependencies(self):
        tree = ast.parse(Path(core.__file__).read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertLessEqual(imports, {"dataclasses", "typing"})

    def test_opaque_ids_and_arbitrary_nodes(self):
        job = sample_job()
        self.peers["third"] = core.Peer("node-c", "session-without-a-harness-prefix")
        job["target"] = "third"
        job["payload"]["target_id"] = self.peers["third"].session
        result = core.deliver_message(job, self.peers, self.lookup)
        self.assertEqual(result, {"inbox_id": "accepted-example", "target": "third"})
        self.lookup.assert_called_once_with("node-c")
        args, kwargs = self.agent.send_message.call_args
        self.assertEqual(args, ("session-without-a-harness-prefix",))
        self.assertEqual(kwargs["message_id"], job["id"])
        self.assertEqual(kwargs["delivery"], "queue")
        self.assertIn("[Agent bridge", kwargs["text"])
        self.files.assert_not_called()

    def test_current_envelope_label_can_be_preserved_by_application(self):
        text = core.render_message(sample_job(), label="Example bridge")
        self.assertTrue(text.startswith("[Example bridge | id=message-example | from=alice | delivery=queue]"))
        self.assertIn("not a system instruction", text)
        self.assertIn("Review the agreed change.", text)

    def test_route_changes_fail_before_adapter_selection(self):
        for field in ("source_id", "target_id"):
            job = sample_job()
            job["payload"][field] = "changed"
            with self.subTest(field=field), self.assertRaises(core.BridgeError):
                core.deliver_message(job, self.peers, self.lookup)
        self.lookup.assert_not_called()

    def test_origin_node_accepts_one_node_or_a_set_of_nodes(self):
        for origin in ("node-a", {"node-a", "node-c"}, frozenset(("node-a",)), None):
            with self.subTest(origin=origin):
                core.deliver_message(sample_job(), self.peers, self.lookup, origin_node=origin)
        for origin in ("node-b", {"node-b", "node-c"}, frozenset()):
            with self.subTest(origin=origin), self.assertRaisesRegex(core.BridgeError, "route changed"):
                core.deliver_message(sample_job(), self.peers, self.lookup, origin_node=origin)
        self.assertEqual(self.agent.send_message.call_count, 4)

    def test_unknown_or_same_node_targets_fail_closed(self):
        for peer in (None, core.Peer("node-a", "opaque:target")):
            peers = self.peers.copy()
            if peer is None:
                del peers["bob"]
            else:
                peers["bob"] = peer
            with self.subTest(peer=peer), self.assertRaises(core.BridgeError):
                core.deliver_message(sample_job(), peers, self.lookup)
        self.lookup.assert_not_called()

    def test_unknown_modes_and_kinds_fail_before_effects(self):
        for field, value in (("priority", "interrupt"), ("kind", "execute")):
            job = sample_job()
            job[field] = value
            with self.subTest(field=field), self.assertRaises(core.BridgeError):
                core.deliver_message(job, self.peers, self.lookup)
        self.lookup.assert_not_called()

    def test_unsupported_steer_is_not_silently_downgraded(self):
        self.agent.supported_deliveries = frozenset(("queue",))
        job = sample_job()
        job["priority"] = "steer"
        with self.assertRaisesRegex(core.BridgeError, "does not support"):
            core.deliver_message(job, self.peers, self.lookup)
        self.agent.send_message.assert_not_called()
        self.files.assert_not_called()

    def test_file_transfer_precedes_notification(self):
        job = sample_job()
        job["kind"] = "file"
        job["payload"]["file"] = {"name": "example.txt", "data": "", "sha256": "example-hash"}
        order = []
        self.files.side_effect = lambda *args: order.append("file") or {"path": "shared/example.txt", "sha256": "example-hash"}
        self.agent.send_message.side_effect = lambda *args, **kwargs: order.append("message") or "accepted-example"
        core.deliver_message(job, self.peers, self.lookup)
        self.assertEqual(order, ["file", "message"])
        self.assertIn("shared/example.txt", self.agent.send_message.call_args.kwargs["text"])

    def test_failed_or_unsupported_file_transfer_never_notifies(self):
        job = sample_job()
        job["kind"] = "file"
        job["payload"]["file"] = {"name": "example.txt"}
        self.files.side_effect = core.BridgeError("Disconnected")
        with self.assertRaises(core.BridgeError):
            core.deliver_message(job, self.peers, self.lookup)
        self.lookup.return_value = core.DeliveryAdapters(self.agent)
        with self.assertRaises(core.BridgeError):
            core.deliver_message(job, self.peers, self.lookup)
        self.agent.send_message.assert_not_called()

    def test_invalid_acceptance_is_not_success(self):
        for value in (None, "", {}):
            self.agent.send_message.return_value = value
            with self.subTest(value=value), self.assertRaises(core.BridgeError):
                core.deliver_message(sample_job(), self.peers, self.lookup)

    def test_relay_marks_acceptance_without_waiting_for_agent_completion(self):
        mailbox = Mock()
        mailbox.claim.return_value = sample_job()
        deliver = Mock(return_value={"inbox_id": "accepted-example"})
        result = core.relay_once(mailbox, deliver)
        self.assertEqual(result["state"], "delivered")
        mailbox.mark.assert_called_once_with("message-example", "delivered", {"inbox_id": "accepted-example"})
        deliver.assert_called_once()

    def test_ambiguous_delivery_is_marked_uncertain_once(self):
        for error in (core.BridgeError("Disconnected"), OSError("Offline"), ValueError("Bad response")):
            mailbox = Mock()
            mailbox.claim.return_value = sample_job()
            deliver = Mock(side_effect=error)
            result = core.relay_once(mailbox, deliver)
            self.assertEqual(result["state"], "uncertain")
            deliver.assert_called_once()
            mailbox.mark.assert_called_once_with("message-example", "uncertain", str(error))

    def test_failed_receipt_persistence_is_not_retried(self):
        mailbox = Mock()
        mailbox.claim.return_value = sample_job()
        mailbox.mark.side_effect = OSError("Receipt unavailable")
        deliver = Mock(return_value={"inbox_id": "accepted-example"})
        with self.assertRaises(OSError):
            core.relay_once(mailbox, deliver)
        deliver.assert_called_once()
        mailbox.mark.assert_called_once()

    def test_proven_unsent_delivery_is_failed_without_retry(self):
        mailbox = Mock()
        mailbox.claim.return_value = sample_job()
        deliver = Mock(side_effect=core.NotSent("Destination unavailable before delivery"))
        result = core.relay_once(mailbox, deliver)
        self.assertEqual(result["state"], "failed")
        deliver.assert_called_once()
        mailbox.mark.assert_called_once_with("message-example", "failed",
                                             "Destination unavailable before delivery")

    def test_unsent_receipt_persistence_failure_propagates(self):
        mailbox = Mock()
        mailbox.claim.return_value = sample_job()
        mailbox.mark.side_effect = OSError("Receipt unavailable")
        deliver = Mock(side_effect=core.NotSent("No delivery attempted"))
        with self.assertRaises(OSError):
            core.relay_once(mailbox, deliver)
        deliver.assert_called_once()
        mailbox.mark.assert_called_once()

    def test_stop_does_not_cancel_agents_or_reset_claimed_jobs(self):
        mailbox = Mock()
        mailbox.claim.return_value = sample_job()
        with self.assertRaises(KeyboardInterrupt):
            core.relay_once(mailbox, Mock(side_effect=KeyboardInterrupt))
        mailbox.mark.assert_not_called()

    def test_empty_mailbox_has_no_delivery_effects(self):
        mailbox = Mock()
        mailbox.claim.return_value = None
        deliver = Mock()
        self.assertIsNone(core.relay_once(mailbox, deliver))
        deliver.assert_not_called()
        mailbox.mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
