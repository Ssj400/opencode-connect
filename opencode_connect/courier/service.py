"""The optional systemd user unit that runs the courier and the local relay at login."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from opencode_connect.config import ROOT
from opencode_connect.core import BridgeError
from opencode_connect.courier.settings import config_home, read_config
from opencode_connect.transports import short_text

SERVICE = "opencode-connect-courier.service"
LAUNCHER = ROOT / "bin/courier.py"
# Variables that choose the queue, this configuration, or the OpenCode service:
# the unit keeps the installing terminal's values, so both use the same state.
SERVICE_ENVIRONMENT = ("OPENCODE_CONNECT_LOCAL_NODE", "OPENCODE_CONNECT_LOCAL_STATE_HOME",
                       "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


def unit_path():
    return config_home() / "systemd/user" / SERVICE


def unit_word(value, *, command=False):
    """One quoted unit value in which no specifier, nor in a command a variable, expands."""
    value = str(value)
    if not value.isprintable():
        raise BridgeError("Service paths and settings must be printable.")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (value.replace("$", "$$") if command else value) + '"'


def render_unit(python, script, config, environment):
    """The user unit: absolute paths, and the installing terminal's settings."""
    if not all(Path(path).is_absolute() for path in (python, script, config)):
        raise BridgeError("The service requires absolute paths.")
    command = " ".join(unit_word(word, command=True)
                       for word in (python, "-B", script, "--config", config, "run", "--relay"))
    return "\n".join([
        "[Unit]",
        "Description=OpenCode Connect courier and local relay",
        "StartLimitIntervalSec=0",  # Keep restarting while a network or service is down.
        "",
        "[Service]",
        "ExecStart=" + command,
        *("Environment=" + unit_word(f"{key}={value}") for key, value in environment.items()),
        "Restart=on-failure",
        "RestartSec=5",
        # SIGTERM reaches only the courier, which lets a delivery in progress finish;
        # the stop timeout is above the 30 s a delivery may take.
        "KillMode=mixed",
        "TimeoutStopSec=45",
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=default.target",
        ""])


def service_environment(environ):
    """PATH with the folder of the `opencode` this terminal runs, and the variables above."""
    opencode = shutil.which("opencode", path=environ.get("PATH"))
    folders = [str(Path(opencode).absolute().parent)] if opencode else []
    folders += [folder for folder in SYSTEM_PATH if folder not in folders]
    if any(":" in folder for folder in folders):
        raise BridgeError("The opencode folder cannot be part of a PATH.")
    environment = {"PATH": ":".join(folders)}
    environment.update((key, environ[key]) for key in SERVICE_ENVIRONMENT if environ.get(key))
    return environment, opencode


def systemctl(*arguments):
    try:
        result = subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(f"systemctl --user {arguments[0]} failed ({type(exc).__name__}).") from exc
    if result.returncode:
        detail = short_text(" ".join(result.stderr.split())) or f"status {result.returncode}"
        raise BridgeError(f"systemctl --user {arguments[0]} failed: {detail}")


def install_service(config_path, environ=os.environ):
    """Write the unit once, then enable and start it. A different existing unit is kept."""
    read_config(config_path)  # A unit that cannot read its configuration only restarts.
    environment, opencode = service_environment(environ)
    text = render_unit(sys.executable, LAUNCHER, config_path, environment)
    path = unit_path()
    if path.is_symlink() or (path.exists() and path.read_text(encoding="utf-8") != text):
        raise BridgeError(f"A different {path} exists; it was not overwritten. "
                          "Review it, then run `python3 bin/courier.py service uninstall`.")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as unit:
            unit.write(text)
    systemctl("daemon-reload")
    systemctl("enable", "--now", SERVICE)
    return path, opencode


def restart_service():
    """Restart the installed unit if it is running, so it uses a changed configuration.

    False when this command never installed the unit.
    """
    if unit_path().is_symlink() or not unit_path().is_file():
        return False
    systemctl("try-restart", SERVICE)
    return True


def uninstall_service():
    """Stop, disable, and remove the unit `install` wrote; never another file."""
    path = unit_path()
    if path.is_symlink() or not path.is_file():
        raise BridgeError("The courier service is not installed.")
    if "Description=OpenCode Connect courier" not in path.read_text(encoding="utf-8"):
        raise BridgeError(f"{path} was not written by this command; it was left untouched.")
    systemctl("disable", "--now", SERVICE)
    path.unlink()
    systemctl("daemon-reload")
    return path


def service_command(action, config_path):
    if action in {"status", "logs"}:
        # systemd's own view, passed through; nothing changes.
        command = (["systemctl", "--user", "status", "--no-pager", SERVICE] if action == "status" else
                   ["journalctl", "--user", "--unit", SERVICE, "--lines", "100", "--no-pager"])
        try:
            return subprocess.run(command, timeout=60).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"{command[0]} failed ({type(exc).__name__}).") from exc
    if action == "install":
        path, opencode = install_service(config_path)
        print(f"Service installed at {path} and started; it starts again at each login.")
        if opencode is None:
            print("opencode is not on PATH here: the service cannot deliver to OpenCode sessions "
                  "until it is reinstalled from a terminal where it is.")
    elif action == "uninstall":
        print(f"Service stopped and removed: {uninstall_service()}")
    else:
        systemctl(action, SERVICE)
        print("Service started." if action == "start" else
              "Service stopped; a delivery in progress finished first.")
    return 0
