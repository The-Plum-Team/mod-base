"""``python3 -P -m mod_base <command> ...`` entry point."""

from __future__ import annotations

import sys

from mod_base.cli import main

if __name__ == "__main__":
    sys.exit(main())
