"""Dependency extraction for C++, Java and JavaScript.

Every language answers the same two questions differently:

  * under what name does the rest of the codebase refer to this file?
  * given a name written in an import, which file does it mean?

Python's answers live in depgraph.py and stay there. These are the other three.
Each is a genuinely different naming scheme, not a cosmetic variation:

  C++   addresses files by path, resolved relative to the including file or to
        an include root, so one file is registered under every suffix of its
        path and the longest match wins.
  Java  addresses files by package-qualified class name, which is derived from
        the `package` declaration rather than from the directory -- a file can
        sit anywhere and still be `com.acme.Invoice`.
  JS    addresses files by relative path with the extension left off, and a
        directory stands for its index file.

Bare specifiers (`<vector>`, `java.util.List`, `react`) name things outside the
repository. They are counted as unresolved and never become edges -- a
dependency graph of third-party packages is a different tool.
"""

from __future__ import annotations

import posixpath
import re

from .specs import CPP, JAVA, LanguageSpec

# ----------------------------------------------------------------- C++

def _cpp_keys(rel: str, source: str, tree) -> list[str]:
    """Every suffix of the path. `#include "core.h"` must find src/engine/core.h."""
    parts = rel.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]


def _cpp_targets(tree, source: str) -> list[tuple[str, str]]:
    out = []
    for node in _iter(tree.root_node, {"preproc_include"}):
        path_node = node.child_by_field_name("path")
        if path_node is None:
            continue
        raw = path_node.text.decode("utf-8", errors="replace").strip()
        kind = "include"
        if raw.startswith("<") and raw.endswith(">"):
            raw, kind = raw[1:-1], "system_include"
        elif raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        if raw:
            out.append((raw, kind))
    return out


def _cpp_resolve(raw: str, rel: str, index: dict[str, str]) -> str | None:
    raw = raw.replace("\\", "/").lstrip("./") if raw.startswith("./") else raw.replace("\\", "/")
    # Relative to the including file first: that is what the compiler does for
    # a quoted include, and it is the only way two same-named headers in
    # different directories can be told apart.
    here = posixpath.dirname(rel)
    if here:
        joined = posixpath.normpath(posixpath.join(here, raw))
        if joined in index:
            return index[joined]
    return index.get(raw)


# ----------------------------------------------------------------- Java

_PKG = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def _java_keys(rel: str, source: str, tree) -> list[str]:
    """package-qualified class name, plus the bare path as a fallback."""
    stem = posixpath.basename(rel)
    if stem.endswith(".java"):
        stem = stem[: -len(".java")]
    m = _PKG.search(source)
    keys = [rel]
    if m:
        keys.insert(0, f"{m.group(1)}.{stem}")
    else:
        keys.insert(0, stem)
    return keys


def _java_targets(tree, source: str) -> list[tuple[str, str]]:
    out = []
    for node in _iter(tree.root_node, {"import_declaration"}):
        text = node.text.decode("utf-8", errors="replace")
        text = text.replace("import", "", 1).replace("static", "", 1)
        text = text.strip().rstrip(";").strip()
        if not text:
            continue
        if text.endswith(".*"):
            # A wildcard names a package, not a file. There is no single edge
            # to draw, so it is recorded as unresolved rather than guessed at.
            out.append((text[:-2], "wildcard_import"))
        else:
            out.append((text, "import"))
    return out


def _java_resolve(raw: str, rel: str, index: dict[str, str]) -> str | None:
    if raw in index:
        return index[raw]
    # `import com.acme.Outer.Inner` refers to a nested class inside Outer.java.
    parts = raw.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in index:
            return index[candidate]
    return None


# ----------------------------------------------------------------- JS / TS

_JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")


def _js_keys(rel: str, source: str, tree) -> list[str]:
    """Path without extension, and the directory when the file is its index."""
    base = rel
    for ext in _JS_EXT:
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    keys = [base]
    if posixpath.basename(base) == "index":
        parent = posixpath.dirname(base)
        if parent:
            keys.append(parent)
    return keys


def _string_value(node) -> str | None:
    if node is None:
        return None
    text = node.text.decode("utf-8", errors="replace").strip()
    if len(text) >= 2 and text[0] in "\"'`" and text[-1] == text[0]:
        return text[1:-1]
    return None


def _js_targets(tree, source: str) -> list[tuple[str, str]]:
    out = []
    wanted = {"import_statement", "export_statement", "call_expression"}
    for node in _iter(tree.root_node, wanted):
        if node.type in ("import_statement", "export_statement"):
            val = _string_value(node.child_by_field_name("source"))
            if val:
                out.append((val, "import" if node.type == "import_statement" else "reexport"))
            continue

        # require("x") and the dynamic import("x")
        fn = node.child_by_field_name("function")
        if fn is None:
            continue
        name = fn.text.decode("utf-8", errors="replace")
        if name not in ("require", "import"):
            continue
        args = node.child_by_field_name("arguments")
        if args is None:
            continue
        for child in args.named_children:
            val = _string_value(child)
            if val:
                out.append((val, "require" if name == "require" else "dynamic_import"))
            break
    return out


def _js_resolve(raw: str, rel: str, index: dict[str, str]) -> str | None:
    # A specifier that does not start with a dot or a slash names a package in
    # node_modules, not a file in this repository.
    if not raw.startswith((".", "/")):
        return None
    here = posixpath.dirname(rel)
    target = posixpath.normpath(posixpath.join(here, raw)) if raw.startswith(".") else raw.lstrip("/")

    if target in index:
        return index[target]
    for ext in _JS_EXT:                       # written with the extension on
        if target.endswith(ext) and target[: -len(ext)] in index:
            return index[target[: -len(ext)]]
    indexed = posixpath.join(target, "index")
    return index.get(indexed)


# ----------------------------------------------------------------- dispatch

def _iter(root, types: set[str]):
    """Every node of the given types. Iterative, for the same reason as measure()."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in types:
            yield node
        stack.extend(reversed(node.children))


_HANDLERS = {
    "cpp":        (_cpp_keys,  _cpp_targets,  _cpp_resolve),
    "java":       (_java_keys, _java_targets, _java_resolve),
    "javascript": (_js_keys,   _js_targets,   _js_resolve),
    "typescript": (_js_keys,   _js_targets,   _js_resolve),
    "tsx":        (_js_keys,   _js_targets,   _js_resolve),
}


def index_keys(spec: LanguageSpec, rel: str, source: str, tree) -> list[str]:
    return _HANDLERS[spec.name][0](rel, source, tree)


def targets(spec: LanguageSpec, tree, source: str) -> list[tuple[str, str]]:
    if tree is None:
        return []
    return _HANDLERS[spec.name][1](tree, source)


def resolve(spec: LanguageSpec, raw: str, rel: str, index: dict[str, str]) -> str | None:
    return _HANDLERS[spec.name][2](raw, rel, index)
