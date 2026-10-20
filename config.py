"""Required local configuration; no personal defaults or network discovery."""
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

ROOT = Path(__file__).resolve().parent


class BridgeError(Exception):
    pass


def clean_text(value, field, maximum=4096):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BridgeError(f"Campo inválido: {field}.")
    return value


def absolute_path(value, field):
    clean_text(value, field)
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) == "/" or ".." in path.parts or value.startswith("//"):
        raise BridgeError(f"{field} debe ser una ruta Linux absoluta, sin '..'.")
    return value


def validate_ssh(ssh):
    fields = {"host", "ssh_user", "wsl_user", "remote_opencode", "remote_directory"}
    if not isinstance(ssh, dict) or set(ssh) != fields:
        raise BridgeError("ssh requiere host, ssh_user, wsl_user, remote_opencode y remote_directory.")
    host = clean_text(ssh["host"], "host", 253)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise BridgeError("Host inválido: usa una IP o nombre DNS, no opciones SSH.")
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
               for label in host.split('.')):
            raise BridgeError("Nombre DNS inválido.")
    user = clean_text(ssh["ssh_user"], "ssh_user", 128)
    if not re.fullmatch(r"[\w][\w .@\\-]*", user):
        raise BridgeError("Usuario SSH inválido.")
    if not isinstance(ssh["wsl_user"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", ssh["wsl_user"]):
        raise BridgeError("Usuario WSL inválido.")
    for key in ("remote_opencode", "remote_directory"):
        absolute_path(ssh[key], key)
    return ssh


def validate_config(cfg):
    if not isinstance(cfg, dict) or set(cfg) != {"side", "peers", "ssh"}:
        raise BridgeError("La configuración requiere exactamente side, peers y ssh.")
    if cfg["side"] not in ("linux", "wsl"):
        raise BridgeError("side debe ser linux o wsl.")
    peers = cfg["peers"]
    if not isinstance(peers, dict) or not peers:
        raise BridgeError("peers debe contener sesiones explícitamente registradas.")
    seen = set()
    sides = set()
    for alias, peer in peers.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", alias):
            raise BridgeError("Alias inválido.")
        if not isinstance(peer, dict) or set(peer) != {"side", "sessionID"}:
            raise BridgeError("Cada peer requiere side y sessionID.")
        if peer["side"] not in ("linux", "wsl"):
            raise BridgeError("Equipo del peer inválido.")
        sid = peer["sessionID"]
        if not isinstance(sid, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,128}", sid):
            raise BridgeError("sessionID inválido.")
        if sid.startswith("ses_REPLACE_"):
            raise BridgeError("Sustituye los placeholders por sesiones autorizadas antes de usar el puente.")
        identity = (peer["side"], sid)
        if identity in seen:
            raise BridgeError("Una sesión no puede tener aliases duplicados en el mismo equipo.")
        seen.add(identity)
        sides.add(peer["side"])
    if sides != {"linux", "wsl"}:
        raise BridgeError("Registra al menos una sesión de cada equipo.")
    validate_ssh(cfg["ssh"])
    return cfg


def load_config(root=ROOT):
    path = Path(root) / "routes.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError("Falta routes.json: copia routes.example.json y configura ambos equipos.") from exc
    except (ValueError, OSError) as exc:
        raise BridgeError("No se pudo leer routes.json como JSON UTF-8.") from exc
    return validate_config(cfg)


def private_directory(path):
    """Create our directory, never chmod or take over an existing insecure one."""
    path = Path(path)
    if not path.is_absolute():
        raise BridgeError("El directorio privado debe ser absoluto.")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise BridgeError("No se permiten enlaces en la ruta del directorio privado.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise BridgeError("Directorio privado inseguro: requiere propietario actual y permisos 0700.")
    return path
