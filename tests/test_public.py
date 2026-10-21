import ast
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import uuid

from opencode_connect import config
from opencode_connect.bridge import collab, deploy, mcp_server
from tests.bridge.test_bridge import fixture
from tools import audit_public

ROOT = Path(__file__).resolve().parents[1]
# A project rule: split a file before it grows past this many lines.
MAX_LINES = 600


def module_name(path):
    return path[:-3].removesuffix("/__init__").replace("/", ".")


def exported_modules():
    """Importable non-test modules in the export allowlist, as (module, path) pairs."""
    return [(module_name(name), name) for name in deploy.FILES
            if name.endswith(".py") and name.startswith(("opencode_connect/", "tools/"))]


def exported_test_modules():
    """Every exported test module except this one, which audits the export itself."""
    return [module_name(name) for name in deploy.FILES
            if name.startswith("tests/") and Path(name).name.startswith("test_") and name != "tests/test_public.py"]


def command_line_scripts():
    """Exported launchers whose module has an argparse entry point, so `--help` must work."""
    scripts = []
    for name in deploy.FILES:
        if not name.startswith("bin/"):
            continue
        launcher = ast.parse((ROOT / name).read_text())
        guarded = any(isinstance(node, ast.If) and "__main__" in ast.unparse(node.test) for node in launcher.body)
        (target,) = [node.module for node in launcher.body
                     if isinstance(node, ast.ImportFrom) and node.module.startswith("opencode_connect.")]
        module = ast.parse((ROOT / (target.replace(".", "/") + ".py")).read_text())
        parses = any(isinstance(node, ast.Attribute) and node.attr == "ArgumentParser" for node in ast.walk(module))
        if guarded and parses:
            scripts.append(name)
    return scripts


class PublicTests(unittest.TestCase):
    def test_export_has_no_private_data_or_extra_files(self):
        self.assertEqual(audit_public.audit(ROOT), [])

    def test_mit_license_is_included_in_export(self):
        self.assertIn("LICENSE", deploy.FILES)
        license_text = (ROOT / "LICENSE").read_text()
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 Ssj400", license_text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)

    def test_root_git_metadata_is_not_exported_content(self):
        with TemporaryDirectory(prefix="audit-repo-") as folder:
            root = Path(folder)
            for name in deploy.FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("local repository metadata")
            self.assertEqual(audit_public.audit(root), [])
            (root / "unexpected").mkdir()
            (root / "unexpected" / ".git").mkdir()
            self.assertTrue(audit_public.audit(root))

    def test_privacy_rules_with_generated_synthetic_values(self):
        # Construct synthetic samples; do not embed operational identifiers/tokens.
        token = "aB3" * 11
        samples = ["/home/" + "fictional-person" + "/project",
                   ".".join(("10", "20", "30", "40")), ".".join(("100", "101", "102", "103")),
                   "[" + ":".join(("fd12", "3456", "789a")) + "::1]:8765",
                   ":".join(("2606", "4700")) + "::1111", "fe80" + "::1%eth0",
                   "laptop.example" + ".ts.net",
                   "ses_" + "A" * 24, "msg_" + "a" * 32, "claude:" + str(uuid.UUID(int=1)),
                   "SHA256:" + "A" * 43, "ghp_" + "a" * 36, "github_pat_" + "a" * 30,
                   "tskey-" + "auth-" + "a" * 20, "xoxb-" + "1" * 12, "AKIA" + "A" * 16,
                   "eyJ" + "a" * 10 + ".eyJ" + "a" * 10 + ".signature",
                   "Authorization: Bearer " + token[:20], json.dumps({"token": token[:20]}),
                   "card " + token, "-----BEGIN " + "PRIVATE KEY-----"]
        for index, sample in enumerate(samples):
            with self.subTest(sample=index):
                self.assertTrue(audit_public.findings(sample))
        placeholders = ["192.0.2.10 ses_REPLACE_LINUX /home/example/project",
                        "2001:db8::10 [::1]:8765 :: items[1::2] text[::-1] 10:22:33",
                        "unique local fc00::/7, link-local fe80::/10, global 2000::/3",
                        "claude:00000000-0000-0000-0000-000000000000 claude:<uuid>",
                        'headers={"Authorization": "Bearer " + card["token"]}',
                        'f"Bearer {token}" {"token": "REPLACE_WITH_CARD_TOKEN"}',
                        hashlib.sha256(b"export").hexdigest(), "ExampleContactCardTokenPlaceholder",
                        "EXAMPLE_TOKEN_0123456789_ABCDEFGHIJ",
                        "test_untrust_rollback_restores_token_card_and_pending_2"]
        for index, sample in enumerate(placeholders):
            with self.subTest(placeholder=index):
                self.assertEqual(audit_public.findings(sample), [])

    def test_private_deny_list_is_read_at_runtime_and_never_echoed(self):
        term = "fictional" + "-mesh"
        with TemporaryDirectory(prefix="audit-terms-") as folder:
            path = Path(folder) / "terms.txt"
            path.write_text("# private names, one per line\n\n  " + term.upper() + "  \n")
            with patch.dict(os.environ, {audit_public.TERMS_VARIABLE: str(path)}):
                terms = audit_public.private_terms()
            self.assertEqual(terms, ((3, term),))
            issues = audit_public.findings("listen on " + term + "0", terms)
            self.assertEqual(issues, ["Term from line 3 of the private deny-list"])
            self.assertNotIn(term, " ".join(issues))
            self.assertEqual(audit_public.findings("listen on 192.0.2.10", terms), [])
            with patch.dict(os.environ, {audit_public.TERMS_VARIABLE: str(path) + ".missing"}):
                with self.assertRaises(OSError):
                    audit_public.private_terms()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(audit_public.main(), 2)
        with patch.dict(os.environ, {audit_public.TERMS_VARIABLE: ""}):
            self.assertEqual(audit_public.private_terms(), ())

    def test_no_exported_file_exceeds_the_line_limit(self):
        for name in deploy.FILES:
            with self.subTest(file=name):
                self.assertLessEqual(len((ROOT / name).read_text(encoding="utf-8").splitlines()), MAX_LINES)

    def test_no_agent_cancellation_or_auto_chat(self):
        for _, path in exported_modules():
            tree = ast.parse((ROOT / path).read_text())
            strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            self.assertFalse(any("/interrupt" in value for value in strings))
            self.assertFalse(any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "chat"
                                 for n in ast.walk(tree)))

    def test_example_does_not_require_session_registration(self):
        example = json.loads((ROOT / "routes.example.json").read_text())
        self.assertEqual(config.validate_config(example)["peers"], {})

    def test_skill_links_and_metadata(self):
        skill = ROOT / "skills/opencode-connect/SKILL.md"
        text = skill.read_text()
        self.assertTrue(text.startswith("---\nname: opencode-connect\nlicense: MIT\ndescription:"))
        self.assertIn("Coordinate remote OpenCode agents", text)
        self.assertIn("user-authorized", text)
        for relative in ("../../README.md", "../../SECURITY.md"):
            self.assertTrue((skill.parent / relative).is_file())
        self.assertIn("Do not install anything automatically", text)

    def test_english_project_policy_and_documentation(self):
        instructions = (ROOT / "AGENTS.md").read_text()
        self.assertIn("Use English for all project documentation", instructions)
        self.assertIn("Preserve Unicode coverage", instructions)
        headings = {
            "SECURITY.md": "# Security and limitations",
            "docs/decisions.md": "# Architecture decisions",
            "docs/release-checklist.md": "# Publication and stable-release checklist",
        }
        for name, heading in headings.items():
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).read_text().startswith(heading))
        self.assertIn("Directed communication", mcp_server.INSTRUCTIONS)
        with TemporaryDirectory(prefix="english-mcp-") as folder:
            result = mcp_server.handle({"id": 1, "method": "tools/call", "params": {
                "name": "send", "arguments": {"to": "wsl", "text": "hello"}}}, Path(folder))
        self.assertTrue(result["result"]["isError"])
        self.assertIn("Missing ai.opencode/sessionID", result["result"]["content"][0]["text"])

    def test_cli_help_is_in_english(self):
        cases = [("bin/bridge.py", [], "transport"),
                 ("bin/collab.py", ["send"], "Local session alias or node:ses_ID"),
                 ("bin/deploy.py", [], "Explicitly copy routes.json"),
                 ("bin/local_bridge.py", [], "Local OpenCode text queues"),
                 ("bin/courier.py", [], "consent-gated")]
        for script, arguments, expected in cases:
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, "-B", str(ROOT / script),
                                         *arguments, "--help"], cwd=ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_detached_export_imports_and_cli_without_personal_files(self):
        # Isolated search path; exported files only. No ancestor config/import fallback.
        with TemporaryDirectory(prefix="export-") as folder:
            root = Path(folder)
            for name in deploy.FILES:
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            code = '''import importlib,json,sys,pathlib,unittest
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root))
spec=json.loads(sys.argv[2])
for name, path in spec["modules"]:
    assert pathlib.Path(importlib.import_module(name).__file__) == root / path, name
from opencode_connect import config
from opencode_connect.bridge import mcp_server
from tools import audit_public
assert audit_public.audit(root) == []
try: config.load_config(root)
except config.BridgeError: pass
else: raise AssertionError("unexpected configuration")
assert mcp_server.handle({"id":1,"method":"initialize"})["result"]["serverInfo"]["name"] == "opencode-connect"
suite=unittest.defaultTestLoader.loadTestsFromNames(spec["tests"])
result=unittest.TextTestRunner(verbosity=0).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
'''
            spec = json.dumps({"modules": exported_modules(), "tests": exported_test_modules()})
            result = subprocess.run([sys.executable, "-B", "-I", "-c", code, folder, spec],
                                    cwd=folder, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for script in command_line_scripts():
                result = subprocess.run([sys.executable, "-B", str(root / script), "--help"],
                                        cwd=folder, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_session_creation_is_explicit_ask_without_prompt(self):
        from types import SimpleNamespace
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / "routes.json").write_text(json.dumps(fixture()))
            with patch("opencode_connect.bridge.collab.endpoint") as endpoint:
                endpoint.return_value.call.return_value = {"data": {"id": "ses_new"}}
                collab.create_session(SimpleNamespace(side="wsl", title="Example task"), root)
                args = endpoint.return_value.call.call_args.args
                self.assertEqual(args[:2], ("post", "/api/session"))
                self.assertEqual(args[2]["permissions"][0]["effect"], "ask")
                self.assertEqual(endpoint.return_value.call.call_count, 1)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        old = os.umask(0o077); os.umask(old)
        self.addCleanup(os.umask, old)
        self.tmp = TemporaryDirectory(prefix="deploy-"); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "remote"

    def request(self, files, install=False):
        return {"root": str(self.root), "install_config": install,
                "files": {name: base64.b64encode(data).decode() for name, data in files.items()}}

    def test_hashes_and_preserve_runtime(self):
        request = self.request({"README.md": b"first"})
        result = deploy.apply_bundle(request)
        self.assertEqual(result["README.md"], hashlib.sha256(b"first").hexdigest())
        state = self.root / ".state" / "keep.txt"; state.write_bytes(b"private")
        deploy.apply_bundle(self.request({"README.md": b"second"}))
        self.assertEqual(state.read_bytes(), b"private")
        self.assertEqual((self.root / "README.md").read_bytes(), b"second")

    def test_conflict_preflight_no_partial_overwrite(self):
        deploy.apply_bundle(self.request({"README.md": b"original", "SECURITY.md": b"security"}))
        (self.root / "SECURITY.md").write_bytes(b"user change")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            deploy.apply_bundle(self.request({"README.md": b"replacement", "SECURITY.md": b"replacement"}))
        self.assertEqual((self.root / "README.md").read_bytes(), b"original")
        self.assertEqual((self.root / "SECURITY.md").read_bytes(), b"user change")

    def test_reject_unmanaged_file_and_symlink(self):
        self.root.mkdir(); (self.root / "README.md").write_bytes(b"user content")
        with self.assertRaises(ValueError):
            deploy.apply_bundle(self.request({"README.md": b"new content"}))
        (self.root / "SECURITY.md").symlink_to(self.root / "README.md")
        with self.assertRaises(ValueError):
            deploy.apply_bundle(self.request({"SECURITY.md": b"new content"}))

    def test_reject_state_paths_and_unrequested_config(self):
        for name in ("../escape", ".state/mail.sqlite3", "routes.json", "inbox/example.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                deploy.apply_bundle(self.request({name: b"data"}))

    def test_bundle_excludes_config_unless_explicit(self):
        with patch("opencode_connect.bridge.deploy.settings", return_value=fixture()):
            cfg, default = deploy.bundle(ROOT)
            self.assertNotIn("routes.json", default["files"])
            self.assertEqual(set(default["files"]), set(deploy.FILES))
            cfg, explicit = deploy.bundle(ROOT, install_config=True)
            remote = json.loads(base64.b64decode(explicit["files"]["routes.json"]))
            self.assertEqual(remote["side"], "wsl")
            self.assertEqual(cfg["side"], "linux")
            self.assertEqual(remote["peers"], cfg["peers"])

    def test_explicit_config_manifest_survives_code_deploy(self):
        cfg = fixture(); cfg["side"] = "wsl"
        data = json.dumps(cfg).encode()
        deploy.apply_bundle(self.request({"routes.json": data}, install=True))
        deploy.apply_bundle(self.request({"README.md": b"new code"}))
        manifest = json.loads((self.root / ".state/deployment.json").read_text())
        self.assertIn("routes.json", manifest)
        self.assertEqual((self.root / "routes.json").read_bytes(), data)

    def test_runtime_symlink_rejected_before_code_changes(self):
        deploy.apply_bundle(self.request({"README.md": b"original"}))
        (self.root / "inbox").rmdir()
        (self.root / "inbox").symlink_to(self.root / "outgoing", target_is_directory=True)
        with self.assertRaises(ValueError):
            deploy.apply_bundle(self.request({"README.md": b"replacement"}))
        self.assertEqual((self.root / "README.md").read_bytes(), b"original")

    def test_generated_receiver_is_standalone(self):
        # Exercise the exact receiver program locally, never the SSH transport.
        cfg = fixture(); request = self.request({"README.md": b"standalone"})
        expected = {"README.md": hashlib.sha256(b"standalone").hexdigest()}
        with patch("sys.argv", ["deploy.py"]), \
                patch("opencode_connect.bridge.deploy.bundle", return_value=(cfg, request)), \
                patch("opencode_connect.bridge.deploy.endpoint") as endpoint, contextlib.redirect_stdout(io.StringIO()):
            endpoint.return_value.remote_exec.return_value = expected
            self.assertEqual(deploy.main(), 0)
            script = endpoint.return_value.remote_exec.call_args.args[0][2]
        result = subprocess.run([sys.executable, "-B", "-I", "-c", script],
                                input=json.dumps(request), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_deploy_main_uses_validated_transport(self):
        cfg = fixture(); request = self.request({"README.md": b"data"})
        result = {"README.md": hashlib.sha256(b"data").hexdigest()}
        with patch("sys.argv", ["deploy.py"]), \
                patch("opencode_connect.bridge.deploy.bundle", return_value=(cfg, request)), \
                patch("opencode_connect.bridge.deploy.endpoint") as endpoint, contextlib.redirect_stdout(io.StringIO()):
            endpoint.return_value.remote_exec.return_value = result
            self.assertEqual(deploy.main(), 0)
            endpoint.assert_called_once_with(cfg, "wsl")
            args = endpoint.return_value.remote_exec.call_args
            self.assertEqual(args.args[0][:2], ["python3", "-c"])
            self.assertEqual(json.loads(args.kwargs["data"]), request)


if __name__ == "__main__":
    unittest.main()
