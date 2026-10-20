"""JSON command execution locally or through Windows SSH into WSL.

This infrastructure layer knows neither agent session IDs nor harness API routes.
"""

import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any, NamedTuple, Protocol

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


def without_ssh_advisories(error):
    """Remove OpenSSH advisory lines without interpreting application errors."""
    return " ".join(line.strip() for line in str(error).splitlines()
                    if line.strip() and not line.lstrip().startswith("**"))


class CommandError(BridgeError):
    """A failed command with its original output available to protocol owners."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = without_ssh_advisories(stderr.decode("utf-8", errors="replace"))
        super().__init__(message or stdout.decode("utf-8", errors="replace").strip()[:300]
                         or f"Command exited with status {returncode}.")


def run_json(command, payload=None, timeout=30, cwd=None):
    try:
        result = subprocess.run(command, input=payload, capture_output=True,
                                timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("Request timed out; the message will not be retried.") from exc
    except OSError as exc:
        raise BridgeError(str(exc)) from exc
    if result.returncode:
        raise CommandError(result.returncode, result.stdout, result.stderr)
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


class Probe(NamedTuple):
    """One TCP connection attempt. `reason` is stable for callers: open, refused,
    unreachable, timeout, unresolved, or error."""
    ok: bool
    reason: str
    detail: str


def tcp_probe(host, port, timeout=5):
    """Open and close one TCP connection, reading an optional banner; sends nothing."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            try:
                banner = connection.recv(256).decode("utf-8", "replace").strip()
            except OSError:
                banner = ""
        return Probe(True, "open", banner)
    except socket.gaierror as exc:
        return Probe(False, "unresolved", exc.strerror or str(exc))
    except TimeoutError:
        return Probe(False, "timeout", f"no answer within {timeout} s")
    except OSError as exc:
        reason = {errno.ECONNREFUSED: "refused", errno.EHOSTUNREACH: "unreachable",
                  errno.ENETUNREACH: "unreachable"}.get(exc.errno, "error")
        return Probe(False, reason, exc.strerror or str(exc))


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

    def destination(self):
        """Effective hostname and port after the user's OpenSSH configuration; no network."""
        command = ["ssh", "-G", "-l", self.options["ssh_user"], self.options["host"]]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"Could not read the OpenSSH client configuration: {exc}") from exc
        if result.returncode:
            raise BridgeError(result.stderr.strip() or "ssh -G failed.")
        values = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            values.setdefault(key.lower(), value.strip())
        try:
            return values.get("hostname") or self.options["host"], int(values.get("port", "22"))
        except ValueError as exc:
            raise BridgeError("ssh -G returned an invalid port.") from exc

    def master_alive(self):
        """Ask the local control socket whether the reusable connection is alive.

        No authentication and no new connection: `-O check` only talks to the socket.
        """
        control = socket_path(self.options["ssh_user"], self.options["host"])
        command = ["ssh", "-O", "check", "-o", f"ControlPath={control}",
                   "-l", self.options["ssh_user"], self.options["host"]]
        try:
            return subprocess.run(command, capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

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
