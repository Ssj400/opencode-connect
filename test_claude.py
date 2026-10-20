"""Claude Code harness: identity stamps and pointer hooks. Offline; no Claude Code needed."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import claude_hook
from core import BridgeError
from local_bridge import LocalMailbox, state_root

ROOT = Path(__file__).resolve().parent
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

    def test_hooks_ignore_malformed_input_and_create_nothing(self):
        payloads = ("", "not json", "[]", json.dumps({"session_id": "not-a-uuid"}), json.dumps({"session_id": 7}),
                    json.dumps({"session_id": SESSION, "tool_use_id": "../escape"}))
        for event in ("pre-tool", "prompt", "stop"):
            for index, payload in enumerate(payloads):
                with self.subTest(event=event, payload=index):
                    self.assertEqual(self.hook(event, payload), "")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_hook_command_line_stamps_and_exits_zero(self):
        payload = json.dumps({"session_id": SESSION, "tool_use_id": "toolu_CLI"})
        result = subprocess.run([sys.executable, "-B", str(ROOT / "claude_hook.py"), "pre-tool"], input=payload,
                                capture_output=True, text=True, timeout=10, env=dict(os.environ))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(claude_hook.consume("toolu_CLI"), ADDRESS)


if __name__ == "__main__":
    unittest.main()
