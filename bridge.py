#!/usr/bin/env python3
"""Linux -> Windows SSH -> WSL transport. Only connect/check; no automatic conversations."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from config import BridgeError, load_config, private_directory, validate_ssh


def decode_response(output):
    if not output.strip():
        return None
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise BridgeError("The API did not return valid JSON.") from exc
    if isinstance(result, dict) and "_tag" in result:
        raise BridgeError(f"API error: {result.get('message', result['_tag'])}")
    return result


def run_json(command, payload=None, timeout=30):
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout)
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


class Endpoint:
    def __init__(self, args, remote=False):
        validate_ssh({key: getattr(args, key, None) for key in (
            "host", "ssh_user", "wsl_user", "remote_opencode", "remote_directory")})
        self.args = args
        self.remote = remote

    def ssh(self, batch=True):
        # Compute lazily: constructing a local endpoint has no filesystem effects.
        control = socket_path(self.args.ssh_user, self.args.host)
        command = ["ssh", "-T", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                   "-o", f"ControlPath={control}"]
        if batch:
            command += ["-o", "BatchMode=yes"]
        return command + ["-l", self.args.ssh_user, self.args.host]

    def connect(self):
        command = self.ssh(batch=False)
        command[1:1] = ["-M", "-N", "-f", "-o", "ControlPersist=600"]
        if subprocess.run(command).returncode:
            raise BridgeError("Could not open a reusable SSH connection.")

    def call(self, method, path, body=None, timeout=30):
        argv = [self.args.remote_opencode if self.remote else self.args.local_opencode,
                "api", method, path]
        if body is not None:
            argv += ["--data", json.dumps(body, ensure_ascii=False)]
        if not self.remote:
            return run_json(argv, timeout=timeout)
        return self.remote_exec(argv, timeout=timeout)

    def remote_exec(self, argv, timeout=30, cwd=None, data=None):
        # All variable payloads/paths travel as JSON on stdin, not through cmd.exe.
        runner = (
            "import json,subprocess,sys;"
            "r=json.load(sys.stdin);"
            "p=subprocess.run(r['argv'],input=r.get('input'),cwd=r.get('cwd'),"
            "encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE);"
            "sys.stdout.buffer.write(p.stdout.encode('utf-8'));"
            "sys.stderr.buffer.write(p.stderr.encode('utf-8'));sys.exit(p.returncode)"
        )
        encoded = base64.b64encode(runner.encode()).decode()
        remote_command = (f'wsl.exe -u {self.args.wsl_user} -- python3 -c '
                          f'"import base64;exec(base64.b64decode(\'{encoded}\'))"')
        return run_json(self.ssh() + [remote_command], json.dumps(
            {"argv": argv, "cwd": cwd, "input": data}, ensure_ascii=False).encode(), timeout)


def check(endpoints):
    for label, endpoint in endpoints:
        for path in ("/api/info", "/api/session?limit=1"):
            try:
                response = endpoint.call("get", path)
            except BridgeError as exc:
                raise BridgeError(f"{label}, GET {path}: {exc}") from exc
            if path == "/api/info":
                if not isinstance(response, dict) or not isinstance(response.get("version"), str):
                    raise BridgeError(f"{label}: unexpected /api/info response.")
                info = response
                if not info["version"].startswith("2."):
                    raise BridgeError(f"{label}: OpenCode V2 is required.")
            elif not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise BridgeError(f"{label}: unexpected session list response.")
        print(f"{label}: OpenCode {info['version']}, API accessible.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["connect", "check"])
    args = parser.parse_args()
    try:
        cfg = load_config()
        if cfg["side"] != "linux":
            raise BridgeError("Run connect/check from the Linux coordinator.")
        options = SimpleNamespace(**cfg["ssh"], local_opencode=shutil.which("opencode") or "opencode")
        local, remote = Endpoint(options), Endpoint(options, remote=True)
        if args.command == "connect":
            remote.connect()
            print("Reusable connection opened (expires after 10 idle minutes).")
        else:
            check([("Linux", local), ("Windows/WSL", remote)])
    except (BridgeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nClient stopped; no agents were cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
