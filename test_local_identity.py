"""Offline canonical local identity and persisted queue contract regressions."""

import itertools
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import collab
import config
from core import BridgeError, relay_once
from local_bridge import (LocalMailbox, canonical, deliver_local, local_adapter, local_relay_once,
                          session, state_root)

# Synthetic Claude Code session ID, built at runtime so no operational-looking ID is stored.
CLAUDE_SESSION = str(uuid.UUID(int=1))
from local_mcp_server import call_tool
from opencode_adapter import OpenCodeAdapter


def tool_params(name, source="ses_ALPHA", **arguments):
    return {"name": name, "arguments": arguments,
            "_meta": {"ai.opencode/sessionID": source}}


def mixed_config():
    """Synthetic paired-node configuration; never installed or contacted."""
    return {"side": "linux", "peers": {
        "desk": {"side": "linux", "sessionID": "ses_ALPHA"},
        "remote": {"side": "wsl", "sessionID": "ses_REMOTE"}},
        "ssh": {"host": "192.0.2.20", "ssh_user": "example", "wsl_user": "example",
                "remote_opencode": "/opt/example/opencode", "remote_directory": "/srv/example"}}


class LocalNodeValidationTests(unittest.TestCase):
    def test_bridge_state_override_does_not_change_cli_discovery_environment(self):
        with tempfile.TemporaryDirectory(prefix="local-state-") as root:
            with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_STATE_HOME": root,
                                         "XDG_STATE_HOME": "/srv/example/service-state"}):
                self.assertEqual(state_root(), Path(root) / "opencode-connect/local")
                self.assertEqual(os.environ["XDG_STATE_HOME"], "/srv/example/service-state")
            with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_STATE_HOME": "relative"}):
                with self.assertRaises(BridgeError):
                    state_root()

    def test_factory_constructs_only_local_transport_with_explicit_cli(self):
        with patch("local_bridge.LocalCommandTransport") as transport, \
                patch("local_bridge.OpenCodeCli") as cli, \
                patch("local_bridge.shutil.which", return_value="/opt/example/opencode") as which, \
                patch("transports.WindowsWslSshTransport",
                      side_effect=AssertionError("Local factory must not construct SSH")) as remote:
            adapter = local_adapter()
            which.assert_called_once_with("opencode")
            transport.assert_called_once_with()
            cli.assert_called_once_with(transport.return_value, "/opt/example/opencode")
            self.assertIsInstance(adapter, OpenCodeAdapter)
            self.assertIs(adapter.api, cli.return_value)
            remote.assert_not_called()

    def test_factory_missing_cli_path_keeps_local_transport(self):
        with patch("local_bridge.LocalCommandTransport") as transport, \
                patch("local_bridge.OpenCodeCli") as cli, \
                patch("local_bridge.shutil.which", return_value=None), \
                patch("transports.WindowsWslSshTransport",
                      side_effect=AssertionError("Missing CLI must not trigger SSH")) as remote:
            adapter = local_adapter()
            transport.assert_called_once_with()
            cli.assert_called_once_with(transport.return_value, "opencode")
            self.assertIs(adapter.api, cli.return_value)
            remote.assert_not_called()

    def test_default_environment_and_explicit_node(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.local_node(), "local")
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "linux"}):
            self.assertEqual(config.local_node(), "linux")
            self.assertEqual(config.local_node("Desk_2-test"), "Desk_2-test")
        for node in ("a", "Z" + "0" * 63, "local", "wsl"):
            with self.subTest(node=node):
                self.assertEqual(config.local_node(node), node)

    def test_invalid_environment_and_explicit_nodes_fail_closed(self):
        invalid = ("", " ", " linux", "linux ", "1linux", "_linux", "a" * 65,
                   "linux:ses_ALPHA", "linux/other", "node.name", "node\n", "nódé")
        for node in invalid:
            with self.subTest(node=node):
                with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": node}):
                    with self.assertRaises(BridgeError):
                        config.local_node()
                with self.assertRaises(BridgeError):
                    config.local_node(node)
        for node in (False, 7, [], {}):
            with self.subTest(node=node), self.assertRaises(BridgeError):
                config.local_node(node)

    def test_reserved_namespaces_cannot_be_node_names(self):
        for node in ("claude", "Claude", "person", "PERSON"):
            with self.subTest(node=node):
                with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": node}):
                    with self.assertRaisesRegex(BridgeError, "reserved"):
                        config.local_node()
                with self.assertRaisesRegex(BridgeError, "reserved"):
                    config.local_node(node)
        for node in ("claudette", "personal", "claude2"):
            with self.subTest(node=node):
                self.assertEqual(config.local_node(node), node)

    def test_session_accepts_only_bare_alias_or_own_canonical_address(self):
        for value in ("ses_ALPHA", "local:ses_ALPHA", "linux:ses_ALPHA"):
            with self.subTest(value=value):
                self.assertEqual(session(value, node="linux"), "ses_ALPHA")
        for value in ("wsl:ses_ALPHA", "other:ses_ALPHA", "linux:local:ses_ALPHA",
                      "linux:", "linux:ses_ALPHA/other", "ses_ALPHA\n", None):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                session(value, node="linux")
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "linux"}):
            self.assertEqual(session("linux:ses_ALPHA"), "ses_ALPHA")

    def test_canonical_addresses_cover_three_namespaces(self):
        cases = {"ses_ALPHA": "linux:ses_ALPHA", "local:ses_ALPHA": "linux:ses_ALPHA",
                 "linux:ses_ALPHA": "linux:ses_ALPHA",
                 "claude:" + CLAUDE_SESSION.upper(): "claude:" + CLAUDE_SESSION,
                 "person:Alice": "person:alice", "person:bob_2-x": "person:bob_2-x"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(canonical(value, node="linux"), expected)
        invalid = ("wsl:ses_ALPHA", "other:ses_ALPHA", "linux:local:ses_ALPHA", "linux:",
                   "claude:ses_ALPHA", "claude:not-a-uuid", "claude:" + CLAUDE_SESSION + "\n",
                   "person:", "person:has space", "person:a/b", "person:1abc", "", None, 7)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(BridgeError):
                canonical(value, node="linux")


class LocalIdentityQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.box = self.open_box("linux")

    def open_box(self, node):
        box = LocalMailbox(self.root, node=node)
        self.addCleanup(box.db.close)
        return box

    def records(self):
        return [tuple(row) for row in self.box.db.execute("SELECT * FROM messages ORDER BY id")]

    def contract(self):
        exists = self.box.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_contract'").fetchone()
        if exists is None:
            return None
        row = self.box.db.execute("SELECT * FROM local_contract WHERE singleton=1").fetchone()
        return dict(row) if row is not None else None

    def seed_states(self):
        for state in ("pending", "sending", "uncertain"):
            sent = self.box.enqueue("ses_ALPHA", "ses_BETA", "Synthetic " + state)
            with self.box.db:
                self.box.db.execute("UPDATE messages SET state=? WHERE id=?", (state, sent["id"]))

    def insert_pending(self, box, source, target, created):
        """A row from another producer, written with the real schema."""
        message_id = "msg_" + uuid.uuid4().hex
        payload = {"text": "Synthetic row", "source_id": source.split(":", 1)[1],
                   "target_id": target.split(":", 1)[1],
                   "source_address": source, "target_address": target}
        with box.db:
            box.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL)",
                           (message_id, created, source, target, "queue", "message",
                            json.dumps(payload), "pending"))
        return message_id

    def test_each_consumer_claims_only_its_own_namespace(self):
        self.box.enqueue("ses_ALPHA", "ses_BETA", "Bind the contract")
        self.box.claim()
        claude = "claude:" + CLAUDE_SESSION
        other_claude = "claude:" + str(uuid.UUID(int=2))
        rows = {"claude": self.insert_pending(self.box, "linux:ses_ALPHA", claude, 1.0),
                "other": self.insert_pending(self.box, "linux:ses_ALPHA", other_claude, 2.0),
                "person": self.insert_pending(self.box, "linux:ses_ALPHA", "person:alice", 3.0),
                "opencode": self.insert_pending(self.box, claude, "linux:ses_BETA", 4.0)}
        self.assertEqual(self.box.claim()["id"], rows["opencode"])
        self.assertIsNone(self.box.claim())
        self.assertEqual(self.box.claim(claude)["id"], rows["claude"])
        self.assertIsNone(self.box.claim(claude))
        self.assertEqual(self.box.claim("person:")["id"], rows["person"])
        self.assertEqual(self.box.claim("claude:")["id"], rows["other"])

    def test_claim_prefix_is_literal_not_a_like_pattern(self):
        box = self.open_box("lin_x")
        box.enqueue("ses_ALPHA", "ses_BETA", "Bind the contract")
        box.claim()
        self.insert_pending(box, "linAx:ses_ALPHA", "linAx:ses_FOREIGN", 1.0)
        self.assertIsNone(box.claim())

    def set_stored_contract(self, version, node="linux"):
        with self.box.db:
            self.box.db.execute("UPDATE local_contract SET schema_version=?, canonical_node=? WHERE singleton=1",
                                (version, node))

    def test_version_1_queue_fails_closed_until_migrated_with_rows_untouched(self):
        self.seed_states()
        self.set_stored_contract(1)
        before = self.records()
        self.assert_blocked_unchanged(self.box)
        self.assertTrue(self.box.diagnostics()["migration_needed"])
        self.assertEqual(self.box.migrate(), {"migrated": True, "contract": self.box.contract})
        self.assertEqual(self.records(), before)
        self.assertEqual(self.contract()["schema_version"], 2)
        self.assertEqual(self.box.migrate(), {"migrated": False, "contract": self.box.contract})
        self.assertEqual(self.box.claim()["payload"]["text"], "Synthetic pending")

    def test_migration_refuses_other_nodes_and_unknown_versions(self):
        self.seed_states()
        for version, node in ((1, "wsl"), (3, "linux")):
            with self.subTest(version=version, node=node):
                self.set_stored_contract(version, node)
                before = self.records()
                with self.assertRaisesRegex(BridgeError, "Only a version 1 queue"):
                    self.box.migrate()
                self.assertEqual((self.contract()["schema_version"], self.contract()["canonical_node"]),
                                 (version, node))
                self.assertEqual(self.records(), before)

    def test_unbound_queue_needs_no_migration(self):
        self.assertEqual(self.box.migrate(), {"migrated": False, "contract": None})
        self.assertIsNone(self.contract())

    def test_version_1_code_fails_closed_on_a_version_2_queue(self):
        self.seed_states()
        old = self.open_box("linux")
        old.contract = {**old.contract, "schema_version": 1}
        self.assert_blocked_unchanged(old)

    def test_concurrent_migrations_upgrade_exactly_once(self):
        self.seed_states()
        self.set_stored_contract(1)

        def migrate(_):
            box = LocalMailbox(self.root, node="linux")
            try:
                return box.migrate()["migrated"]
            finally:
                box.db.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(migrate, range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.contract()["schema_version"], 2)

    def test_resolve_records_a_human_outcome_only_for_stale_states(self):
        self.seed_states()
        rows = {row["state"]: row["id"] for row in self.box.db.execute("SELECT id, state FROM messages")}
        self.assertEqual(self.box.resolve(rows["sending"], "delivered"), {"id": rows["sending"], "state": "delivered"})
        self.assertEqual(self.box.resolve(rows["uncertain"], "failed")["state"], "failed")
        result = json.loads(self.box.db.execute("SELECT result FROM messages WHERE id=?",
                                                (rows["sending"],)).fetchone()[0])
        self.assertEqual(result, {"resolved_from": "sending", "by": "operator"})
        for message_id, state in ((rows["pending"], "failed"), (rows["sending"], "failed"),
                                  ("msg_" + "0" * 32, "failed"), (rows["pending"], "pending")):
            with self.subTest(message_id=message_id, state=state), self.assertRaises(BridgeError):
                self.box.resolve(message_id, state)

    def test_claim_requires_a_namespace(self):
        for match in ("", "linux", "person", b"linux:", 7):
            with self.subTest(match=match), self.assertRaises(BridgeError):
                self.box.claim(match)

    def assert_blocked_unchanged(self, box):
        before = self.records()
        with self.assertRaises(BridgeError):
            box.enqueue("ses_ALPHA", "ses_BETA", "Must not be queued")
        self.assertEqual(self.records(), before)
        with self.assertRaises(BridgeError):
            box.claim()
        self.assertEqual(self.records(), before)

    def test_all_address_variants_persist_canonical_addresses(self):
        sources = ("ses_ALPHA", "local:ses_ALPHA", "linux:ses_ALPHA")
        targets = ("ses_BETA", "local:ses_BETA", "linux:ses_BETA")
        for source, target in itertools.product(sources, targets):
            with self.subTest(source=source, target=target):
                result = self.box.enqueue(source, target, "Synthetic question")
                job = self.box.claim()
                self.assertEqual(job["id"], result["id"])
                self.assertEqual(job["source"], "linux:ses_ALPHA")
                self.assertEqual(job["target"], "linux:ses_BETA")
                self.assertEqual(job["payload"]["source_address"], "linux:ses_ALPHA")
                self.assertEqual(job["payload"]["target_address"], "linux:ses_BETA")
                self.assertEqual(job["payload"]["source_id"], "ses_ALPHA")
                self.assertEqual(job["payload"]["target_id"], "ses_BETA")

    def test_all_self_variants_and_foreign_sources_or_targets_rejected(self):
        aliases = ("ses_ALPHA", "local:ses_ALPHA", "linux:ses_ALPHA")
        for source, target in itertools.product(aliases, repeat=2):
            with self.subTest(source=source, target=target), self.assertRaises(BridgeError):
                self.box.enqueue(source, target, "Synthetic self message")
        for source, target in (("wsl:ses_ALPHA", "ses_BETA"),
                               ("ses_ALPHA", "wsl:ses_BETA"),
                               ("other:ses_ALPHA", "local:ses_BETA")):
            with self.subTest(source=source, target=target), self.assertRaises(BridgeError):
                self.box.enqueue(source, target, "Synthetic foreign message")
        self.assertEqual(self.records(), [])

    def test_remote_people_never_enter_as_queue_sources(self):
        for source in ("person:alice", "person:ALICE"):
            with self.subTest(source=source):
                with self.assertRaisesRegex(BridgeError, "only through the courier"):
                    self.box.enqueue(source, "ses_BETA", "Forged inbound message")
        self.assertEqual(self.records(), [])

    def test_claude_sources_and_claude_or_person_targets_are_queued_canonically(self):
        claude = "claude:" + CLAUDE_SESSION
        cases = ((("ses_ALPHA", "claude:" + CLAUDE_SESSION.upper()), ("linux:ses_ALPHA", claude)),
                 (("ses_ALPHA", "person:Alice"), ("linux:ses_ALPHA", "person:alice")),
                 ((claude, "local:ses_BETA"), (claude, "linux:ses_BETA")))
        for (source, target), expected in cases:
            with self.subTest(source=source, target=target):
                sent = self.box.enqueue(source, target, "Synthetic question")
                row = self.box.db.execute("SELECT source, target, payload FROM messages WHERE id=?",
                                          (sent["id"],)).fetchone()
                payload = json.loads(row["payload"])
                self.assertEqual((row["source"], row["target"]), expected)
                self.assertEqual((payload["source_address"], payload["target_address"]), expected)
        with self.assertRaisesRegex(BridgeError, "own session"):
            self.box.enqueue(claude, "claude:" + CLAUDE_SESSION.upper(), "Self in another spelling")

    def test_node_is_pinned_when_environment_changes(self):
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "linux"}):
            box = LocalMailbox(self.root)
        self.addCleanup(box.db.close)
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "wsl"}):
            box.enqueue("ses_ALPHA", "ses_BETA", "Pinned identity")
            job = box.claim()
        self.assertEqual(box.node, "linux")
        self.assertEqual(job["source"], "linux:ses_ALPHA")

    def test_empty_claim_binds_exactly_one_contract(self):
        self.assertIsNone(self.contract())
        self.assertIsNone(self.box.claim())
        contract = self.contract()
        self.assertEqual(contract["singleton"], 1)
        self.assertEqual(contract["canonical_node"], "linux")
        self.assertIsInstance(contract["schema_version"], int)
        self.assertIsInstance(contract["mode"], str)
        self.box.enqueue("ses_ALPHA", "ses_BETA", "First message")
        self.assertEqual(self.contract(), contract)
        self.assertEqual(self.box.db.execute("SELECT count(*) FROM local_contract").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.box.db:
                self.box.db.execute("INSERT INTO local_contract VALUES (2, ?, ?, ?)",
                                    (contract["schema_version"], contract["mode"], "linux"))

    def test_first_enqueue_binds_previously_empty_queue(self):
        self.assertIsNone(self.contract())
        self.box.enqueue("ses_ALPHA", "ses_BETA", "First message")
        self.assertEqual(self.contract()["canonical_node"], "linux")
        self.assertEqual(len(self.records()), 1)

    def test_changed_node_blocks_pending_sending_and_uncertain_without_rebinding(self):
        self.seed_states()
        contract = self.contract()
        other = self.open_box("wsl")
        self.assert_blocked_unchanged(other)
        self.assertEqual(self.contract(), contract)
        self.assertFalse(other.diagnostics()["compatible"])

    def test_existing_connection_rechecks_external_contract_change(self):
        self.seed_states()
        other = self.open_box("linux")
        with other.db:
            other.db.execute("UPDATE local_contract SET canonical_node='wsl' WHERE singleton=1")
        self.assert_blocked_unchanged(self.box)

    def test_schema_and_mode_mismatch_block_both_operations(self):
        self.seed_states()
        original = self.contract()
        for field, value in (("schema_version", original["schema_version"] + 1),
                             ("mode", "synthetic-unsupported-mode")):
            with self.subTest(field=field):
                with self.box.db:
                    self.box.db.execute(f"UPDATE local_contract SET {field}=? WHERE singleton=1", (value,))
                self.assert_blocked_unchanged(self.box)
                self.assertFalse(self.box.diagnostics()["compatible"])
                with self.box.db:
                    self.box.db.execute(f"UPDATE local_contract SET {field}=? WHERE singleton=1",
                                        (original[field],))

    def test_nonempty_unbound_database_is_never_adopted(self):
        self.seed_states()
        with self.box.db:
            self.box.db.execute("DROP TABLE local_contract")
        legacy = self.open_box("linux")
        self.assert_blocked_unchanged(legacy)
        diagnostics = legacy.diagnostics()
        self.assertFalse(diagnostics["compatible"])
        self.assertIsNone(diagnostics["stored"])
        self.assertIsNone(self.contract())

    def test_malformed_contract_fails_cleanly_without_repair(self):
        self.seed_states()
        with self.box.db:
            self.box.db.execute("DROP TABLE local_contract")
            self.box.db.execute("CREATE TABLE local_contract (unsupported TEXT)")
        self.assert_blocked_unchanged(self.box)
        with self.assertRaisesRegex(BridgeError, "Cannot read local queue contract"):
            self.box.diagnostics()
        with self.assertRaises(sqlite3.OperationalError):
            self.box.db.execute("SELECT canonical_node FROM local_contract")

    def test_claim_checks_contract_inside_selection_transaction(self):
        self.box.enqueue("ses_ALPHA", "ses_BETA", "Transaction test")
        statements = []
        self.box.db.set_trace_callback(statements.append)
        self.addCleanup(self.box.db.set_trace_callback, None)
        self.box.claim()
        sql = [statement.upper() for statement in statements]
        begin = next(i for i, statement in enumerate(sql) if "BEGIN IMMEDIATE" in statement)
        contract = next(i for i, statement in enumerate(sql)
                        if "SELECT" in statement and "LOCAL_CONTRACT" in statement)
        selection = next(i for i, statement in enumerate(sql)
                         if "SELECT" in statement and "FROM MESSAGES" in statement
                         and "PENDING" in statement)
        update = next(i for i, statement in enumerate(sql) if "UPDATE MESSAGES" in statement)
        commit = next(i for i, statement in enumerate(sql) if statement == "COMMIT")
        self.assertLess(begin, contract)
        self.assertLess(contract, selection)
        self.assertLess(selection, update)
        self.assertLess(update, commit)

    def test_enqueue_checks_contract_inside_insertion_transaction(self):
        self.box.enqueue("ses_ALPHA", "ses_BETA", "Bind queue")
        statements = []
        self.box.db.set_trace_callback(statements.append)
        self.addCleanup(self.box.db.set_trace_callback, None)
        self.box.enqueue("ses_ALPHA", "ses_BETA", "Transaction test")
        sql = [statement.upper() for statement in statements]
        begin = next(i for i, statement in enumerate(sql) if "BEGIN IMMEDIATE" in statement)
        contract = next(i for i, statement in enumerate(sql)
                        if "SELECT" in statement and "LOCAL_CONTRACT" in statement)
        insert = next(i for i, statement in enumerate(sql) if "INSERT INTO MESSAGES" in statement)
        commit = next(i for i, statement in enumerate(sql) if statement == "COMMIT")
        self.assertLess(begin, contract)
        self.assertLess(contract, insert)
        self.assertLess(insert, commit)

    def test_competing_first_enqueues_cannot_bind_different_nodes(self):
        def enqueue(node):
            box = LocalMailbox(self.root, node=node)
            try:
                try:
                    box.enqueue("ses_ALPHA", "ses_BETA", "Concurrent binding")
                except BridgeError:
                    return False
                return True
            finally:
                box.db.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(enqueue, ("linux", "wsl")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.records()), 1)
        job = self.box.db.execute("SELECT source FROM messages").fetchone()
        self.assertEqual(job["source"], self.contract()["canonical_node"] + ":ses_ALPHA")

    def test_diagnostics_exposes_only_contract_and_queue_path(self):
        self.box.enqueue("ses_ALPHA", "ses_BETA", "PRIVATE_SYNTHETIC_TEXT")
        for box, compatible in ((self.box, True), (self.open_box("wsl"), False)):
            with self.subTest(node=box.node):
                result = box.diagnostics()
                self.assertEqual(set(result), {"configured", "stored", "queue_path", "compatible",
                                               "migration_needed"})
                self.assertEqual(result["compatible"], compatible)
                self.assertFalse(result["migration_needed"])
                self.assertEqual(result["configured"]["canonical_node"], box.node)
                self.assertEqual(result["stored"]["canonical_node"], "linux")
                self.assertIsInstance(result["queue_path"], str)
                self.assertEqual(Path(result["queue_path"]), self.box.state / "mail.sqlite3")
                serialized = json.dumps(result)
                for private in ("ses_ALPHA", "ses_BETA", "PRIVATE_SYNTHETIC_TEXT"):
                    self.assertNotIn(private, serialized)

    def test_mixed_mcp_addressbook_understands_canonical_local_reply_address(self):
        book = collab.address_book(config.validate_config(mixed_config()))
        identity = call_tool(tool_params("whoami"), node="linux")["address"]
        self.assertEqual(book.resolve(identity), book.resolve("desk"))
        self.assertEqual(book.address(book.resolve(identity)), identity)
        self.assertEqual(book.resolve("remote").node, "wsl")
        with self.assertRaises(BridgeError):
            book.resolve("local:ses_ALPHA")


class LocalIdentityMcpTests(unittest.TestCase):
    def test_separate_stdio_clients_and_cli_share_configured_contract(self):
        with tempfile.TemporaryDirectory(prefix="local-stdio-") as root:
            environment = {**os.environ, "OPENCODE_CONNECT_LOCAL_STATE_HOME": root,
                           "OPENCODE_CONNECT_LOCAL_NODE": "wsl"}
            checkout = Path(__file__).resolve().parent

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
        with patch("local_mcp_server.LocalMailbox", side_effect=AssertionError("No queue access")), \
                patch("local_mcp_server.local_adapter", side_effect=AssertionError("No API access")), \
                patch("local_mcp_server.state_root", side_effect=AssertionError("No state access")):
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
            with patch("local_mcp_server.local_adapter", side_effect=AssertionError("No API access")):
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
            with patch("transports.WindowsWslSshTransport.run",
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
