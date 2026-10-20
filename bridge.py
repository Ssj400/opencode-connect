#!/usr/bin/env python3
"""Transporte Linux -> SSH Windows -> WSL. Solo connect y check; no debate automático."""
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
        raise BridgeError("La API no devolvió JSON válido.") from exc
    if isinstance(result, dict) and "_tag" in result:
        raise BridgeError(f"Error de API: {result.get('message', result['_tag'])}")
    return result


def run_json(command, payload=None, timeout=30):
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("Tiempo agotado; no se reintentará el mensaje.") from exc
    except OSError as exc:
        raise BridgeError(str(exc)) from exc
    if result.returncode:
        raise BridgeError(result.stderr.decode("utf-8", errors="replace").strip()
                          or f"El comando terminó con código {result.returncode}.")
    return decode_response(result.stdout)


def socket_directory():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = private_directory(Path(runtime))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        if not base.is_absolute():
            raise BridgeError("XDG_CACHE_HOME debe ser absoluto.")
    return private_directory(base / "opencode-connect")


def socket_path(ssh_user, host):
    folder = socket_directory()
    digest = hashlib.sha256(f"{ssh_user}@{host}".encode()).hexdigest()[:16]
    path = folder / (digest + ".sock")
    if len(os.fsencode(path)) > 100:
        raise BridgeError("Ruta del socket demasiado larga; configura un XDG_RUNTIME_DIR privado más corto.")
    if path.is_symlink():
        raise BridgeError("El socket SSH no puede ser un enlace.")
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
            raise BridgeError("No se pudo abrir la conexión SSH reutilizable.")

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
                    raise BridgeError(f"{label}: respuesta de /api/info inesperada.")
                info = response
                if not info["version"].startswith("2."):
                    raise BridgeError(f"{label}: se requiere OpenCode V2.")
            elif not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise BridgeError(f"{label}: respuesta de sesiones inesperada.")
        print(f"{label}: OpenCode {info['version']}, API accesible.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["connect", "check"])
    args = parser.parse_args()
    try:
        cfg = load_config()
        if cfg["side"] != "linux":
            raise BridgeError("connect/check se ejecutan desde el coordinador Linux.")
        options = SimpleNamespace(**cfg["ssh"], local_opencode=shutil.which("opencode") or "opencode")
        local, remote = Endpoint(options), Endpoint(options, remote=True)
        if args.command == "connect":
            remote.connect()
            print("Conexión reutilizable abierta (expira tras 10 minutos sin uso).")
        else:
            check([("Linux", local), ("Windows/WSL", remote)])
    except (BridgeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCliente detenido; no se cancelan agentes.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
