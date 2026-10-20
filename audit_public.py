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
            issues.add("IP fuera de redes de documentación/loopback")
    for owner in re.findall(r"/(?:home|Users)/([A-Za-z0-9_.-]+)(?:/|\b)", text):
        if owner not in {"example", "user"}:
            issues.add("Ruta de hogar identificable")
    if re.search(r"\bses_[A-Za-z0-9]{20,}\b|\bmsg_[a-f0-9]{32}\b", text):
        issues.add("ID de sesión/mensaje con apariencia operativa")
    if re.search(r"SHA256:[A-Za-z0-9+/]{40,}={0,2}", text):
        issues.add("Huella SSH con apariencia operativa")
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
        issues.add("Clave privada")
    if re.search(r"\b(?:gh[pousr]_[A-Za-z0-9]{24,}|sk-[A-Za-z0-9_-]{24,})\b", text):
        issues.add("Token con apariencia secreta")
    return sorted(issues)


def audit(root):
    root = Path(root)
    issues = []
    for name in FILES:
        path = root / name
        if path.is_symlink():
            issues.append(f"{name}: enlace no permitido")
            continue
        if any((root / parent).is_symlink() for parent in Path(name).parents):
            issues.append(f"{name}: directorio enlazado")
            continue
        if not path.is_file():
            issues.append(f"{name}: archivo ausente")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            issues.append(f"{name}: no es texto UTF-8")
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
                issues.append(f"{relative}: enlace no exportable")
            elif relative == Path(".git"):
                continue
            elif path.name in ignored and path.is_dir():
                continue
            elif path.is_dir():
                if str(relative) not in allowed_dirs:
                    issues.append(f"{relative}: directorio fuera de lista (posible estado privado)")
                else:
                    visit(path)
            elif str(relative) not in FILES:
                issues.append(f"{relative}: archivo fuera de lista")
    visit(root)
    return sorted(set(issues))


def main():
    issues = audit(Path(__file__).resolve().parent)
    for issue in issues:
        print(issue)
    if issues:
        print("Auditoría fallida; revisar, no borrar automáticamente archivos del usuario.")
        return 1
    print(f"Auditoría OK: {len(FILES)} archivos permitidos; sin coincidencias heurísticas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
