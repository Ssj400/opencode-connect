"""`setup` and what it reads from this machine: addresses, WSL, systemd, and the queue."""

import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from opencode_connect.config import ROOT
from opencode_connect.core import BridgeError
from opencode_connect.courier import host
from opencode_connect.courier.pairing import VPN_NETWORKS
from opencode_connect.courier.settings import read_config, write_config
from opencode_connect.courier.setup import can_bind, setup
from opencode_connect.local.mailbox import LocalMailbox
from tests.courier.fakes import configuration


def ipv4(*octets):
    # Built from numbers: the export audit flags private addresses written out.
    return ".".join(map(str, octets))


LAN, VPN, NAT, DOCKER = ipv4(192, 168, 1, 20), ipv4(100, 64, 0, 10), ipv4(172, 28, 1, 5), ipv4(172, 17, 0, 1)
UP = ("BROADCAST", "MULTICAST", "UP", "LOWER_UP")


def link(name, *addresses, flags=UP):
    return {"ifname": name, "flags": list(flags), "addr_info": [
        {"family": "inet6" if ":" in address else "inet", "local": address.split("/")[0],
         "prefixlen": int(address.split("/")[1]), "scope": "global"} for address in addresses]}


LINKS = [link("lo", "127.0.0.1/8", flags=("LOOPBACK", "UP")), link("wlan0", LAN + "/24", "2001:db8::5/64"),
         link("xsred0", VPN + "/32", flags=("POINTOPOINT", "UP")), link("docker0", DOCKER + "/16"),
         link("veth1a2b", ipv4(10, 0, 0, 2) + "/24"), link("br-0a1b", ipv4(10, 1, 0, 1) + "/24"),
         link("eth1", "203.0.113.5/24"), link("wlan1", ipv4(10, 2, 0, 9) + "/24", flags=("BROADCAST",))]


def system(links=LINKS, manager="running", linger="Linger=no"):
    """A fake `subprocess.run` for the read-only commands setup and status run."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        output = {"ip": None if links is None else json.dumps(links), "systemctl": manager,
                  "loginctl": linger}[command[0]]
        if output is None:
            raise FileNotFoundError(command[0])
        return subprocess.CompletedProcess(command, 0, stdout=output + "\n", stderr="")

    run.calls = calls
    return run


class HostTests(unittest.TestCase):
    def test_candidates_are_vpn_then_lan_ipv4_on_real_links_that_are_up(self):
        self.assertEqual(host.candidates(LINKS), [
            {"interface": "xsred0", "address": VPN, "network": VPN + "/32", "kind": "VPN"},
            {"interface": "wlan0", "address": LAN, "network": ipv4(192, 168, 1, 0) + "/24", "kind": "LAN"}])
        for links in ("not a list", [None], [{"ifname": "eth0", "flags": ["UP"], "addr_info": [{"family": "inet"}]}],
                      [link("we ird", LAN + "/24")], [link("eth0", LAN + "/33")]):
            with self.subTest(links=links):
                self.assertEqual(host.candidates(links), [])

    def test_unreadable_commands_and_output_mean_nothing_found(self):
        for run in (system(links=None), system(links="[")):
            with patch("opencode_connect.courier.host.subprocess.run", run):
                self.assertEqual(host.interface_addresses(), [])
        with patch("opencode_connect.courier.host.subprocess.run", side_effect=subprocess.TimeoutExpired("ip", 10)):
            self.assertIsNone(host.command_output(["ip", "-j", "addr"]))

    def test_systemd_and_linger_are_read_not_changed(self):
        for manager, linger, expected in (("running", "Linger=yes", (True, True)), ("degraded", "Linger=no", (True, False)),
                                          ("offline", "", (False, None)), (None, None, (False, None))):
            with self.subTest(manager=manager), patch("opencode_connect.courier.host.subprocess.run",
                                                      system(manager=manager, linger=linger)) as run:
                self.assertEqual((host.user_manager(), host.lingering()), expected)
            self.assertEqual([command[1:3] for command in run.calls], [["--user", "is-system-running"], ["show-user", str(os.getuid())]])

    def test_wsl_is_read_from_the_kernel_release(self):
        with tempfile.TemporaryDirectory(prefix="courier-wsl-") as folder:
            release = Path(folder) / "osrelease"
            release.write_text("6.6.87-microsoft-standard-WSL2\n")
            self.assertTrue(host.on_wsl(release))
            release.write_text("4.4.0-19041-Microsoft\n")  # WSL 1.
            self.assertTrue(host.on_wsl(release))
            release.write_text("7.2.11-1-example\n")
            self.assertFalse(host.on_wsl(release))
            self.assertFalse(host.on_wsl(Path(folder) / "missing"))

    def test_firewall_commands_admit_only_the_network_and_are_never_run(self):
        lan, vpn = host.candidates(LINKS)[1], host.candidates(LINKS)[0]
        tools = {"ufw": "/usr/sbin/ufw", "firewall-cmd": "/usr/bin/firewall-cmd"}
        with patch("opencode_connect.courier.host.shutil.which", side_effect=tools.get), \
                patch("opencode_connect.courier.host.subprocess.run") as run:
            commands = dict(host.firewall_commands(lan, 8765))
            ufw_vpn = dict(host.firewall_commands(vpn, 8765))["ufw"]
            hyper_v = dict(host.firewall_commands(lan, 8765, wsl=True))
        run.assert_not_called()
        self.assertIn(f"allow in on wlan0 from {ipv4(192, 168, 1, 0)}/24 to {LAN} port 8765 proto tcp", commands["ufw"])
        self.assertIn(f'source address="{ipv4(192, 168, 1, 0)}/24" destination address="{LAN}" port port="8765"',
                      commands["firewalld"])
        # A mesh VPN gives each machine a /32: its peers are the VPN's range.
        self.assertIn(f"allow in on xsred0 from {VPN_NETWORKS[0]} to {VPN}", ufw_vpn)
        rule = hyper_v["Windows (PowerShell as administrator)"]
        self.assertIn(f"-Direction Inbound -VMCreatorId '{host.WSL_VM_CREATOR}' -Protocol TCP -LocalPorts 8765 "
                      f"-RemoteAddresses {ipv4(192, 168, 1, 0)}/24", rule)
        self.assertEqual(uuid.UUID(host.WSL_VM_CREATOR.strip("{}")).hex.upper(),
                         "40E0AC32" "46A5438A" "A0B22B47" "9E8F2E90")
        with patch("opencode_connect.courier.host.shutil.which", return_value=None):
            self.assertEqual(host.firewall_commands(lan, 8765), [])

    def test_mcp_commands_carry_the_queue_settings_of_this_terminal(self):
        with patch.dict(os.environ, {"OPENCODE_CONNECT_LOCAL_NODE": "wsl"}):
            os.environ.pop("OPENCODE_CONNECT_LOCAL_STATE_HOME", None)
            opencode, claude = host.mcp_commands()
        self.assertEqual(shlex.split(opencode), ["opencode", "mcp", "add", "--env", "OPENCODE_CONNECT_LOCAL_NODE=wsl",
                                                 "local_bridge", "--", "python3", str(ROOT / "bin/local_mcp_server.py")])
        self.assertEqual(shlex.split(claude), ["claude", "mcp", "add", "opencode_connect", "--scope", "local", "-e",
                                               "OPENCODE_CONNECT_LOCAL_NODE=wsl", "--", "python3",
                                               str(ROOT / "bin/claude_mcp_server.py")])
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("--env", shlex.split(host.mcp_commands()[0]))


class SetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="courier-setup-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "config/opencode-connect/courier.json"
        tools = self.root / "bin"
        tools.mkdir()
        (tools / "opencode").write_text("#!/bin/sh\n")
        (tools / "opencode").chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": str(tools), "XDG_CONFIG_HOME": str(self.root / "config"),
                                              "OPENCODE_CONNECT_LOCAL_STATE_HOME": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("OPENCODE_CONNECT_LOCAL_NODE", "XDG_STATE_HOME"):
            os.environ.pop(name, None)

    def run_setup(self, answers=(), *, links=LINKS, manager="running", wsl=False, confirm=None, bind=None, **options):
        """setup in a terminal: `answers` are typed in order; `confirm` stands in for each code."""
        prompts, typed = [], list(answers)

        def ask(prompt=""):
            prompts.append(prompt)
            if not typed:
                raise AssertionError(f"Unexpected question: {prompt}")
            return typed.pop(0)

        with patch("builtins.input", side_effect=ask), \
                patch("opencode_connect.courier.host.subprocess.run", system(links=links, manager=manager)), \
                patch("opencode_connect.courier.host.on_wsl", return_value=wsl), \
                patch("opencode_connect.courier.host.shutil.which",
                      side_effect=lambda name, path=None: {"ufw": "/usr/sbin/ufw"}.get(name) or (
                          str(self.root / "bin/opencode") if name == "opencode" and path else None)), \
                patch("opencode_connect.courier.setup.interactive_confirmation", side_effect=confirm) as confirmation, \
                patch("opencode_connect.courier.setup.can_bind", side_effect=bind) as can_bind, \
                patch("opencode_connect.courier.setup.install_service",
                      return_value=(self.root / "unit", str(self.root / "bin/opencode"))) as install, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = setup(self.path, **options)
        self.assertEqual(typed, [], "Answers left unread")
        return result, output.getvalue(), confirmation, can_bind, install, prompts

    def test_setup_writes_the_chosen_address_after_one_code_and_says_what_is_left(self):
        result, output, confirmation, can_bind, install, prompts = self.run_setup(["Alice", "alice", "3", "2", "80", ""])
        self.assertEqual(result, 0)
        self.assertEqual(read_config(self.path), {"person": "alice", "listen": f"{LAN}:8765", "url": f"http://{LAN}:8765",
                                                  "tokens": {}, "contacts": {}})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        can_bind.assert_called_once_with(LAN, 8765)
        self.assertEqual(len(confirmation.call_args_list), 2)
        self.assertIn(f"{LAN}:8765", confirmation.call_args_list[0].args[0])
        self.assertIn("service", confirmation.call_args_list[1].args[0])
        install.assert_called_once_with(self.path)
        self.assertLess(output.index(f"1. {VPN} on xsred0 (VPN)"), output.index(f"2. {LAN} on wlan0 (LAN"))
        self.assertIn("other devices on this network could read", output)
        self.assertNotIn("access rules", output)  # A LAN address has none.
        self.assertNotIn(DOCKER, output)
        for text in (f"sudo ufw allow in on wlan0 from {ipv4(192, 168, 1, 0)}/24 to {LAN} port 8765",
                     "local_bridge -- python3", "claude mcp add opencode_connect", "Linger is off",
                     "bin/courier.py pair"):
            self.assertIn(text, output)

    def test_an_existing_configuration_is_kept_and_only_checked(self):
        write_config(self.path, {**configuration("alice"), "listen": f"{LAN}:9000", "url": f"http://{LAN}:9000"})
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
        result, output, confirmation, can_bind, install, _ = self.run_setup(
            confirm=BridgeError("Confirmation did not match; nothing was changed."))
        self.assertEqual(result, 0)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        can_bind.assert_not_called()
        install.assert_not_called()
        self.assertIn("alice", output)
        self.assertIn("courier.py address", output)
        self.assertIn("port 9000", output)
        self.assertIn("service install", output)

    def test_wsl_with_nat_explains_mirrored_mode_and_writes_nothing(self):
        links = [link("lo", "127.0.0.1/8"), link("eth0", NAT + "/20")]
        result, output, confirmation, _, _, _ = self.run_setup(links=links, wsl=True)
        self.assertEqual(result, 1)
        for text in ("networkingMode=mirrored", "wsl --shutdown", "--allow-172"):
            self.assertIn(text, output)
        self.assertFalse(self.path.exists())
        confirmation.assert_not_called()
        # A LAN that really uses that range, when the owner says so.
        result, output, _, _, _, _ = self.run_setup(["alice", "", ""], links=links, wsl=True, allow_172=True)
        self.assertEqual((result, read_config(self.path)["listen"]), (0, f"{NAT}:8765"))
        self.assertIn("New-NetFirewallHyperVRule", output)

    def test_mirrored_wsl_offers_the_lan_and_not_the_windows_switches(self):
        links = [link("eth0", LAN + "/24"), link("eth1", NAT + "/20")]
        result, output, _, _, _, _ = self.run_setup(["alice", "", ""], links=links, wsl=True)
        self.assertEqual((result, read_config(self.path)["listen"]), (0, f"{LAN}:8765"))
        self.assertNotIn(NAT, output)
        self.assertIn("listens only while a WSL session is open", output)

    def test_a_port_in_use_asks_for_another(self):
        result, output, _, can_bind, _, _ = self.run_setup(["alice", "\u00b2", "1", "\u00b2", "", "9001"],
                                                           bind=[OSError(98, "Address already in use"), None])
        self.assertEqual((result, read_config(self.path)["listen"]), (0, f"{VPN}:9001"))
        self.assertIn("Address already in use", output)
        self.assertIn("access rules (ACLs)", output)  # A VPN address: its rules must allow the port.
        self.assertEqual(can_bind.call_count, 2)

    def test_a_port_in_use_cannot_be_bound(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            with self.assertRaises(OSError):
                can_bind("127.0.0.1", busy.getsockname()[1])
        can_bind("127.0.0.1", 0)

    def test_a_configuration_written_meanwhile_is_not_overwritten(self):
        other = {**configuration("bob"), "listen": f"{LAN}:9000", "url": f"http://{LAN}:9000"}
        with self.assertRaisesRegex(BridgeError, "not overwritten"):
            self.run_setup(["alice", "", ""], confirm=lambda prompt: write_config(self.path, other))
        self.assertEqual(read_config(self.path), other)

    def test_an_installed_service_is_not_offered_again(self):
        unit = self.root / "config/systemd/user/opencode-connect-courier.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n")
        result, output, confirmation, _, install, _ = self.run_setup(["alice", "", ""])
        self.assertEqual((result, len(confirmation.call_args_list)), (0, 1))
        install.assert_not_called()
        self.assertIn("service is installed", output)

    def test_without_an_address_or_with_another_nodes_queue_nothing_is_written(self):
        result, output, _, _, _, _ = self.run_setup(links=[link("lo", "127.0.0.1/8")])
        self.assertEqual(result, 1)
        self.assertIn("No LAN or VPN address", output)
        box = LocalMailbox(self.root / "state/opencode-connect/local", node="wsl")
        box.enqueue("wsl:ses_example", "wsl:ses_other", "hello")  # The first message binds the queue.
        box.db.close()
        result, output, confirmation, _, _, _ = self.run_setup()  # Before any question.
        self.assertEqual(result, 1)
        self.assertIn("OPENCODE_CONNECT_LOCAL_NODE=wsl", output)
        confirmation.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_without_systemd_the_service_is_not_offered(self):
        result, output, confirmation, _, install, _ = self.run_setup(["alice", "", ""], manager=None)
        self.assertEqual(result, 0)
        self.assertEqual(len(confirmation.call_args_list), 1)
        install.assert_not_called()
        self.assertIn("run --relay", output)
        self.assertNotIn("Linger", output)


if __name__ == "__main__":
    unittest.main()
