"""A desktop notice when a contact's message waits for approval; never its text."""

import shutil
import subprocess


def desktop_notification(name):
    """Best effort: a missing notify-send or desktop session never affects delivery.

    Only the contact's name is shown. It is a validated lowercase identifier, while
    a message is untrusted text that a notification server could render as markup.
    """
    program = shutil.which("notify-send")
    if program is None:
        return
    try:
        subprocess.run([program, "--app-name=opencode-connect", f"Message from {name} waiting",
                        "Review it with `courier.py inbox`, then approve or reject it."],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
