"""`courier.py` without arguments, in a terminal: a numbered menu over the same commands.

Each choice builds a subcommand's arguments and runs it through `cli.main`, so it gets
exactly the same checks and confirmation codes. The menu only adds lists to choose
contacts and sessions from, and connecting a project's sessions to the local queue.
"""

import os
from pathlib import Path
import sqlite3
import time

from opencode_connect.claude import hook as claude_hook
from opencode_connect.config import local_node
from opencode_connect.core import BridgeError
from opencode_connect.courier.connect import connect_project
from opencode_connect.courier.doctor import local_queue_root, open_state
from opencode_connect.courier.settings import clean_display, read_config
from opencode_connect.courier.status import ago, read_state
from opencode_connect.local.mailbox import local_adapter

MENU = """\
 People
  1. Set up this machine
  2. Invite someone: show a pairing link
  3. Join someone with their link
  4. Choose where a contact's messages go
  5. Remove a contact
  6. Status
 Sessions on this machine
  7. Connect a project's sessions (OpenCode and Claude Code)
  8. Show session addresses
  0. Exit"""
TYPE_ADDRESS = "Type an address"
MODES = {"ask": "Held until you approve each one (the default)",
         "direct": "Delivered on arrival, up to 20 an hour"}


def pick(rows, describe, extra=None):
    """A numbered choice among `rows`, plus `extra` if given; None for 0 or Enter."""
    for number, row in enumerate(rows, 1):
        print(clean_display(f"  {number}. {describe(row)}"))
    if extra:
        print(f"  {len(rows) + 1}. {extra}")
    print("  0. Back")
    while True:
        answer = input("Choose: ").strip()
        if answer in {"", "0"}:
            return None
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(rows):
            return rows[int(answer) - 1]
        if extra and answer == str(len(rows) + 1):
            return extra
        print(f"Type a number from 0 to {len(rows) + bool(extra)}.")


def header(config_path):
    try:
        config = read_config(config_path)
    except BridgeError:
        return "OpenCode Connect: this machine is not set up yet (choose 1)."
    return f"OpenCode Connect: {config['person']} at {config['url']}"


def contacts(config_path):
    """Contact names, from the queue when it can be read, else from the configuration."""
    try:
        config = read_config(config_path)
    except BridgeError:
        return []
    try:
        db = open_state(local_queue_root())
        if db is not None:
            try:
                state = read_state(db, time.time())
            finally:
                db.close()
            config = state["config"] if state else config
    except (BridgeError, OSError, ValueError, sqlite3.Error):
        pass
    return sorted(set(config["tokens"]) | set(config["contacts"]))


def destinations():
    """Sessions on this machine to choose from: recent OpenCode sessions, then the Claude
    Code sessions its hooks have seen. Listing OpenCode's may start its local service."""
    rows = []
    try:
        node = local_node()
        rows += [{"address": f"{node}:{session['id']}",
                  "label": f"OpenCode: {session['title'] or '(untitled)'} ({session['directory']})"}
                 for session in local_adapter().recent_sessions()]
    except (BridgeError, OSError):
        print("OpenCode sessions could not be listed here; type the address, or run whoami in that session.")
    rows += [{"address": session["address"],
              "label": f"Claude Code: {session['cwd'] or 'unknown folder'}, seen {ago(time.time() - session['seen'])}"}
             for session in claude_hook.sessions()]
    return rows


def describe(row):
    return f"{row['label']}  ({row['address']})"


def choose_contact(config_path):
    names = contacts(config_path)
    if not names:
        print("No contacts yet: pair with someone first (2 or 3).")
        return None
    return pick(names, str)


def route(run, config_path):
    name = choose_contact(config_path)
    if name is None:
        return
    print(f"Where should {name}'s messages go?")
    choice = pick(destinations(), describe, extra=TYPE_ADDRESS)
    if choice is None:
        return
    target = input("Address (node:ses_… or claude:UUID): ").strip() if choice == TYPE_ADDRESS else choice["address"]
    print(f"How should {name}'s messages arrive?")
    mode = pick(list(MODES), MODES.get)
    if target and mode:
        run(["contact", name, "--to", target, f"--{mode}"])


def remove(run, config_path):
    name = choose_contact(config_path)
    if name is not None:
        run(["unpair", name])


def invite(run):
    name = input("Accept only this person's courier (Enter for anyone): ").strip()
    run(["pair", *(["--for", name] if name else [])])


def join(run):
    link = input("Paste the link they sent you: ").strip()
    if link:
        run(["pair", link])


def connect():
    folder = input(f"Project folder [{os.getcwd()}]: ").strip()
    connect_project(Path(folder or os.getcwd()))


def addresses():
    rows = destinations()
    for row in rows:
        print(clean_display("  " + describe(row)))
    if not rows:
        print("No sessions found: open OpenCode or Claude Code in a connected project first (7).")


def run_menu(config_path, queue_factory):
    from opencode_connect.courier.cli import main  # The menu is part of the command line.

    def run(arguments):
        return main(["--config", str(config_path), *arguments], queue_factory=queue_factory)

    actions = {"1": lambda: run(["setup"]), "2": lambda: invite(run), "3": lambda: join(run),
               "4": lambda: route(run, config_path), "5": lambda: remove(run, config_path),
               "6": lambda: run(["status"]), "7": connect, "8": addresses}
    try:
        while True:
            print()
            print(clean_display(header(config_path)))
            print(MENU)
            choice = input("Choose: ").strip()
            if choice == "0":
                return 0
            if choice not in actions:
                print(f"Type a number from 0 to {len(actions)}.")
                continue
            try:
                actions[choice]()
            except BridgeError as exc:
                print(clean_display(exc))
            input("Press Enter to return to the menu.")
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
