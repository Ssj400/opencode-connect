"""Explicit managed deployment to WSL; never copies queues or installs MCP config."""
import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path
import sys

from opencode_connect.bridge.bridge import BridgeError
from opencode_connect.bridge.collab import ROOT, endpoint, settings

# This is also the complete export allowlist. No recursive copying.
# One entry per line, grouped by area, so parallel branches add files without conflicts.
# test_public derives its module, test, and command-line lists from this tuple.
FILES = (
    # Project documents, examples, and maintenance tools
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    ".gitignore",
    "routes.example.json",
    "docs/decisions.md",
    "docs/release-checklist.md",
    "docs/architecture.md",
    "examples/opencode.jsonc.example",
    "skills/opencode-connect/SKILL.md",
    "tools/__init__.py",
    "tools/audit_public.py",
    # Delivery core and adapters
    "opencode_connect/__init__.py",
    "opencode_connect/config.py",
    "opencode_connect/core.py",
    "opencode_connect/transports.py",
    "opencode_connect/opencode_adapter.py",
    "tests/__init__.py",
    "tests/test_core.py",
    "tests/test_adapters.py",
    "tests/test_public.py",
    # The cross-machine bridge
    "bin/bridge.py",
    "bin/collab.py",
    "bin/mcp_server.py",
    "bin/deploy.py",
    "opencode_connect/bridge/__init__.py",
    "opencode_connect/bridge/bridge.py",
    "opencode_connect/bridge/collab.py",
    "opencode_connect/bridge/mcp_server.py",
    "opencode_connect/bridge/deploy.py",
    "tests/bridge/__init__.py",
    "tests/bridge/test_bridge.py",
    "tests/bridge/test_collab.py",
    "tests/bridge/test_chat.py",
    # Local messaging
    "bin/local_bridge.py",
    "bin/local_mcp_server.py",
    "opencode_connect/local/__init__.py",
    "opencode_connect/local/mailbox.py",
    "opencode_connect/local/mcp_server.py",
    "tests/local/__init__.py",
    "tests/local/test_queue.py",
    "tests/local/test_mcp.py",
    "tests/local/test_courier_api.py",
    "tests/local/test_identity.py",
    "tests/local/test_identity_mcp.py",
    "docs/local-messaging.md",
    # Federated courier between people
    "bin/courier.py",
    "opencode_connect/courier/__init__.py",
    "opencode_connect/courier/settings.py",
    "opencode_connect/courier/app.py",
    "opencode_connect/courier/server.py",
    "opencode_connect/courier/worker.py",
    "opencode_connect/courier/service.py",
    "opencode_connect/courier/doctor.py",
    "opencode_connect/courier/cli.py",
    "tests/courier/__init__.py",
    "tests/courier/fakes.py",
    "tests/courier/test_app.py",
    "tests/courier/test_worker.py",
    "tests/courier/test_service.py",
    "tests/courier/test_cli.py",
    "tests/courier/test_queue.py",
    "tests/courier/test_doctor.py",
    "docs/multi-person.md",
    # Claude Code harness
    "bin/claude_hook.py",
    "bin/claude_mcp_server.py",
    "opencode_connect/claude/__init__.py",
    "opencode_connect/claude/hook.py",
    "opencode_connect/claude/mcp_server.py",
    "tests/claude/__init__.py",
    "tests/claude/test_claude.py",
    "examples/claude-settings.local.json.example",
    "docs/claude-code.md",
)


def bundle(root=ROOT, install_config=False):
    cfg = settings(root)
    if cfg["side"] != "linux":
        raise BridgeError("Deploy only from Linux.")
    files = {}
    for name in FILES:
        path = Path(root) / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise BridgeError("Do not deploy symlinks.")
        files[name] = base64.b64encode(path.read_bytes()).decode()
    if install_config:
        remote = {**cfg, "side": "wsl"}
        files["routes.json"] = base64.b64encode(
            (json.dumps(remote, ensure_ascii=False, indent=2) + "\n").encode()).decode()
    return cfg, {"root": cfg["ssh"]["remote_directory"], "files": files,
                 "install_config": install_config}


def apply_bundle(request):
    """Standalone receiver; runs through the validated SSH transport, not MCP."""
    import base64
    import hashlib
    import json
    import os
    from pathlib import Path
    import stat
    import tempfile

    os.umask(0o077)
    root = Path(request["root"])
    if not root.is_absolute() or root == Path("/") or ".." in root.parts:
        raise ValueError("Invalid deployment root.")

    def reject_links(path):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Do not overwrite symlinks or traverse linked directories.")

    reject_links(root)
    allowed = set(FILES)
    if request.get("install_config") is True:
        allowed.add("routes.json")
    files = request["files"]
    if not isinstance(files, dict) or not files or not set(files) <= allowed:
        raise ValueError("File is not in the allowlist.")
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".state"
    reject_links(state)
    state.mkdir(mode=0o700, exist_ok=True)
    info = state.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Insecure state directory; its permissions were not changed.")
    manifest = state / "deployment.json"
    reject_links(manifest)
    old = json.loads(manifest.read_text()) if manifest.exists() else {}
    if not isinstance(old, dict):
        raise ValueError("Invalid manifest.")
    decoded = {name: base64.b64decode(data, validate=True) for name, data in files.items()}
    for directory in ("outgoing", "inbox"):
        path = root / directory
        reject_links(path)
        if path.exists() and not path.is_dir():
            raise ValueError("The existing mailbox path is not a directory.")
    # Complete preflight before replacing any managed file.
    for name, data in decoded.items():
        path = root / name
        reject_links(path)
        if path.exists():
            current = path.read_bytes()
            if current != data and hashlib.sha256(current).hexdigest() != old.get(name):
                raise ValueError("Remote file changed or unmanaged; do not overwrite: " + name)
    if "routes.json" in decoded and json.loads(decoded["routes.json"])["side"] != "wsl":
        raise ValueError("Remote configuration must declare side=wsl.")

    def atomic_write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    hashes = {}
    for name, data in decoded.items():
        path = root / name
        atomic_write(path, data)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Preserve tracking of explicitly installed config when not sending it again.
    atomic_write(manifest, (json.dumps({**old, **hashes}, indent=2) + "\n").encode())
    for directory in ("outgoing", "inbox"):
        path = root / directory
        reject_links(path)
        path.mkdir(mode=0o700, exist_ok=True)
    return hashes


def inspect_installation(request):
    """Standalone read-only probe of a deployed tree for diagnostics; changes nothing."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import sqlite3
    import stat
    import sys

    root = Path(request["root"])
    report = {"python": list(sys.version_info[:3]), "installed": root.is_dir() and not root.is_symlink(),
              "files": {}, "manifest": None, "state_private": None, "config": None,
              "queue": None, "queue_error": None}
    if not report["installed"]:
        return report
    for name in request["files"]:
        path = root / name
        report["files"][name] = (hashlib.sha256(path.read_bytes()).hexdigest()
                                 if path.is_file() and not path.is_symlink() else None)
    state = root / ".state"
    if state.is_dir() and not state.is_symlink():
        info = state.stat()
        report["state_private"] = info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077
        manifest = state / "deployment.json"
        if manifest.is_file() and not manifest.is_symlink():
            try:
                report["manifest"] = json.loads(manifest.read_text())
            except ValueError:
                pass
        database = state / "mail.sqlite3"
        report["queue"] = {}
        if database.is_file() and not database.is_symlink():
            try:
                connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                try:
                    report["queue"] = dict(connection.execute(
                        "SELECT state, count(*) FROM messages GROUP BY state").fetchall())
                finally:
                    connection.close()
            except sqlite3.Error as exc:
                report["queue"], report["queue_error"] = None, str(exc)
    routes = root / "routes.json"
    if routes.is_file() and not routes.is_symlink():
        try:
            cfg = json.loads(routes.read_text(encoding="utf-8"))
            report["config"] = {"side": cfg.get("side"), "peers": cfg.get("peers")}
        except (ValueError, AttributeError):
            report["config"] = {"invalid": True}
    return report


def inspection_script():
    return ("import json,sys\n" + inspect.getsource(inspect_installation)
            + "\nprint(json.dumps(inspect_installation(json.load(sys.stdin))))\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-config", action="store_true",
                         help="Explicitly copy routes.json to WSL with side=wsl, never into the repository.")
    args = parser.parse_args()
    try:
        cfg, request = bundle(install_config=args.install_config)
        script = ("import json,sys\nFILES = " + repr(FILES) + "\n" + inspect.getsource(apply_bundle)
                  + "\nprint(json.dumps(apply_bundle(json.load(sys.stdin))))\n")
        result = endpoint(cfg, "wsl").remote_exec(
            ["python3", "-c", script], data=json.dumps(request), timeout=60)
        expected = {name: hashlib.sha256(base64.b64decode(data)).hexdigest()
                    for name, data in request["files"].items()}
        if result != expected:
            raise BridgeError("Remote hashes do not match the requested deployment.")
        print(f"{len(expected)} files verified in WSL. MCP configuration was not changed.")
    except (BridgeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
