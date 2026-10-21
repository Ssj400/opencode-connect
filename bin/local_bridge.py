#!/usr/bin/env python3
"""Local text queue between sessions on this machine, and its relay."""

from pathlib import Path
import sys

# Run from any directory without installing: the package is next to this folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opencode_connect.local.mailbox import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
