#!/usr/bin/env python3
"""Person-to-person courier over a private LAN or VPN."""

import os
from pathlib import Path
import sys

# Run from any directory without installing: the package is next to this folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opencode_connect.courier.cli import main  # noqa: E402

if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
