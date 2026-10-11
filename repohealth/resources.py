"""Locating bundled data files.

`schema.sql` and the web assets sit next to the code during normal use, but a
PyInstaller build unpacks them into a temporary directory and `__file__` points
somewhere else entirely. Everything that reads a bundled file goes through here
so the one-file executable works without a second code path.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def bundled(*parts: str) -> Path:
    """Path to a data file shipped with the package."""
    base = Path(getattr(sys, "_MEIPASS", "")) / "repohealth" if is_frozen() else PACKAGE_ROOT
    return base.joinpath(*parts)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))
