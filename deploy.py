#!/usr/bin/env python3
"""Explicit managed deployment to WSL; never copies queues or installs MCP config."""
import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path
import sys

from bridge import BridgeError
from collab import ROOT, endpoint, settings

# This is also the complete export allowlist. No recursive copying.
FILES = (
    "AGENTS.md", "README.md", "SECURITY.md", ".gitignore", "routes.example.json",
    "config.py", "bridge.py", "collab.py", "mcp_server.py", "deploy.py", "audit_public.py",
    "test_bridge.py", "test_collab.py", "test_public.py",
    "docs/decisions.md", "docs/release-checklist.md",
    "examples/opencode.jsonc.example", "skills/opencode-connect/SKILL.md",
)


def bundle(root=ROOT, install_config=False):
    cfg = settings(root)
    if cfg["side"] != "linux":
        raise BridgeError("Despliega únicamente desde Linux.")
    files = {}
    for name in FILES:
        path = Path(root) / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise BridgeError("No desplegar enlaces.")
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
        raise ValueError("Raíz de despliegue inválida.")

    def reject_links(path):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("No sobrescribir enlaces ni atravesar directorios enlazados.")

    reject_links(root)
    allowed = set(FILES)
    if request.get("install_config") is True:
        allowed.add("routes.json")
    files = request["files"]
    if not isinstance(files, dict) or not files or not set(files) <= allowed:
        raise ValueError("Archivo fuera de la lista permitida.")
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".state"
    reject_links(state)
    state.mkdir(mode=0o700, exist_ok=True)
    info = state.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Directorio de estado inseguro; no se alteraron sus permisos.")
    manifest = state / "deployment.json"
    reject_links(manifest)
    old = json.loads(manifest.read_text()) if manifest.exists() else {}
    if not isinstance(old, dict):
        raise ValueError("Manifest inválido.")
    decoded = {name: base64.b64decode(data, validate=True) for name, data in files.items()}
    for directory in ("outgoing", "inbox"):
        path = root / directory
        reject_links(path)
        if path.exists() and not path.is_dir():
            raise ValueError("La bandeja existente no es un directorio.")
    # Complete preflight before replacing any managed file.
    for name, data in decoded.items():
        path = root / name
        reject_links(path)
        if path.exists():
            current = path.read_bytes()
            if current != data and hashlib.sha256(current).hexdigest() != old.get(name):
                raise ValueError("Archivo remoto cambiado/no administrado; no sobrescribir: " + name)
    if "routes.json" in decoded and json.loads(decoded["routes.json"])["side"] != "wsl":
        raise ValueError("La configuración remota debe declarar side=wsl.")

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-config", action="store_true",
                        help="Copiar explícitamente routes.json a WSL con side=wsl; nunca al repositorio.")
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
            raise BridgeError("Los hashes remotos no coinciden con el despliegue solicitado.")
        print(f"{len(expected)} archivos verificados en WSL. No se modificó la configuración MCP.")
    except (BridgeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
