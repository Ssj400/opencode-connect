"""Claude Code harness: identity stamps, pointer hooks and the MCP server. Offline; no Claude Code needed."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from opencode_connect.claude import hook as claude_hook
from opencode_connect.claude import mcp_server as claude_mcp_server
from opencode_connect.core import BridgeError
from opencode_connect.local.mailbox import LocalMailbox, state_root

ROOT = Path(__file__).resolve().parents[2]
SESSION = str(uuid.UUID(int=7))
ADDRESS = "claude:" + SESSION


class ClaudeTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "local",
                                              "OPENCODE_CONNECT_LOCAL_STATE_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)

    def mailbox(self):
        box = LocalMailbox(state_root())
        self.addCleanup(box.db.close)
        return box

    def hook(self, event, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(claude_hook.main([event], stdin=io.StringIO(text)), 0)
        return output.getvalue()


class StampTests(ClaudeTestCase):
    def test_a_stamp_names_the_session_once_and_stays_private(self):
        claude_hook.record("toolu_A", ADDRESS)
        folder = claude_hook.spool()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / "toolu_A.stamp").stat().st_mode), 0o600)
        self.assertEqual(claude_hook.consume("toolu_A"), ADDRESS)
        with self.assertRaisesRegex(BridgeError, "PreToolUse hook"):
            claude_hook.consume("toolu_A")

    def test_missing_stale_or_forged_stamps_fail_closed(self):
        claude_hook.record("toolu_OLD", ADDRESS, now=time.time() - claude_hook.STAMP_TTL - 1)
        with self.assertRaisesRegex(BridgeError, "expired"):
            claude_hook.consume("toolu_OLD")
        for tool_use_id in (None, "", "../escape", "toolu_" + "x" * 200):
            with self.subTest(tool_use_id=tool_use_id), self.assertRaises(BridgeError):
                claude_hook.consume(tool_use_id)
        folder = claude_hook.spool()
        valid = json.dumps({"address": ADDRESS, "created": time.time()})
        (folder / "toolu_PERSON.stamp").write_text(json.dumps({"address": "person:alice", "created": time.time()}))
        (folder / "toolu_GARBAGE.stamp").write_text("not json")
        (folder / "real.json").write_text(valid)
        (folder / "toolu_LINK.stamp").symlink_to(folder / "real.json")
        for tool_use_id in ("toolu_PERSON", "toolu_GARBAGE", "toolu_LINK"):
            with self.subTest(tool_use_id=tool_use_id), self.assertRaises(BridgeError):
                claude_hook.consume(tool_use_id)
        outside = folder.parent / "outside.stamp"  # A traversal must neither read nor delete it.
        outside.write_text(valid)
        with self.assertRaises(BridgeError):
            claude_hook.consume("../outside")
        self.assertTrue(outside.exists())
        with self.assertRaises(BridgeError):
            claude_hook.record("../escape", ADDRESS)
        self.assertFalse((folder.parent / "escape.stamp").exists())

    def test_recording_removes_only_stale_stamps(self):
        claude_hook.record("toolu_OLD", ADDRESS)
        claude_hook.record("toolu_RECENT", ADDRESS)
        folder = claude_hook.spool()
        past = time.time() - claude_hook.STAMP_TTL - 10
        os.utime(folder / "toolu_OLD.stamp", (past, past))
        claude_hook.record("toolu_NEW", ADDRESS)
        self.assertEqual(sorted(path.name for path in folder.glob("*.stamp")),
                         ["toolu_NEW.stamp", "toolu_RECENT.stamp"])


class HookTests(ClaudeTestCase):
    def test_pre_tool_stamps_the_calling_session(self):
        payload = {"session_id": SESSION.upper(), "tool_use_id": "toolu_X", "hook_event_name": "PreToolUse"}
        self.assertEqual(self.hook("pre-tool", payload), "")
        self.assertEqual(claude_hook.consume("toolu_X"), ADDRESS)

    def test_prompt_counts_waiting_messages_without_their_text(self):
        box = self.mailbox()
        self.assertEqual(self.hook("prompt", {"session_id": SESSION}), "")
        box.enqueue("ses_SOURCE", ADDRESS, "Secret plan for the release")
        box.enqueue("ses_SOURCE", "claude:" + str(uuid.UUID(int=8)), "For another session")
        for _ in range(2):  # Every prompt reports the count, announced or not.
            output = json.loads(self.hook("prompt", {"session_id": SESSION}))
            self.assertEqual(output, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                             "additionalContext": claude_hook.POINTER.format(count=1)}})
        self.assertNotIn("Secret", json.dumps(output))

    def test_stop_continues_once_per_new_message(self):
        box = self.mailbox()
        box.enqueue("ses_SOURCE", ADDRESS, "First")
        self.assertEqual(json.loads(self.hook("stop", {"session_id": SESSION, "stop_hook_active": False})),
                         {"decision": "block", "reason": claude_hook.POINTER.format(count=1)})
        self.assertEqual(self.hook("stop", {"session_id": SESSION, "stop_hook_active": False}), "")
        box.enqueue("ses_SOURCE", ADDRESS, "Second")
        self.assertEqual(self.hook("stop", {"session_id": SESSION, "stop_hook_active": True}), "")
        self.assertEqual(json.loads(self.hook("stop", {"session_id": SESSION}))["reason"],
                         claude_hook.POINTER.format(count=2))

    def test_a_prompt_announcement_spares_the_stop_that_follows(self):
        self.mailbox().enqueue("ses_SOURCE", ADDRESS, "Already mentioned")
        self.assertTrue(self.hook("prompt", {"session_id": SESSION}))
        self.assertEqual(self.hook("stop", {"session_id": SESSION}), "")

    def test_prompt_and_stop_remember_where_each_session_runs(self):
        other, third = ("claude:" + str(uuid.UUID(int=number)) for number in (8, 12))
        self.hook("prompt", {"session_id": SESSION, "cwd": "/srv/example/project"})
        with patch("opencode_connect.claude.hook.time.time", return_value=time.time() + 60):
            self.hook("stop", {"session_id": other.split(":")[1], "cwd": 7})
        with patch("opencode_connect.claude.hook.time.time", return_value=time.time() + 30):
            self.hook("prompt", {"session_id": third.split(":")[1], "cwd": "/srv/third"})
        self.hook("pre-tool", {"session_id": str(uuid.UUID(int=9)), "tool_use_id": "toolu_Y"})
        spool = self.home / "opencode-connect/local/.state/claude"
        (spool / "session-not-a-uuid.json").write_text("{}")
        (spool / ("session-" + str(uuid.UUID(int=10)) + ".json")).write_text("not json")
        # A record whose name is not its own session's is ignored.
        (spool / ("session-" + str(uuid.UUID(int=11)) + ".json")).write_text(
            json.dumps({"address": ADDRESS, "cwd": "/srv/forged", "seen": time.time() + 120}))
        self.assertEqual([(row["address"], row["cwd"]) for row in claude_hook.sessions()],
                         [(other, None), (third, "/srv/third"), (ADDRESS, "/srv/example/project")])

    def test_a_failed_session_record_never_hides_the_pointer(self):
        self.mailbox().enqueue("ses_SOURCE", ADDRESS, "Waiting")
        with patch("opencode_connect.claude.hook.note_session", side_effect=OSError("No space left")):
            self.assertIn("1 message(s)", self.hook("prompt", {"session_id": SESSION}))

    def test_hooks_ignore_malformed_input_and_create_nothing(self):
        payloads = ("", "not json", "[]", json.dumps({"session_id": "not-a-uuid"}), json.dumps({"session_id": 7}),
                    json.dumps({"session_id": SESSION, "tool_use_id": "../escape"}))
        for event in ("pre-tool", "prompt", "stop"):
            # The last payload is malformed only for pre-tool: its session is valid.
            for index, payload in enumerate(payloads if event == "pre-tool" else payloads[:-1]):
                with self.subTest(event=event, payload=index):
                    self.assertEqual(self.hook(event, payload), "")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_wake_exits_two_with_the_pointer_when_a_message_arrives(self):
        box = self.mailbox()
        arrivals = iter(["Arrived while idle"])

        def sleep(seconds):
            box.enqueue("ses_SOURCE", ADDRESS, next(arrivals))

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(claude_hook.wake(ADDRESS, sleep=sleep), 2)
        self.assertEqual(errors.getvalue(), claude_hook.POINTER.format(count=1))
        self.assertEqual(self.hook("stop", {"session_id": SESSION}), "")  # Announced once, by the wake.

    def test_wake_waits_for_messages_nobody_announced(self):
        self.mailbox().enqueue("ses_SOURCE", ADDRESS, "Already announced")
        self.assertTrue(self.hook("prompt", {"session_id": SESSION}))
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                raise InterruptedError("timeout")

        with self.assertRaises(InterruptedError):
            claude_hook.wake(ADDRESS, poll=0.5, sleep=sleep)
        self.assertEqual(polls, [0.5, 0.5, 0.5])

    def test_only_one_watcher_waits_per_session(self):
        import fcntl
        with open(claude_hook.spool() / f"wake-{SESSION}.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.mailbox().enqueue("ses_SOURCE", ADDRESS, "Waiting")
            self.assertEqual(claude_hook.wake(ADDRESS, sleep=self.fail), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(claude_hook.wake(ADDRESS, sleep=self.fail), 2)

    def test_wake_command_line_exits_two_only_for_a_new_message(self):
        command = [sys.executable, "-B", str(ROOT / "bin/claude_hook.py"), "wake"]
        self.mailbox().enqueue("ses_SOURCE", ADDRESS, "Secret plan")
        result = subprocess.run(command, input=json.dumps({"session_id": SESSION}), capture_output=True,
                                text=True, timeout=10, env=dict(os.environ))
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (2, "", claude_hook.POINTER.format(count=1)))
        result = subprocess.run(command, input="not json", capture_output=True, text=True, timeout=10,
                                env=dict(os.environ))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_hook_command_line_stamps_and_exits_zero(self):
        payload = json.dumps({"session_id": SESSION, "tool_use_id": "toolu_CLI"})
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/claude_hook.py"), "pre-tool"], input=payload,
                                capture_output=True, text=True, timeout=10, env=dict(os.environ))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(claude_hook.consume("toolu_CLI"), ADDRESS)


class ClaudeServerTests(ClaudeTestCase):
    def call(self, name, tool_use="toolu_CALL", stamp=True, **arguments):
        if stamp:
            claude_hook.record(tool_use, ADDRESS)
        request = {"id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": arguments, "_meta": {"claudecode/toolUseId": tool_use}}}
        result = claude_mcp_server.handle(request)["result"]
        if result.get("isError"):
            raise BridgeError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def test_identity_comes_only_from_the_hook_stamp(self):
        self.assertEqual(self.call("whoami"), {"address": ADDRESS})
        with self.assertRaisesRegex(BridgeError, "PreToolUse hook"):
            self.call("whoami", stamp=False)
        with self.assertRaisesRegex(BridgeError, "tool use ID"):
            claude_mcp_server.call_claude_tool({"name": "whoami", "arguments": {}})
        with self.assertRaises(BridgeError):  # Arguments can never name the source.
            self.call("send", to="ses_TARGET", text="Hello", source="ses_OTHER")

    def test_send_queues_from_this_session(self):
        sent = self.call("send", to="ses_TARGET", text="From Claude")
        job = self.mailbox().claim()
        self.assertEqual((job["id"], job["source"], job["target"]), (sent["id"], ADDRESS, "local:ses_TARGET"))

    def test_receive_hands_over_one_message_and_acknowledges_it(self):
        box = self.mailbox()
        first = box.enqueue("ses_SOURCE", ADDRESS, "First question")
        box.enqueue("ses_SOURCE", ADDRESS, "Second question")
        other = box.enqueue("ses_SOURCE", "claude:" + str(uuid.UUID(int=8)), "Not for this session")
        received = self.call("receive")
        self.assertEqual((received["id"], received["remaining"]), (first["id"], 1))
        self.assertIn("First question", received["message"])
        self.assertIn("Reply address: local:ses_SOURCE", received["message"])
        self.assertEqual(json.loads(box.get(first["id"])["result"]), {"received_by": ADDRESS})
        self.assertEqual((box.get(first["id"])["state"], box.get(other["id"])["state"]), ("delivered", "pending"))
        self.assertEqual(self.call("receive", tool_use="toolu_2")["remaining"], 0)
        self.assertEqual(self.call("receive", tool_use="toolu_3"), {"message": None, "remaining": 0})
        with self.assertRaises(BridgeError):
            self.call("receive", tool_use="toolu_4", limit=5)

    def test_receive_quotes_remote_people(self):
        box = self.mailbox()
        held = box.insert_inbound("person:alice", "Line one\nLine two", remote_key="key-1")
        box.approve_inbound(held["id"], ADDRESS)
        message = self.call("receive")["message"]
        self.assertIn("Quoted text from person:alice:\n> Line one\n> Line two", message)
        self.assertIn("Reply address: person:alice", message)

    def test_a_failed_acknowledgment_leaves_the_message_pending(self):
        box = self.mailbox()
        sent = box.enqueue("ses_SOURCE", ADDRESS, "Keep me")
        with patch.object(LocalMailbox, "mark", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaisesRegex(BridgeError, "disk I/O error"):
                self.call("receive")
        self.assertEqual(box.get(sent["id"])["state"], "pending")

    def test_a_message_rerouted_after_enqueue_is_failed_not_shown(self):
        box = self.mailbox()
        sent = box.enqueue("ses_SOURCE", ADDRESS, "Tampered")
        payload = dict(box.get(sent["id"])["payload"], target_address="claude:" + str(uuid.UUID(int=8)))
        with box.transaction():
            box.db.execute("UPDATE messages SET payload=? WHERE id=?", (json.dumps(payload), sent["id"]))
        received = self.call("receive")
        self.assertEqual((received["id"], received["state"], received["remaining"]), (sent["id"], "failed", 0))
        self.assertNotIn("message", received)
        self.assertEqual(box.get(sent["id"])["state"], "failed")

    def test_example_settings_wire_each_hook_to_its_event(self):
        settings = json.loads((ROOT / "examples/claude-settings.local.json.example").read_text())
        hooks = settings["hooks"]
        wired = {(event, hook["command"].split()[-1]): hook
                 for event, groups in hooks.items() for group in groups for hook in group["hooks"]}
        self.assertEqual(set(wired), {("PreToolUse", "pre-tool"), ("UserPromptSubmit", "prompt"),
                                      ("Stop", "stop"), ("Stop", "wake")})
        for (event, subcommand), hook in wired.items():
            with self.subTest(subcommand=subcommand):
                self.assertTrue(hook["command"].endswith(f"/bin/claude_hook.py {subcommand}"))
                self.assertEqual(hook.get("asyncRewake", False), subcommand == "wake")
        self.assertIsInstance(wired[("Stop", "wake")]["timeout"], int)
        (matcher,) = [group["matcher"] for group in hooks["PreToolUse"]]
        for tool in claude_mcp_server.TOOLS:
            self.assertTrue(re.fullmatch(matcher, "mcp__opencode_connect__" + tool["name"]))
        self.assertFalse(re.fullmatch(matcher, "mcp__another_server__send"))
        self.assertLessEqual({f"{rule}(~/{path}/opencode-connect/**)" for rule in ("Read", "Edit")
                              for path in (".config", ".local/state")}, set(settings["permissions"]["deny"]))

    def test_protocol_lists_the_local_tools_and_receive(self):
        tools = claude_mcp_server.handle({"id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools},
                         {"whoami", "find_sessions", "send", "status", "diagnose", "receive"})
        info = claude_mcp_server.handle({"id": 2, "method": "initialize", "params": {}})["result"]
        self.assertEqual(info["serverInfo"]["name"], "opencode-connect-claude")
        self.assertIn("PreToolUse hook", info["instructions"])
        self.assertIsNone(claude_mcp_server.handle({"method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
