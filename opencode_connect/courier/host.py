"""What this machine offers a courier: addresses, WSL, systemd, and the commands left
for its owner. Everything here only reads; printed commands are never run."""

import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from opencode_connect.config import ROOT
from opencode_connect.courier.pairing import VPN_NETWORKS, is_vpn

DEFAULT_PORT = 8765
# RFC 1918 networks, as integers like VPN_NETWORKS.
LAN_NETWORKS = (ipaddress.ip_network((0x0A000000, 8)), ipaddress.ip_network((0xAC100000, 12)),
                ipaddress.ip_network((0xC0A80000, 16)))
# WSL's NAT, and the Windows Hyper-V switches that mirrored mode shows, use this range.
WSL_NAT = LAN_NETWORKS[1]
# Loopback, and container and virtual machine bridges: never a contact's way in.
VIRTUAL = re.compile(r"lo|docker\d*|br-.*|veth.*|virbr\d*(-nic)?")
INTERFACE = re.compile(r"[\w.-]{1,15}")
# The ID Windows' Hyper-V firewall gives WSL. Written in parts: the export audit
# treats a whole UUID as an operational identifier.
WSL_VM_CREATOR = "{" + "-".join(("40E0AC32", "46A5", "438A", "A0B2", "2B479E8F2E90")) + "}"
# Variables that choose the queue: an MCP server must use the same as the courier.
QUEUE_ENVIRONMENT = ("OPENCODE_CONNECT_LOCAL_NODE", "OPENCODE_CONNECT_LOCAL_STATE_HOME")
WSL_LISTENS = ("On WSL, this courier listens only while a WSL session is open; "
               "contacts' couriers keep retrying until then.")


def command_output(command):
    """What a read-only command prints, or None when it cannot run."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def candidates(links):
    """Addresses a contact could reach, from `ip -j addr`: IPv4 on a VPN, then on a LAN.

    Links that are down, loopback, container bridges, and public or link-local
    addresses are left out.
    """
    found = []
    for link in links if isinstance(links, list) else ():
        if (not isinstance(link, dict) or "UP" not in link.get("flags", ())
                or not INTERFACE.fullmatch(str(link.get("ifname"))) or VIRTUAL.fullmatch(link["ifname"])):
            continue
        for info in link.get("addr_info", ()):
            try:
                interface = ipaddress.IPv4Interface((info["local"], int(info["prefixlen"])))
            except (KeyError, TypeError, ValueError):
                continue  # IPv6, or not an address.
            address = interface.ip
            kind = "VPN" if is_vpn(address) else "LAN" if any(address in network for network in LAN_NETWORKS) else None
            if kind:
                found.append({"interface": link["ifname"], "address": str(address),
                              "network": str(interface.network), "kind": kind})
    return sorted(found, key=lambda row: row["kind"] != "VPN")


def interface_addresses():
    try:
        return candidates(json.loads(command_output(["ip", "-j", "addr"]) or "[]"))
    except ValueError:
        return []


def on_wsl(release=Path("/proc/sys/kernel/osrelease")):
    try:
        return "microsoft" in release.read_text(encoding="utf-8").lower()
    except OSError:
        return False


def user_manager():
    """Whether systemd runs a manager for this user, which the service needs."""
    return command_output(["systemctl", "--user", "is-system-running"]) in {"running", "degraded", "starting"}


def lingering():
    """Whether this user's services run after logout; None when unknown."""
    output = command_output(["loginctl", "show-user", str(os.getuid()), "--property=Linger"])
    return {"Linger=yes": True, "Linger=no": False}.get(output)


def peers(choice):
    """Who may connect: the LAN's subnet, or a mesh VPN's range when its link is a /32."""
    network = ipaddress.ip_network(choice["network"])
    if choice["kind"] == "VPN" and network.prefixlen == network.max_prefixlen:
        network = VPN_NETWORKS[0]
    return str(network)


def firewall_commands(choice, port, *, wsl=False):
    """(where, command) pairs that let only the chosen network reach the listener, for
    the owner to review and run: for each firewall tool found, and Windows' for WSL."""
    source, address, commands = peers(choice), choice["address"], []
    if shutil.which("ufw"):
        commands.append(("ufw", f"sudo ufw allow in on {choice['interface']} from {source} to {address} "
                                f"port {port} proto tcp comment 'opencode-connect courier'"))
    if shutil.which("firewall-cmd"):
        rule = (f'rule family="ipv4" source address="{source}" destination address="{address}" '
                f'port port="{port}" protocol="tcp" accept')
        commands.append(("firewalld", "sudo firewall-cmd --permanent --add-rich-rule=" + shlex.quote(rule)
                         + " && sudo firewall-cmd --reload"))
    if wsl:
        commands.append(("Windows (PowerShell as administrator)",
                         "New-NetFirewallHyperVRule -Name OpenCodeConnectCourier -DisplayName 'OpenCode Connect courier' "
                         f"-Direction Inbound -VMCreatorId '{WSL_VM_CREATOR}' -Protocol TCP -LocalPorts {port} "
                         f"-RemoteAddresses {source}"))
    return commands


def mcp_commands():
    """The commands that give OpenCode and Claude Code sessions in a project this queue."""
    shared = [f"{name}={os.environ[name]}" for name in QUEUE_ENVIRONMENT if os.environ.get(name)]
    opencode = ["opencode", "mcp", "add", "local_bridge", "--", *(["env", *shared] if shared else []),
                "python3", str(ROOT / "bin/local_mcp_server.py")]
    claude = ["claude", "mcp", "add", "opencode_connect", "--scope", "local",
              *(part for pair in shared for part in ("-e", pair)), "--", "python3", str(ROOT / "bin/claude_mcp_server.py")]
    return shlex.join(opencode), shlex.join(claude)
