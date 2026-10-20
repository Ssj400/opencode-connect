"""JSON command execution locally or through Windows SSH into WSL.

This infrastructure layer knows neither agent session IDs nor harness API routes.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Protocol

from config import private_directory, validate_ssh
from core import BridgeError


class CommandTransport(Protocol):
    def run(self, argv: list[str], *, timeout: float = 30, cwd: str | None = None,
            data: str | None = None) -> Any:
        ...


def decode_response(output):
    if not output.strip():
        return None
    try:
        return json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise BridgeError("The command did not return valid JSON.") from exc


def run_json(command, payload=None, timeout=30, cwd=None):
    try:
        result = subprocess.run(command, input=payload, capture_output=True,
                                timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("Request timed out; the message will not be retried.") from exc
    except OSError as exc:
        raise BridgeError(str(exc)) from exc
    if result.returncode:
        raise BridgeError(result.stderr.decode("utf-8", errors="replace").strip()
                          or f"Command exited with status {result.returncode}.")
    return decode_response(result.stdout)


def socket_directory():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = private_directory(Path(runtime))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        if not base.is_absolute():
            raise BridgeError("XDG_CACHE_HOME must be an absolute path.")
    return private_directory(base / "opencode-connect")


def socket_path(ssh_user, host):
    folder = socket_directory()
    digest = hashlib.sha256(f"{ssh_user}@{host}".encode()).hexdigest()[:16]
    path = folder / (digest + ".sock")
    if len(os.fsencode(path)) > 100:
        raise BridgeError("Socket path is too long; configure a shorter private XDG_RUNTIME_DIR.")
    if path.is_symlink():
        raise BridgeError("The SSH socket must not be a symlink.")
    return str(path)


class LocalCommandTransport:
    def run(self, argv, *, timeout=30, cwd=None, data=None):
        payload = data.encode("utf-8") if data is not None else None
        return run_json(argv, payload=payload, timeout=timeout, cwd=cwd)


class WindowsWslSshTransport:
    def __init__(self, options):
        self.options = validate_ssh({key: options[key] for key in (
            "host", "ssh_user", "wsl_user", "remote_opencode", "remote_directory")})

    def ssh(self, batch=True):
        control = socket_path(self.options["ssh_user"], self.options["host"])
        command = ["ssh", "-T", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                   "-o", f"ControlPath={control}"]
        if batch:
            command += ["-o", "BatchMode=yes"]
        return command + ["-l", self.options["ssh_user"], self.options["host"]]

    def connect(self):
        command = self.ssh(batch=False)
        command[1:1] = ["-M", "-N", "-f", "-o", "ControlPersist=600"]
        if subprocess.run(command).returncode:
            raise BridgeError("Could not open a reusable SSH connection.")

    def run(self, argv, *, timeout=30, cwd=None, data=None):
        # Variable arguments travel on stdin, never through the Windows shell.
        runner = (
            "import json,subprocess,sys;"
            "r=json.load(sys.stdin);"
            "p=subprocess.run(r['argv'],input=r.get('input'),cwd=r.get('cwd'),"
            "encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE);"
            "sys.stdout.buffer.write(p.stdout.encode('utf-8'));"
            "sys.stderr.buffer.write(p.stderr.encode('utf-8'));sys.exit(p.returncode)"
        )
        encoded = base64.b64encode(runner.encode()).decode()
        command = (f'wsl.exe -u {self.options["wsl_user"]} -- python3 -c '
                   f'"import base64;exec(base64.b64decode(\'{encoded}\'))"')
        request = json.dumps({"argv": argv, "cwd": cwd, "input": data}, ensure_ascii=False).encode()
        return run_json(self.ssh() + [command], request, timeout)
