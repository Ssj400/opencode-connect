#!/usr/bin/env python3
"""Linux to WSL bridge: connect, check, and the read-only doctor."""

from pathlib import Path
import sys

# Run from any directory without installing: the package is next to this folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opencode_connect.bridge.bridge import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
