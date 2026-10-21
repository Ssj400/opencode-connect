"""`setup`: this courier's name and address, a check of this machine, and what is left
for its owner to do. It writes the configuration and installs the service only after
a confirmation code; firewall and MCP commands are printed, never run."""

import ipaddress
import os
from pathlib import Path
import socket

from opencode_connect.config import local_node
from opencode_connect.core import BridgeError
from opencode_connect.courier import host
from opencode_connect.courier.service import install_service, service_environment, unit_path
from opencode_connect.courier.settings import interactive_confirmation, listen_address, person, read_config, write_config
from opencode_connect.local.mailbox import inspect_queue

WSL_MIRRORED = """\
This WSL uses NAT networking, so other machines cannot reach it. Switch it to mirrored
networking, which needs Windows 11 22H2 or later:
  1. In Windows, add these lines to %UserProfile%\\.wslconfig:
       [wsl2]
       networkingMode=mirrored
  2. In Windows, run `wsl --shutdown`. It closes every open WSL window and session.
  3. Open WSL again and run `python3 bin/courier.py setup` again.
If {network} is the range of your actual LAN, run `setup --allow-172` instead."""


def can_bind(address, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # As the listener does.
        probe.bind((address, port))


def check_machine(wsl):
    """Print what this machine has for a courier. Returns whether the queue is usable,
    and whether systemd runs a manager for this user."""
    try:
        queue = inspect_queue()
    except BridgeError as exc:
        print(f"FAIL  The local queue cannot be read: {exc}")
        return False, False
    if queue is not None and not queue["compatible"]:
        stored = (queue["contract"] or {}).get("canonical_node")
        print(f"FAIL  The local queue belongs to node {stored!r}, not {queue['configured']['canonical_node']!r}. "
              + (f"Set OPENCODE_CONNECT_LOCAL_NODE={stored} in this terminal and run setup again."
                 if stored else "Run `python3 bin/bridge.py doctor --local` to see why."))
        return False, False
    print(f"OK    Local queue for node {local_node()}: " + ("new." if queue is None else "in use."))
    manager = host.user_manager()
    if not manager:
        print("WARN  No systemd user manager: start the courier with `python3 bin/courier.py run --relay` instead.")
    elif host.lingering() is False:
        print("WARN  Linger is off: the service runs from login to logout. `loginctl enable-linger` keeps it running.")
    else:
        print("OK    systemd can run the courier as a service.")
    opencode = service_environment(os.environ)[1]
    print(f"OK    opencode at {opencode}." if opencode else
          "WARN  opencode is not on PATH: the courier cannot deliver to OpenCode sessions from this terminal.")
    if wsl:
        print("WARN  " + host.WSL_LISTENS)
    return True, manager


def whole(text, low, high):
    return text.isascii() and text.isdigit() and low <= int(text) <= high


def ask(question, check):
    """Ask until `check(answer)` returns a value; it raises BridgeError to ask again."""
    while True:
        try:
            return check(input(question).strip())
        except BridgeError as exc:
            print(exc)


def choose(found):
    print("Addresses where contacts can reach this machine:")
    for number, row in enumerate(found, 1):
        detail = "VPN" if row["kind"] == "VPN" else "LAN: unencrypted, other devices on this network could read messages"
        print(f"  {number}. {row['address']} on {row['interface']} ({detail})")

    def number(answer):
        if not whole(answer or "1", 1, len(found)):
            raise BridgeError(f"Type a number from 1 to {len(found)}.")
        return found[int(answer or "1") - 1]

    return ask("Address [1]: ", number)


def free_port(address):
    def port(answer):
        if not whole(answer or str(host.DEFAULT_PORT), 1024, 65535):
            raise BridgeError("Type a port from 1024 to 65535.")
        value = int(answer or host.DEFAULT_PORT)
        try:
            can_bind(address, value)
        except OSError as exc:
            raise BridgeError(f"Cannot listen on {address}:{value} ({exc.strerror or exc}); "
                              "choose another port.") from exc
        return value

    return ask(f"Port [{host.DEFAULT_PORT}]: ", port)


def new_config(found, wsl, allow_172):
    """Ask for a name and an address; None when this machine has none to offer."""
    if wsl and not allow_172:
        # Under NAT these are WSL's own; in mirrored mode, Windows' virtual switches.
        found = [row for row in found if ipaddress.ip_address(row["address"]) not in host.WSL_NAT]
    if not found:
        print(WSL_MIRRORED.format(network=host.WSL_NAT) if wsl else
              "No LAN or VPN address found: connect to the network or start the VPN that your contacts use, "
              "then run setup again.")
        return None
    name = ask("Your name, as contacts will see it (lowercase letters, digits, - and _): ", person)
    choice = choose(found)
    port = free_port(choice["address"])
    origin = f"{choice['address']}:{port}"
    return {"person": name, "listen": origin, "url": "http://" + origin, "tokens": {}, "contacts": {}}


def print_firewall(choice, port, wsl):
    commands = host.firewall_commands(choice, port, wsl=wsl)
    print("Let contacts through the firewall. Review these and run them yourself; setup changed nothing:")
    for where, command in commands:
        print(f"  {where}: {command}")
    if not commands:
        print(f"  If this machine filters incoming connections, allow TCP port {port} to {choice['address']} "
              f"on {choice['interface']}, from {host.peers(choice)} only.")


def offer_service(config_path, manager):
    if not manager:
        print("Start the courier in a terminal with `python3 bin/courier.py run --relay`, and keep it open.")
        return
    if unit_path().is_file():
        print("The courier service is installed; `python3 bin/courier.py service status` shows it.")
        return
    try:
        interactive_confirmation("Install the service that runs the courier at each login (Enter skips)")
    except BridgeError:
        print("Service not installed. Later: `python3 bin/courier.py service install`, "
              "or `python3 bin/courier.py run --relay` in a terminal.")
        return
    path, _ = install_service(config_path)
    print(f"Service installed at {path} and started; it starts again at each login.")


def setup(config_path, *, allow_172=False):
    config_path = Path(config_path)
    wsl = host.on_wsl()
    ready, manager = check_machine(wsl)
    if not ready:
        return 1
    found = host.interface_addresses()
    existing = config_path.exists() or config_path.is_symlink()
    if existing:
        config = read_config(config_path)
        print(f"Already set up as {config['person']}, listening on {config['listen']}; "
              "`python3 bin/courier.py address` changes the address.")
    else:
        config = new_config(found, wsl, allow_172)
        if config is None:
            return 1
        interactive_confirmation(f"Confirm setup: {config['person']}, listening on {config['listen']}, "
                                 f"reachable at {config['url']}")
        if config_path.exists() or config_path.is_symlink():
            raise BridgeError("Courier configuration already exists; it was not overwritten.")
        write_config(config_path, config)
        print(f"Configuration written to {config_path}.")
    address, port = listen_address(config["listen"], allow_wildcard=True)
    choice = next((row for row in found if row["address"] == address), None)
    if choice is None:
        print(f"{address} is not an address of this machine now: is its network or VPN down?")
    else:
        print_firewall(choice, port, wsl)
    opencode, claude = host.mcp_commands()
    print("Let the agents in a project use the courier (see docs/local-messaging.md and docs/claude-code.md):")
    print(f"  OpenCode: {opencode}")
    print(f"  Claude Code: {claude}")
    print("    plus the hooks in examples/claude-settings.local.json.example.")
    offer_service(config_path, manager)
    print("Next, pair with a contact: `python3 bin/courier.py pair`.")
    return 0
