"""Courier identity, contact cards, and the private configuration file."""

import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import string
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit

from opencode_connect.config import private_directory
from opencode_connect.core import BridgeError

SOCKET_TIMEOUT = 3
PERSON = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")


def person(value):
    if not isinstance(value, str) or not PERSON.fullmatch(value):
        raise BridgeError("Person names must be lowercase ASCII identifiers (1-64 characters).")
    return value


def clean_display(value):
    """Remove terminal controls, escape sequences, and invisible formatting controls."""
    # Remove complete ANSI CSI/OSC sequences, not just their escape introducers.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if not unicodedata.category(c).startswith("C"))


def require_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BridgeError("This command requires an interactive terminal on stdin and stdout.")


def interactive_confirmation(action):
    require_terminal()
    code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(5))
    print(f"{clean_display(action)}. Type {code} to confirm:", flush=True)
    if input().strip() != code:
        raise BridgeError("Confirmation did not match; nothing was changed.")


def listen_address(value, allow_wildcard=False):
    try:
        host, port = value.rsplit(":", 1)
        address = ipaddress.ip_address(host.strip("[]"))
        port = int(port)
        if not 0 <= port <= 65535:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise BridgeError("Listen must be an IP address and port (use [IPv6]:PORT).") from exc
    if address.is_unspecified and not allow_wildcard:
        raise BridgeError("Wildcard listening requires --allow-wildcard and explicit network authorization.")
    if address.is_multicast:
        raise BridgeError("Multicast listen addresses are not supported.")
    return str(address), port


def contact_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.port == 0
                or url.username is not None or url.password is not None
                or url.path not in {"", "/"} or url.query or url.fragment
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise BridgeError("A contact URL must be an HTTP(S) origin without credentials or a path.") from exc
    return value.rstrip("/")


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"person", "listen", "url", "tokens", "contacts"}:
        raise BridgeError("Courier configuration requires person, listen, url, tokens, and contacts.")
    person(config["person"])
    listen_address(config["listen"], allow_wildcard=True)
    contact_url(config["url"])
    if not isinstance(config["tokens"], dict) or not isinstance(config["contacts"], dict):
        raise BridgeError("Courier tokens and contacts must be mappings.")
    for name, digest in config["tokens"].items():
        person(name)
        if name == config["person"] or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BridgeError("Invalid issued token digest.")
    for name, card in config["contacts"].items():
        person(name)
        validate_card(card)
        if name == config["person"] or card["person"] != name:
            raise BridgeError("Contact card identity does not match its name.")
    return config


def validate_card(card):
    if not isinstance(card, dict) or set(card) != {"person", "url", "token"}:
        raise BridgeError("A contact card requires person, url, and token.")
    person(card["person"])
    contact_url(card["url"])
    if not isinstance(card["token"], str) or not TOKEN.fullmatch(card["token"]):
        raise BridgeError("Invalid contact card token.")
    return card


def config_home():
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not base.is_absolute():
        raise BridgeError("XDG_CONFIG_HOME must be absolute.")
    return base


def default_config_path():
    return config_home() / "opencode-connect/courier.json"


def read_config(path):
    path = Path(path)
    try:
        check_private_directory(path.parent)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise BridgeError("Courier configuration must be an owned regular file with mode 0600.")
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError) as exc:
        raise BridgeError("Could not read the private courier configuration.") from exc


def check_private_directory(path):
    """Validate an existing directory without creating or repairing anything."""
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise BridgeError("Courier state paths must be absolute and must not traverse symlinks.")
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BridgeError("Courier directories require current-user ownership and private permissions.")


def write_config(path, config):
    validate_config(config)
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise BridgeError("Courier configuration must not be a symlink.")
    if path.exists():
        read_config(path)
    fd, temporary = tempfile.mkstemp(prefix=".courier-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
