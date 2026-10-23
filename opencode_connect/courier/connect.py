"""Connect a project's OpenCode and Claude Code sessions to this machine's local queue.

Only what is missing changes, after one confirmation code: the MCP servers, added with
each harness's own command; the Claude Code hooks and deny rules, merged into the
project's `.claude/settings.local.json`; and that file, kept out of Git locally.
"""

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile

from opencode_connect.config import ROOT
from opencode_connect.core import BridgeError
from opencode_connect.courier.host import claude_mcp_add, opencode_mcp_add, queue_variables
from opencode_connect.courier.settings import clean_display, interactive_confirmation
from opencode_connect.local.mailbox import state_path
from opencode_connect.transports import short_text

SETTINGS = Path(".claude/settings.local.json")
HOOK = ROOT / "bin/claude_hook.py"


def run(command, project):
    try:
        return subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError(f"{command[0]} could not run ({type(exc).__name__}).") from exc


def home_relative(path):
    path = Path(path)
    return "~/" + str(path.relative_to(Path.home())) if path.is_relative_to(Path.home()) else str(path)


def claude_settings():
    """The hooks and deny rules of `examples/claude-settings.local.json.example`, for
    this checkout and this terminal's queue."""
    prefix = ["env", *queue_variables()] if queue_variables() else []

    def hook(event, **extra):
        return {"type": "command", "command": shlex.join([*prefix, "python3", str(HOOK), event]), **extra}

    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode-connect"
    state = home_relative(state_path().parent)
    return {"hooks": {"PreToolUse": [{"matcher": "mcp__opencode_connect__.*", "hooks": [hook("pre-tool")]}],
                      "UserPromptSubmit": [{"hooks": [hook("prompt")]}],
                      "Stop": [{"hooks": [hook("stop"), hook("wake", asyncRewake=True, timeout=86400)]}]},
            "permissions": {"deny": ["Bash(*courier.py*)", "Bash(*local_bridge.py*)",
                                     *(f"{rule}({folder}/**)" for folder in (home_relative(config), state)
                                       for rule in ("Read", "Edit"))]}}


def read_settings(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise BridgeError(f"{path} is a symbolic link; it was left untouched.")
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BridgeError(f"{path} is not valid JSON; fix it by hand first.") from exc
    if not isinstance(settings, dict):
        raise BridgeError(f"{path} must hold a JSON object; fix it by hand first.")
    return settings


def merge_settings(current, wanted):
    """`current` plus the hooks and deny rules of `wanted` it lacks, and what was added.
    Nothing is removed; a hook counts as present when it runs the same event of
    `claude_hook.py`, even from another checkout."""
    merged = json.loads(json.dumps(current))
    hooks = merged.setdefault("hooks", {})
    permissions = merged.setdefault("permissions", {})
    deny = permissions.setdefault("deny", []) if isinstance(permissions, dict) else None
    if not isinstance(hooks, dict) or not isinstance(deny, list):
        raise BridgeError("The settings' hooks or permissions are not in the expected shape; fix them by hand first.")
    added = []
    for event, groups in wanted["hooks"].items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list) or not all(isinstance(group, dict) for group in existing):
            raise BridgeError(f"The settings' {event} hooks are not in the expected shape; fix them by hand first.")
        commands = [hook.get("command") for group in existing
                    for hook in (group.get("hooks") if isinstance(group.get("hooks"), list) else [])
                    if isinstance(hook, dict)]
        present = {command.split(" ")[-1] for command in commands
                   if isinstance(command, str) and "claude_hook.py" in command}
        for group in groups:
            missing = [hook for hook in group["hooks"] if hook["command"].split(" ")[-1] not in present]
            if missing:
                existing.append({**group, "hooks": missing})
                added += [f"the {hook['command'].split(' ')[-1]} hook" for hook in missing]
    for rule in wanted["permissions"]["deny"]:
        if rule not in deny:
            deny.append(rule)
            added.append(f"deny {rule}")
    return merged, added


def write_settings(path, settings):
    path.parent.mkdir(exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(settings, output, indent=2, ensure_ascii=False)
            output.write("\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def exclude_from_git(project):
    with (project / ".git/info/exclude").open("a", encoding="utf-8") as exclude:
        exclude.write(f"# Claude Code's settings for this checkout (opencode-connect).\n{SETTINGS}\n")


def applied(command, project):
    result = run(command, project)
    if result.returncode:
        raise BridgeError(f"{shlex.join(command[:4])} failed: "
                          f"{short_text(' '.join(result.stderr.split())) or 'status ' + str(result.returncode)}")


def connect_project(project):
    """Show what the project lacks, ask for one code, then add it. 0 when done."""
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise BridgeError(f"{project} is not a folder.")
    steps = []
    if not shutil.which("opencode"):
        print("OpenCode is not installed here; skipped.")
    elif not any("local_bridge" in line.split() for line in run(["opencode", "mcp", "list"], project).stdout.splitlines()):
        steps.append(("OpenCode: add the MCP server local_bridge", lambda: applied(opencode_mcp_add(), project)))
    if not shutil.which("claude"):
        print("Claude Code is not installed here; skipped.")
    else:
        if run(["claude", "mcp", "get", "opencode_connect"], project).returncode:
            steps.append(("Claude Code: add the MCP server opencode_connect, for this project only",
                          lambda: applied(claude_mcp_add(), project)))
        merged, added = merge_settings(read_settings(project / SETTINGS), claude_settings())
        if added:
            steps.append((f"Claude Code: add to {SETTINGS} " + ", ".join(added),
                          lambda: write_settings(project / SETTINGS, merged)))
        if ((project / ".git/info").is_dir() and shutil.which("git")
                and run(["git", "-C", str(project), "check-ignore", "-q", str(SETTINGS)], project).returncode):
            steps.append((f"Git: keep {SETTINGS} out of commits, in .git/info/exclude",
                          lambda: exclude_from_git(project)))
    if not steps:
        print("This project is already connected.")
        return 0
    print(clean_display(f"To connect {project}:"))
    for description, _ in steps:
        print(clean_display("  - " + description))
    interactive_confirmation("Connect this project")
    for description, action in steps:
        action()
        print(clean_display("Done: " + description))
    print("Restart OpenCode in this project, and start a new Claude Code session there (or reconnect "
          "the server in /mcp). Ask each one for whoami to get its address.")
    return 0
