"""Required local configuration; no personal defaults or network discovery."""
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from core import BridgeError

ROOT = Path(__file__).resolve().parent
# Address namespaces that are not machines: Claude Code sessions and remote people.
RESERVED_NODES = frozenset(("claude", "person"))


def local_node(value=None):
    """Explicit session namespace, independent of OS and command transport."""
    if value is None:
        value = os.environ.get("OPENCODE_CONNECT_LOCAL_NODE", "local")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise BridgeError("OPENCODE_CONNECT_LOCAL_NODE must be a simple node identifier.")
    if value.lower() in RESERVED_NODES:
        raise BridgeError(f"The node name {value!r} is reserved for another address namespace.")
    return value


def clean_text(value, field, maximum=4096):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BridgeError(f"Invalid field: {field}.")
    return value


def absolute_path(value, field):
    clean_text(value, field)
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) == "/" or ".." in path.parts or value.startswith("//"):
        raise BridgeError(f"{field} must be an absolute Linux path without '..'.")
    return value


def validate_ssh(ssh):
    fields = {"host", "ssh_user", "wsl_user", "remote_opencode", "remote_directory"}
    if not isinstance(ssh, dict) or set(ssh) != fields:
        raise BridgeError("ssh requires host, ssh_user, wsl_user, remote_opencode, and remote_directory.")
    host = clean_text(ssh["host"], "host", 253)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise BridgeError("Invalid host: use an IP address or DNS name, not SSH options.")
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
               for label in host.split('.')):
            raise BridgeError("Invalid DNS name.")
    user = clean_text(ssh["ssh_user"], "ssh_user", 128)
    if not re.fullmatch(r"[\w][\w .@\\-]*", user):
        raise BridgeError("Invalid SSH username.")
    if not isinstance(ssh["wsl_user"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", ssh["wsl_user"]):
        raise BridgeError("Invalid WSL username.")
    for key in ("remote_opencode", "remote_directory"):
        absolute_path(ssh[key], key)
    return ssh


def validate_config(cfg):
    if not isinstance(cfg, dict) or set(cfg) != {"side", "peers", "ssh"}:
        raise BridgeError("Configuration requires exactly side, peers, and ssh.")
    if cfg["side"] not in ("linux", "wsl"):
        raise BridgeError("side must be linux or wsl.")
    peers = cfg["peers"]
    if not isinstance(peers, dict):
        raise BridgeError("peers must be a mapping of optional session shortcuts.")
    seen = set()
    for alias, peer in peers.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", alias):
            raise BridgeError("Invalid alias.")
        if not isinstance(peer, dict) or set(peer) != {"side", "sessionID"}:
            raise BridgeError("Each peer requires side and sessionID.")
        if peer["side"] not in ("linux", "wsl"):
            raise BridgeError("Invalid peer side.")
        sid = peer["sessionID"]
        if not isinstance(sid, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,128}", sid):
            raise BridgeError("Invalid sessionID.")
        if sid.startswith("ses_REPLACE_"):
            raise BridgeError("Replace session placeholders with authorized sessions before using the bridge.")
        identity = (peer["side"], sid)
        if identity in seen:
            raise BridgeError("A session cannot have duplicate aliases on the same side.")
        seen.add(identity)
    validate_ssh(cfg["ssh"])
    return cfg


def load_config(root=ROOT):
    path = Path(root) / "routes.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError("Missing routes.json: copy routes.example.json and configure both machines.") from exc
    except (ValueError, OSError) as exc:
        raise BridgeError("Could not read routes.json as UTF-8 JSON.") from exc
    return validate_config(cfg)


def private_directory(path):
    """Create our directory, never chmod or take over an existing insecure one."""
    path = Path(path)
    if not path.is_absolute():
        raise BridgeError("The private directory path must be absolute.")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise BridgeError("Symlinks are not allowed in the private directory path.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BridgeError("Insecure private directory: current user ownership and 0700 permissions are required.")
    return path
