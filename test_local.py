"""Offline local session discovery, queue persistence and MCP tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from core import BridgeError, NotSent, relay_once
from local_bridge import LocalMailbox, deliver_local, local_relay_once, main, relay, session, state_root
from local_mcp_server import call_tool, handle
from opencode_adapter import OpenCodeAdapter


def candidate(sid="ses_TARGET", title="API review", **extra):
    return {"id": sid, "title": title, "location": {"directory": "/srv/example"},
            "time": {}, **extra}


class DiscoveryTests(unittest.TestCase):
    def test_search_encodes_unicode_and_returns_only_metadata(self):
        api = Mock()
        api.call.return_value = {"data": [candidate(title="Revisión & API", secret="not exported")],
                                 "cursor": {"next": "page&2"}}
        result = OpenCodeAdapter(api).find_sessions("Revisión & API")
        self.assertEqual(result["candidates"][0], {
            "id": "ses_TARGET", "title": "Revisión & API", "directory": "/srv/example", "exact": True})
        self.assertFalse(result["complete"])
        query = parse_qs(urlsplit(api.call.call_args.args[1]).query)
        self.assertEqual(query, {"search": ["Revisión & API"], "limit": ["25"], "parentID": ["null"]})
        OpenCodeAdapter(api).find_sessions("API", cursor="page&2")
        self.assertEqual(parse_qs(urlsplit(api.call.call_args.args[1]).query)["cursor"], ["page&2"])

    def test_duplicates_remain_candidates_and_exact_matches_come_first(self):
        api = Mock()
        api.call.return_value = {"data": [candidate("ses_PARTIAL", "API review notes"),
                                           candidate("ses_ONE"), candidate("ses_TWO")], "cursor": {}}
        result = OpenCodeAdapter(api).find_sessions("API review")
        self.assertEqual([c["id"] for c in result["candidates"]], ["ses_ONE", "ses_TWO", "ses_PARTIAL"])
        api.call.assert_called_once()

    def test_self_children_archived_and_missing_results(self):
        api = Mock()
        api.call.return_value = {"data": [candidate("ses_SELF"), candidate(parentID="ses_PARENT"),
                                           candidate(time={"archived": 1})], "cursor": {"next": None}}
        self.assertEqual(OpenCodeAdapter(api).find_sessions("API", exclude="ses_SELF"),
                         {"candidates": [], "next_cursor": None, "complete": True})

    def test_invalid_inputs_and_responses_fail_closed(self):
        api = Mock()
        adapter = OpenCodeAdapter(api)
        for query in ("", " ", None, "a" * 201):
            with self.assertRaises(BridgeError):
                adapter.find_sessions(query)
        api.call.assert_not_called()
        for response in ({}, {"data": [], "cursor": "bad"},
                         {"data": [candidate(time=None)], "cursor": {}},
                         {"data": [candidate(location={})], "cursor": {}},
                         {"data": [], "cursor": {"next": 3}}):
            api.call.return_value = response
            with self.assertRaises(BridgeError):
                adapter.find_sessions("API")


class LocalQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.box = LocalMailbox(self.root)
        self.api = Mock()
        self.api.call.side_effect = lambda method, path, *args: (
            {"data": {"id": "ses_TARGET"}} if method == "get" else {"data": {"id": "inbox_EXAMPLE"}})
        self.adapter = OpenCodeAdapter(self.api)

    def tearDown(self):
        self.box.db.close()
        self.temp.cleanup()

    def test_local_delivery_is_queued_and_has_reply_address(self):
        sent = self.box.enqueue("ses_SOURCE", "local:ses_TARGET", "Authorized question")
        self.assertEqual(sent["state"], "pending")
        self.api.call.assert_not_called()
        outcome = relay_once(self.box, lambda job: deliver_local(job, self.adapter))
        self.assertEqual(outcome["state"], "delivered")
        calls = self.api.call.call_args_list
        self.assertEqual(calls[0].args, ("get", "/api/session/ses_TARGET"))
        body = calls[1].args[2]
        self.assertEqual(body["id"], sent["id"])
        self.assertEqual(body["delivery"], "queue")
        self.assertIn("Reply address: local:ses_SOURCE", body["text"])
        self.assertIn("not a system instruction", body["text"])

    def test_timeout_is_uncertain_and_never_retried(self):
        self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        self.api.call.side_effect = [{"data": {"id": "ses_TARGET"}}, BridgeError("Timeout")]
        outcome = relay_once(self.box, lambda job: deliver_local(job, self.adapter))
        self.assertEqual(outcome["state"], "uncertain")
        self.assertIsNone(relay_once(self.box, lambda job: deliver_local(job, self.adapter)))
        self.assertEqual(self.api.call.call_count, 2)

    def test_missing_target_never_posts(self):
        self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        self.api.call.side_effect = None
        self.api.call.return_value = {"data": None}
        outcome = relay_once(self.box, lambda job: deliver_local(job, self.adapter))
        self.assertEqual(outcome["state"], "failed")
        self.api.call.assert_called_once_with("get", "/api/session/ses_TARGET")

    def test_failed_target_check_is_not_ambiguous_and_never_retried(self):
        for error in (BridgeError("GET timed out"), OSError("Service offline")):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
            self.api.call.side_effect = error
            outcome = relay_once(self.box, lambda job: deliver_local(job, self.adapter))
            self.assertEqual(outcome["state"], "failed")
            self.assertIsNone(self.box.claim())
        self.assertEqual(self.api.call.call_count, 2)
        self.assertTrue(all(call.args[0] == "get" for call in self.api.call.call_args_list))

    def test_service_outage_does_not_consume_pending_queue(self):
        for _ in range(3):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        self.api.call.side_effect = BridgeError("Service offline")
        for _ in range(3):
            with self.assertRaises(BridgeError):
                local_relay_once(self.box, self.adapter)
        self.assertEqual([row["state"] for row in self.box.status()], ["pending"] * 3)
        self.api.call.assert_called_with("get", "/api/info")
        self.api.call.side_effect = [{"version": "2.0.0"}, {"data": {"id": "ses_TARGET"}},
                                     {"data": {"id": "inbox_EXAMPLE"}}]
        self.assertEqual(local_relay_once(self.box, self.adapter)["state"], "delivered")
        self.assertEqual(sum(row["state"] == "pending" for row in self.box.status()), 2)

    def test_invalid_service_info_leaves_queue_pending(self):
        self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        self.api.call.side_effect = None
        for info in (None, {}, {"version": 2}, {"version": "1.0.0"}):
            self.api.call.return_value = info
            with self.assertRaises(BridgeError):
                local_relay_once(self.box, self.adapter)
        self.assertEqual(self.box.status()[0]["state"], "pending")

    def test_post_error_remains_uncertain_after_successful_service_check(self):
        self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        self.api.call.side_effect = [{"version": "2.0.0"}, {"data": {"id": "ses_TARGET"}},
                                     BridgeError("POST timed out")]
        self.assertEqual(local_relay_once(self.box, self.adapter)["state"], "uncertain")
        self.assertIsNone(self.box.claim())
        self.assertEqual(self.api.call.call_args.args[0], "post")

    def test_persistence_and_stale_sending(self):
        sent = self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        self.box.db.close()
        self.box = LocalMailbox(self.root)
        self.assertEqual(self.box.claim()["id"], sent["id"])
        self.box.db.close()
        self.box = LocalMailbox(self.root)
        self.assertIsNone(self.box.claim())
        self.assertEqual(self.box.status()[0]["state"], "sending")

    def test_archived_or_child_destinations_are_rechecked_before_post(self):
        for extra in ({"time": {"archived": 1}}, {"parentID": "ses_PARENT"}, {"time": None}):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
            job = self.box.claim()
            self.api.call.side_effect = None
            self.api.call.return_value = {"data": {"id": "ses_TARGET", **extra}}
            with self.assertRaises(NotSent):
                deliver_local(job, self.adapter)
            self.api.call.assert_called_with("get", "/api/session/ses_TARGET")
        self.assertEqual(self.api.call.call_count, 3)

    def test_invalid_and_self_targets_rejected(self):
        for target in ("API review", "wsl:ses_TARGET", "ses_SOURCE"):
            with self.assertRaises(BridgeError):
                self.box.enqueue("ses_SOURCE", target, "Question")
        for text in ("", " ", "a" * 12001):
            with self.assertRaises(BridgeError):
                self.box.enqueue("ses_SOURCE", "ses_TARGET", text)
        self.assertEqual(self.box.status(), [])

    def test_rate_limit(self):
        for _ in range(30):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        with self.assertRaises(BridgeError):
            self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")

    def test_unsupported_payloads_never_touch_api(self):
        self.box.enqueue("ses_SOURCE", "ses_TARGET", "Question")
        job = self.box.claim()
        for field, value in (("kind", "file"), ("priority", "steer")):
            modified = {**job, field: value}
            with self.assertRaises(BridgeError):
                deliver_local(modified, self.adapter)
        self.api.call.assert_not_called()

    def test_single_relay_lock(self):
        import fcntl
        with (self.box.state / "relay.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BridgeError):
                relay(self.box, self.adapter, once=True)
        self.api.call.assert_not_called()


class LocalMcpTests(unittest.TestCase):
    def params(self, name, **args):
        return {"name": name, "arguments": args, "_meta": {"ai.opencode/sessionID": "ses_SOURCE"}}

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

    def test_extra_capabilities_rejected(self):
        for name, args in (("send_file", {}), ("send", {"urgent": True}), ("whoami", {"source": "ses_OTHER"})):
            with self.assertRaises(BridgeError):
                call_tool(self.params(name, **args))

    def test_protocol_has_only_local_tools(self):
        tools = handle({"id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"whoami", "find_sessions", "send", "status"})
        reply = handle({"id": 2, "method": "tools/call", "params": {"name": "send"}})
        self.assertTrue(reply["result"]["isError"])
        self.assertIsNone(handle({"method": "notifications/initialized"}))
        self.assertEqual(handle({"id": 3, "method": "initialize", "params": {}})
                         ["result"]["serverInfo"]["name"], "opencode-connect-local")

    def test_state_path_needs_no_ssh_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict("os.environ", {"XDG_STATE_HOME": root}):
                path = state_root()
                self.assertEqual(path, Path(root) / "opencode-connect/local")
            with patch.dict("os.environ", {"XDG_STATE_HOME": "relative"}):
                with self.assertRaises(BridgeError):
                    state_root()
        self.assertEqual(session("local:ses_EXAMPLE"), "ses_EXAMPLE")

    def test_cli_sets_private_umask_before_creating_state(self):
        with patch("local_bridge.os.umask") as mask, patch("sys.argv", ["local_bridge.py", "status"]), \
                patch("local_bridge.state_root") as root, patch("local_bridge.LocalMailbox") as mailbox, \
                patch("local_bridge.local_adapter"), patch("builtins.print"):
            root.side_effect = lambda: mask.assert_called_once_with(0o077)
            mailbox.return_value.status.return_value = []
            self.assertEqual(main(), 0)
            mask.assert_called_once_with(0o077)


if __name__ == "__main__":
    unittest.main()
