#!/usr/bin/env python3
"""Offline allowlist/privacy audit. Heuristics, not a substitute for human review."""
import ipaddress
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Run from any directory, like the launchers in bin/.
from opencode_connect.bridge.deploy import FILES  # noqa: E402

# Documentation networks only; no operational addresses in an export.
NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8",
    "2001:db8::/32", "3fff::/20"))
# IPv6-shaped text also occurs in slices such as [1::2]. Only global, unique local
# (used by mesh VPNs) and link-local addresses are operational; the start of each
# range, as written here, names the category rather than a host.
ROUTED_IPV6 = tuple(ipaddress.ip_network(n) for n in ("2000::/3", "fc00::/7", "fe80::/10"))
CATEGORY_IPV6 = {network.network_address for network in ROUTED_IPV6}
# The nil and max UUIDs of RFC 9562; Claude Code session IDs are random UUIDs.
SPECIAL_UUIDS = {"00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"}
# Names a heuristic cannot know (a VPN, host or person) live in a private file
# outside the export, one per line. Findings cite its line numbers, never the terms.
TERMS_VARIABLE = "OPENCODE_CONNECT_AUDIT_TERMS"


def random_shaped(value, length):
    """Random tokens mix upper case, lower case and digits; placeholders rarely do."""
    return (len(value) >= length and any(c.isdigit() for c in value)
            and any(c.islower() for c in value) and any(c.isupper() for c in value))


def private_terms(environ=os.environ):
    """(line number, term) pairs from the file named by TERMS_VARIABLE, if any."""
    if not environ.get(TERMS_VARIABLE):
        return ()
    lines = Path(environ[TERMS_VARIABLE]).read_text(encoding="utf-8").splitlines()
    return tuple((number, line.strip().casefold()) for number, line in enumerate(lines, 1)
                 if line.strip() and not line.lstrip().startswith("#"))


def findings(text, terms=()):
    issues = set()
    for value in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", text):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not any(address in network for network in NETWORKS):
            issues.add("IP address outside documentation or loopback networks")
    for value in re.findall(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])", text):
        try:
            address = ipaddress.IPv6Address(value)
        except ValueError:
            continue
        if (address not in CATEGORY_IPV6 and any(address in network for network in ROUTED_IPV6)
                and not any(address in network for network in NETWORKS)):
            issues.add("IP address outside documentation or loopback networks")
    if re.search(r"(?i)\b[a-z0-9-]+\.ts\.net\b", text):
        issues.add("Mesh VPN host name")
    for owner in re.findall(r"/(?:home|Users)/([A-Za-z0-9_.-]+)(?:/|\b)", text):
        if owner not in {"example", "user"}:
            issues.add("Identifiable home directory path")
    if re.search(r"\bses_[A-Za-z0-9]{20,}\b|\bmsg_[a-f0-9]{32}\b", text):
        issues.add("Session or message ID appears operational")
    if any(value.lower() not in SPECIAL_UUIDS for value in re.findall(
            r"\b[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\b", text)):
        issues.add("UUID appears operational")
    if re.search(r"SHA256:[A-Za-z0-9+/]{40,}={0,2}", text):
        issues.add("SSH fingerprint appears operational")
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        issues.add("Private key")
    if re.search(r"\b(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_\w{22,}|sk-[A-Za-z0-9_-]{24,}"
                 r"|glpat-[\w-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|tskey-[A-Za-z0-9-]{10,}"
                 r"|AKIA[0-9A-Z]{16}|eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.)", text):
        issues.add("Potential secret token")
    labelled = re.findall(r"""(?i:bearer)\s+([^\s"']+)"""
                          r"""|(?i:token|secret|password|api[_-]?key)["']?\s*[:=]\s*["']([^\s"']+)["']""", text)
    bare = re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])", text)
    if (any(random_shaped(header or value, 20) for header, value in labelled)
            or any(random_shaped(value, 32) for value in bare)):
        issues.add("Potential secret token")
    folded = text.casefold()
    for number, term in terms:
        if term in folded:
            issues.add(f"Term from line {number} of the private deny-list")
    return sorted(issues)


def audit(root, terms=None):
    """`terms` defaults to the private deny-list named by TERMS_VARIABLE."""
    root = Path(root)
    terms = private_terms() if terms is None else terms
    issues = []
    for name in FILES:
        path = root / name
        if path.is_symlink():
            issues.append(f"{name}: symlinks are not allowed")
            continue
        if any((root / parent).is_symlink() for parent in Path(name).parents):
            issues.append(f"{name}: linked directory")
            continue
        if not path.is_file():
            issues.append(f"{name}: missing file")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            issues.append(f"{name}: not UTF-8 text")
            continue
        issues.extend(f"{name}: {issue}" for issue in findings(text, terms))
    allowed_dirs = {str(parent) for name in FILES for parent in Path(name).parents}
    # Generated files, and Claude Code's and OpenCode's settings for this checkout
    # (ignored by Git), are never exported. Root Git metadata is not project content;
    # nested repositories and symlinks are still rejected.
    ignored = {"__pycache__", ".test-tmp", ".claude", ".opencode"}
    harness_files = {Path("opencode.json"), Path("opencode.jsonc")}
    def visit(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root)
            if path.is_symlink():
                issues.append(f"{relative}: symlinks cannot be exported")
            elif relative == Path(".git"):
                continue
            elif path.name in ignored and path.is_dir() or relative in harness_files and path.is_file():
                continue
            elif path.is_dir():
                if str(relative) not in allowed_dirs:
                    issues.append(f"{relative}: directory outside the allowlist (potential private state)")
                else:
                    visit(path)
            elif str(relative) not in FILES:
                issues.append(f"{relative}: file outside the allowlist")
    visit(root)
    return sorted(set(issues))


def main():
    try:
        terms = private_terms()
    except (OSError, UnicodeError):
        print(f"Cannot read the private deny-list named by {TERMS_VARIABLE}.")
        return 2
    issues = audit(ROOT, terms)
    for issue in issues:
        print(issue)
    if issues:
        print("Audit failed; review findings without automatically deleting user files.")
        return 1
    print(f"Audit passed: {len(FILES)} allowlisted files; no heuristic matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
