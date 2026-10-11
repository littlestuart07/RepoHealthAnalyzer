"""Language registry.

A file's extension picks its language; the language supplies the grammar facts
and the import rules. Python is deliberately absent from SPECS -- it keeps the
`ast`-based engine in analysis/complexity.py and its own resolver in
analysis/depgraph.py, and is routed by `is_python` instead.
"""

from __future__ import annotations

import posixpath

from .specs import SPECS, LanguageSpec

PYTHON_EXTENSIONS = (".py", ".pyi")

_BY_EXT: dict[str, LanguageSpec] = {}
for _spec in SPECS:
    for _ext in _spec.extensions:
        _BY_EXT.setdefault(_ext, _spec)

# Every extension RepoHealth will analyse, Python included. `git ls-files`
# patterns and the gatekeeper's staged-file filter are both built from this.
ALL_EXTENSIONS: tuple[str, ...] = tuple(
    sorted(set(PYTHON_EXTENSIONS) | set(_BY_EXT))
)

# label -> extensions, for `repohealth languages` and the config file.
LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {"Python": PYTHON_EXTENSIONS}
for _spec in SPECS:
    LANGUAGE_EXTENSIONS.setdefault(_spec.label, ())
    LANGUAGE_EXTENSIONS[_spec.label] += _spec.extensions


def _ext(path: str) -> str:
    return posixpath.splitext(path.replace("\\", "/"))[1].lower()


def is_python(path: str) -> bool:
    return _ext(path) in PYTHON_EXTENSIONS


def spec_for(path: str) -> LanguageSpec | None:
    """The non-Python language of this file, or None for Python and unknowns."""
    return _BY_EXT.get(_ext(path))


def label_for(path: str) -> str:
    if is_python(path):
        return "Python"
    spec = spec_for(path)
    return spec.label if spec else "unknown"


def is_source(path: str, enabled: set[str] | None = None) -> bool:
    """Is this a file we analyse? `enabled` filters by language label."""
    label = label_for(path)
    if label == "unknown":
        return False
    return enabled is None or label in enabled


__all__ = [
    "ALL_EXTENSIONS", "LANGUAGE_EXTENSIONS", "PYTHON_EXTENSIONS", "SPECS",
    "LanguageSpec", "is_python", "is_source", "label_for", "spec_for",
]
