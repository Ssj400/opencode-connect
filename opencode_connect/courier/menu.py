"""`courier.py` without arguments, in a terminal: a numbered menu over the same commands.

Each choice builds a subcommand's arguments and runs it through `cli.main`, so it gets
exactly the same checks and confirmation codes. The menu only adds lists to choose
contacts, sessions, and team members from, and connecting a project's sessions to the
local queue.
"""

import os
from pathlib import Path
import sqlite3
import time

from opencode_connect.claude import hook as claude_hook
from opencode_connect.courier import host, setup
from opencode_connect.config import local_node
from opencode_connect.core import BridgeError
from opencode_connect.courier.connect import connect_project
from opencode_connect.courier.doctor import local_queue_root, open_state
from opencode_connect.courier.settings import clean_display, contact_url, listen_address, read_config
from opencode_connect.courier.status import ago, read_state, team_lines
from opencode_connect.local.mailbox import local_adapter

MENU = """\
 People
  1. Set up this machine
  2. Invite someone: show a pairing link
  3. Join someone with their link
  4. Choose where a contact's messages go
  5. Remove a contact
  6. Status
  7. Change this machine's address
 Sessions on this machine
  8. Connect a project's sessions (OpenCode and Claude Code)
  9. Show session addresses
 Team
 10. Invite a machine to your team: show a link
 11. Join a team with its link
 12. Your team: members, removing, leaving
  0. Exit"""
TYPE_ADDRESS = "Type an address"
MODES = {"ask": "Held until you approve each one (the default)",
         "direct": "Delivered on arrival, up to 20 an hour"}
TEAM_ACTIONS = {"remove": "Remove from the team: the others drop them, and they drop the others",
                "introduce": "Introduce again to the members they are not connected with"}


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
    lost = host.lost_address(config["listen"])
    return f"OpenCode Connect: {config['person']} at {config['url']}" + (
        "\nWarning: " + host.moved_advice(lost) if lost else "")


def queue_state():
    """The courier's state in the queue, read without changing it; None when it cannot be."""
    try:
        db = open_state(local_queue_root())
        if db is None:
            return None
        try:
            return read_state(db, time.time())
        finally:
            db.close()
    except (BridgeError, OSError, ValueError, sqlite3.Error):
        return None


def current_config(config_path):
    """This courier's configuration, from the queue when it can be read; None before setup."""
    try:
        config = read_config(config_path)
    except BridgeError:
        return None
    state = queue_state()
    return state["config"] if state else config


def contacts(config_path):
    """Contact names, from the queue when it can be read, else from the configuration."""
    config = current_config(config_path)
    return [] if config is None else sorted(set(config["tokens"]) | set(config["contacts"]))


def yes(question):
    return input(f"{question} [y/N]: ").strip().lower() in {"y", "yes"}


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


def change_address(run, config_path):
    """A new address from this machine's current ones; `address` asks for the code, and
    contacts' couriers are told the new URL afterwards."""
    try:
        config = read_config(config_path)
    except BridgeError:
        print("This machine is not set up yet: choose 1 first.")
        return
    found = host.reachable(host.interface_addresses(), host.on_wsl())
    if not found:
        print("No LAN or VPN address found: connect to the network or start the VPN first.")
        return
    print(f"This courier listens on {config['listen']}.")
    choice = setup.choose(found)
    port = setup.free_port(choice["address"], default=listen_address(config["listen"], allow_wildcard=True)[1])
    origin = f"{choice['address']}:{port}"
    run(["address", "--listen", origin, "--url", "http://" + origin])


def invite(run):
    name = input("Accept only this person's courier (Enter for anyone): ").strip()
    run(["pair", *(["--for", name] if name else [])])


def join(run):
    link = input("Paste the link they sent you: ").strip()
    if link:
        run(["pair", link])


def invite_team(run, config_path):
    name = input("Accept only this person's courier (Enter for anyone): ").strip()
    again = bool(name) and name in contacts(config_path)
    if again and not yes(f"{name} is already a contact. Admit them to your team, replacing that pairing?"):
        return
    run(["team", "invite", *(["--for", name] if name else []), *(["--replace"] if again else [])])


def join_team(run, config_path):
    link = input("Paste the link the team's owner sent you: ").strip()
    if not link:
        return
    try:
        origin = contact_url(link.partition("#")[0])
    except BridgeError:
        origin = None  # `join` explains what is wrong with the link.
    cards = (current_config(config_path) or {"contacts": {}})["contacts"]
    known = [name for name, card in sorted(cards.items()) if card["url"] == origin]
    if known and not yes(f"The link comes from {known[0]}, already a contact. Join their team, "
                         "replacing that pairing?"):
        return
    run(["join", link, *(["--replace"] if known else [])])


def manage_team(run):
    """The team this courier is in, or owns: leave it, or remove or introduce a member."""
    state = queue_state()
    if state is None or not (state["owner"] or state["members"]):
        print("No team yet: 10 shows a link that admits a machine to your team, and 11 joins someone else's.")
        return
    for line in team_lines(state):
        print(clean_display(line))
    if state["owner"]:
        if pick(["Leave the team"], str) is not None:
            run(["team", "leave"])
        return
    name = pick(state["members"], str)
    action = name and pick(list(TEAM_ACTIONS), TEAM_ACTIONS.get)
    if action:
        run(["team", action, name])


def connect():
    folder = input(f"Project folder [{os.getcwd()}]: ").strip()
    connect_project(Path(folder or os.getcwd()))


def addresses():
    rows = destinations()
    for row in rows:
        print(clean_display("  " + describe(row)))
    if not rows:
        print("No sessions found: open OpenCode or Claude Code in a connected project first (8).")


def run_menu(config_path, queue_factory):
    from opencode_connect.courier.cli import main  # The menu is part of the command line.

    def run(arguments):
        return main(["--config", str(config_path), *arguments], queue_factory=queue_factory)

    actions = {"1": lambda: run(["setup"]), "2": lambda: invite(run), "3": lambda: join(run),
               "4": lambda: route(run, config_path), "5": lambda: remove(run, config_path),
               "6": lambda: run(["status"]), "7": lambda: change_address(run, config_path),
               "8": connect, "9": addresses, "10": lambda: invite_team(run, config_path),
               "11": lambda: join_team(run, config_path), "12": lambda: manage_team(run)}
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
