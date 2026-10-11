"""Entry point for `python -m repohealth` and for the frozen executable.

A packaged build is launched by double-clicking, with no arguments, so the
default there is the app rather than argparse's "a command is required" error.
"""

from __future__ import annotations

import sys

from .cli import main
from .resources import is_frozen

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv and is_frozen():
        argv = ["app", "--window"]
    raise SystemExit(main(argv))
