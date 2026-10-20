#!/usr/bin/env python3
"""Linux -> Windows SSH -> WSL transport. Only connect/check; no automatic conversations."""
import argparse
import shutil
import sys
from types import SimpleNamespace

from config import BridgeError, load_config, validate_ssh
from opencode_adapter import OpenCodeCli, check
from transports import LocalCommandTransport, WindowsWslSshTransport


class Endpoint:
    """Compatibility facade composing one CLI client with a selected transport."""

    def __init__(self, args, remote=False):
        options = {key: getattr(args, key, None) for key in (
            "host", "ssh_user", "wsl_user", "remote_opencode", "remote_directory")}
        validate_ssh(options)
        self.args = args
        self.remote = remote
        self.transport = WindowsWslSshTransport(options) if remote else LocalCommandTransport()
        self.api = OpenCodeCli(self.transport, args.remote_opencode if remote else args.local_opencode)

    def ssh(self, batch=True):
        if not self.remote:
            raise BridgeError("The local transport does not use SSH.")
        return self.transport.ssh(batch)

    def connect(self):
        if not self.remote:
            raise BridgeError("The local transport does not need an SSH connection.")
        return self.transport.connect()

    def call(self, method, path, body=None, timeout=30):
        return self.api.call(method, path, body, timeout)

    def remote_exec(self, argv, timeout=30, cwd=None, data=None):
        if not self.remote:
            raise BridgeError("remote_exec requires a remote transport.")
        return self.transport.run(argv, timeout=timeout, cwd=cwd, data=data)


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
