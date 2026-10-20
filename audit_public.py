#!/usr/bin/env python3
"""Offline allowlist/privacy audit. Heuristics, not a substitute for human review."""
import ipaddress
from pathlib import Path
import re
import sys

from deploy import FILES

# Documentation networks only; no operational IPv4 addresses in an export.
NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8"))


def findings(text):
    issues = set()
    for value in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", text):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not any(address in network for network in NETWORKS):
            issues.add("IP address outside documentation or loopback networks")
    for owner in re.findall(r"/(?:home|Users)/([A-Za-z0-9_.-]+)(?:/|\b)", text):
        if owner not in {"example", "user"}:
            issues.add("Identifiable home directory path")
    if re.search(r"\bses_[A-Za-z0-9]{20,}\b|\bmsg_[a-f0-9]{32}\b", text):
        issues.add("Session or message ID appears operational")
    if re.search(r"SHA256:[A-Za-z0-9+/]{40,}={0,2}", text):
        issues.add("SSH fingerprint appears operational")
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        issues.add("Private key")
    if re.search(r"\b(?:gh[pousr]_[A-Za-z0-9]{24,}|sk-[A-Za-z0-9_-]{24,})\b", text):
        issues.add("Potential secret token")
    return sorted(issues)


def audit(root):
    root = Path(root)
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
        issues.extend(f"{name}: {issue}" for issue in findings(text))
    allowed_dirs = {str(parent) for name in FILES for parent in Path(name).parents}
    # Generated files are never exported. Root Git metadata is not project content;
    # nested repositories and symlinks are still rejected.
    ignored = {"__pycache__", ".test-tmp"}
    def visit(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root)
            if path.is_symlink():
                issues.append(f"{relative}: symlinks cannot be exported")
            elif relative == Path(".git"):
                continue
            elif path.name in ignored and path.is_dir():
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
    issues = audit(Path(__file__).resolve().parent)
    for issue in issues:
        print(issue)
    if issues:
        print("Audit failed; review findings without automatically deleting user files.")
        return 1
    print(f"Audit passed: {len(FILES)} allowlisted files; no heuristic matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
