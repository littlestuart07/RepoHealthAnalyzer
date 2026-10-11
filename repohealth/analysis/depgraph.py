"""Dependency graph construction (SRS 4.3.2.1)."""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def module_name(rel_path: str, source_root: str = "") -> str:
    """pkg/sub/mod.py -> pkg.sub.mod ; pkg/__init__.py -> pkg

    `source_root` is the directory imports are resolved relative to. Under a
    `src/` layout the file src/myapp/app.py is imported as `myapp.app`, not
    `src.myapp.app`, so the root prefix is stripped first.
    """
    p = rel_path.replace("\\", "/")
    if source_root and p.startswith(source_root + "/"):
        p = p[len(source_root) + 1:]
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p == "__init__.py":
        return ""
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def source_root_for(rel_path: str, pathset: set[str]) -> str:
    """The directory this file's imports are resolved against.

    Walks up from the file while each ancestor directory is a package (has an
    __init__.py). The first ancestor that is NOT a package is the source root:
    that is the directory Python itself would have on sys.path.

        src/myapp/core/config.py  with __init__.py in myapp/ and core/  -> "src"
        pkg/core/settings.py      with __init__.py in pkg/ and core/    -> ""
        scripts/build.py          with no __init__.py anywhere          -> "scripts"
    """
    parts = rel_path.replace("\\", "/").split("/")
    d = len(parts) - 1
    while d > 0:
        if "/".join(parts[:d]) + "/__init__.py" in pathset:
            d -= 1
        else:
            break
    return "/".join(parts[:d])


def build_module_index(paths: list[str]) -> dict[str, str]:
    """module name -> repo-relative file path.

    Each file is registered under its source-root-relative name. The
    repo-root-relative name is also registered when it does not collide, so
    monorepos that import across roots both ways still resolve.
    """
    pathset = set(p.replace("\\", "/") for p in paths)
    index: dict[str, str] = {}
    fallback: dict[str, str] = {}

    for p in paths:
        root = source_root_for(p, pathset)
        primary = module_name(p, root)
        if primary:
            index.setdefault(primary, p)
        if root:
            alt = module_name(p, "")
            if alt and alt != primary:
                fallback.setdefault(alt, p)

    for name, p in fallback.items():
        index.setdefault(name, p)
    return index


def _package_of(rel_path: str, source_root: str = "") -> str:
    """Dotted package containing this file, for relative-import resolution."""
    p = rel_path.replace("\\", "/")
    if p.endswith("/__init__.py") or p == "__init__.py":
        return module_name(p, source_root)
    if source_root and p.startswith(source_root + "/"):
        p = p[len(source_root) + 1:]
    parent = "/".join(p.split("/")[:-1])
    return parent.replace("/", ".") if parent else ""


def _resolve(target: str, index: dict[str, str]) -> str | None:
    """Longest-prefix match: `pkg.mod.Class` resolves to `pkg.mod` if that's a file."""
    if not target:
        return None
    parts = target.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in index:
            return index[candidate]
    return None


# Node types that can contain statements. Everything else in an AST is an
# expression, and no import or control-flow statement can hide inside one.
_STATEMENT_HOLDERS = (ast.stmt, ast.ExceptHandler) + (
    (ast.match_case,) if hasattr(ast, "match_case") else ()
)


def iter_statements(node: ast.AST):
    """Every statement under `node`, skipping expression subtrees entirely.

    ast.walk visits every node in the tree -- on a large repository that is
    millions of Name and Constant nodes examined to find a handful of imports.

    Breadth-first, like ast.walk, and deliberately so: cycle detection reports
    one representative set of loops, and which ones it finds depends on the
    order edges were added. Matching ast.walk's order keeps results stable
    rather than shifting because the traversal was made faster.
    """
    queue = deque([node])
    while queue:
        current = queue.popleft()
        for _, value in ast.iter_fields(current):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, _STATEMENT_HOLDERS):
                        yield item
                        queue.append(item)
            elif isinstance(value, _STATEMENT_HOLDERS):
                yield value
                queue.append(value)



def type_checking_statements(tree: ast.AST) -> set[int]:
    """id() of every statement inside an `if TYPE_CHECKING:` block.

    Such an import never executes. It is a dependency for a type checker and
    for nobody else, so it cannot cause an ImportError and cannot form a
    runtime circular import. Counting it as one produces exactly the advice a
    developer has already taken: Flask guards eleven of its imports this way,
    and every one of them was being reported as a loop to go and fix.

    Matched by name, on `TYPE_CHECKING`, `t.TYPE_CHECKING` or
    `typing.TYPE_CHECKING`, which is how it is spelled in practice.
    """
    out: set[int] = set()

    def is_guard(test: ast.AST) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.If) and is_guard(node.test):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    out.add(id(inner))
    return out


@dataclass
class Edge:
    from_path: str
    to_path: str
    kind: str
    # The names imported across this edge. The refactoring advisor needs them:
    # to break a cycle you have to know WHAT crosses the link, not just that
    # something does. Empty when the import names a module rather than symbols
    # from it (`import pkg.mod`), or when the language does not say.
    names: list[str] = field(default_factory=list)

    def line(self) -> str:
        """`a.py -> b.py (Widget, render)`, for a human to read."""
        shown = ", ".join(self.names[:4]) + ("..." if len(self.names) > 4 else "")
        return f"{self.from_path} -> {self.to_path}" + (f" ({shown})" if shown else "")


@dataclass
class DependencyGraph:
    nodes: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)

    def adjacency(self, runtime_only: bool = False) -> dict[str, list[str]]:
        """`runtime_only` drops imports that only a type checker ever follows."""
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if runtime_only and e.kind == "type_checking":
                continue
            if e.to_path not in adj.setdefault(e.from_path, []):
                adj[e.from_path].append(e.to_path)
        return adj

    def reverse_adjacency(self) -> dict[str, list[str]]:
        rev: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if e.from_path not in rev.setdefault(e.to_path, []):
                rev[e.to_path].append(e.from_path)
        return rev

    def to_json(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": [{"from": e.from_path, "to": e.to_path, "kind": e.kind} for e in self.edges],
        }


def extract_imports(
    rel_path: str,
    source: str,
    index: dict[str, str],
    source_root: str = "",
    tree: ast.AST | None = None,
) -> tuple[list[Edge], list[str]]:
    """Returns (resolved edges, unresolved module names). Never raises.

    `tree` lets the caller supply an already-parsed AST; see analyze_source.
    """
    if tree is None:
        source = source.lstrip("\ufeff")   # see analyze_source: UTF-8 BOM
        try:
            tree = ast.parse(source, filename=rel_path)
        except (SyntaxError, ValueError, RecursionError):
            return [], []

    edges: list[Edge] = []
    unresolved: list[str] = []
    pkg = _package_of(rel_path, source_root)
    seen: set[tuple[str, str]] = set()
    deferred = type_checking_statements(tree)

    by_target: dict[str, Edge] = {}

    def add(target: str, kind: str, names: list[str] | None = None) -> None:
        hit = _resolve(target, index)
        if hit is None:
            unresolved.append(target)
            return
        add_hit(hit, kind, names)

    def add_hit(hit: str | None, kind: str, names: list[str] | None = None) -> bool:
        if hit is None or hit == rel_path:
            return False
        key = (rel_path, hit)
        if key in seen:
            # Two import statements can name the same file; the symbols from
            # both belong on the one edge.
            edge = by_target[hit]
            for n in names or []:
                if n not in edge.names:
                    edge.names.append(n)
            return True
        seen.add(key)
        edge = Edge(rel_path, hit, kind, list(names or []))
        by_target[hit] = edge
        edges.append(edge)
        return True

    for node in iter_statements(tree):
        guarded = id(node) in deferred

        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, "type_checking" if guarded else "import")

        elif isinstance(node, ast.ImportFrom):
            # Resolve the dotted package the names are being imported FROM.
            # For a relative import that means walking up from this file's own
            # package by one level per leading dot.
            if node.level and node.level > 0:
                parts = pkg.split(".") if pkg else []
                up = node.level - 1
                if up:
                    parts = parts[: len(parts) - up]
                base = ".".join(p for p in parts if p)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
                kind = "relative"
            else:
                base = node.module or ""
                kind = "from"
            if guarded:
                kind = "type_checking"

            if not base and not node.names:
                continue

            # `from X import a` is ambiguous: `a` may be a SUBMODULE of package
            # X, or an attribute defined inside module X. The submodule is a
            # real file-level dependency, so it wins when one exists -- without
            # this, `from . import b` resolves to the package __init__ and the
            # edge to b.py is lost entirely.
            resolved_any = False
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in index and add_hit(index[candidate], kind, [alias.name]):
                    resolved_any = True

            if not resolved_any:
                hit = _resolve(base, index) if base else None
                if hit is not None:
                    add_hit(hit, kind, [a.name for a in node.names])
                elif base:
                    unresolved.append(base)

    return edges, unresolved


@dataclass
class SourceIndex:
    """Name -> file, kept separate per language.

    One flat index would be wrong: a Java class `Invoice`, a Python module
    `invoice` and a C++ header `invoice.h` are different namespaces that
    happen to share spellings. Merging them would invent edges between files
    that have never heard of each other.
    """

    python: dict[str, str] = field(default_factory=dict)
    by_lang: dict[str, dict[str, str]] = field(default_factory=dict)


def build_index(paths: list[str], sources: dict[str, str], trees: dict[str, Any]) -> SourceIndex:
    from .. import lang as _lang
    from ..lang import imports as _imports

    py_paths = [p for p in paths if _lang.is_python(p)]
    index = SourceIndex(python=build_module_index(py_paths))

    for rel in paths:
        spec = _lang.spec_for(rel)
        if spec is None:
            continue
        table = index.by_lang.setdefault(spec.name, {})
        keys = _imports.index_keys(spec, rel.replace("\\", "/"),
                                   sources.get(rel, ""), trees.get(rel))
        for key in keys:
            # First writer wins, and the keys come most-specific first, so a
            # full path always beats a bare filename shared by two headers.
            table.setdefault(key, rel)
    return index


def extract_imports_any(
    rel_path: str,
    source: str,
    index: SourceIndex,
    source_root: str = "",
    tree: Any = None,
) -> tuple[list[Edge], list[str]]:
    """Language-agnostic front door. Never raises."""
    from .. import lang as _lang
    from ..lang import imports as _imports

    if _lang.is_python(rel_path):
        return extract_imports(rel_path, source, index.python, source_root, tree)

    spec = _lang.spec_for(rel_path)
    if spec is None or tree is None:
        return [], []

    rel = rel_path.replace("\\", "/")
    table = index.by_lang.get(spec.name, {})
    edges: list[Edge] = []
    unresolved: list[str] = []
    seen: set[tuple[str, str]] = set()

    try:
        found = _imports.targets(spec, tree, source)
    except Exception:
        return [], []

    for raw, kind in found:
        hit = _imports.resolve(spec, raw, rel, table)
        if hit is None or hit == rel:
            if hit is None:
                unresolved.append(raw)
            continue
        key = (rel, hit)
        if key in seen:
            continue
        seen.add(key)
        edges.append(Edge(rel, hit, kind))

    return edges, unresolved


def parse_all(
    root: str | Path, paths: list[str], overrides: dict[str, str] | None = None
) -> tuple[dict[str, str], dict[str, Any]]:
    """Read and parse every file once, with the right parser for each.

    `overrides` supplies source text instead of reading from disk -- the
    gatekeeper needs it to judge STAGED content rather than the working tree.
    """
    import ast as _ast
    from .. import lang as _lang
    from ..lang import treesitter as _ts

    overrides = overrides or {}
    root = Path(root)
    sources: dict[str, str] = {}
    trees: dict[str, Any] = {}

    for rel in paths:
        if rel in overrides:
            src = overrides[rel]
        else:
            try:
                src = (root / rel).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
        src = src.lstrip("\ufeff")
        sources[rel] = src

        if _lang.is_python(rel):
            try:
                trees[rel] = _ast.parse(src, filename=rel)
            except (SyntaxError, ValueError, RecursionError):
                pass
            continue

        spec = _lang.spec_for(rel)
        if spec is not None:
            tree = _ts.parse(spec, src)
            if tree is not None:
                trees[rel] = tree

    return sources, trees


def build_graph(
    root: str | Path, paths: list[str], overrides: dict[str, str] | None = None
) -> DependencyGraph:
    """The whole dependency graph, across every supported language.

    Edges only ever join two files in the same language. A Java class cannot
    import a C++ header, and pretending otherwise would be noise.
    """
    paths = [p.replace("\\", "/") for p in paths]
    sources, trees = parse_all(root, paths, overrides)
    index = build_index(paths, sources, trees)
    pathset = set(paths)
    graph = DependencyGraph(nodes=sorted(paths))

    for rel in paths:
        if rel not in sources:
            continue
        edges, unresolved = extract_imports_any(
            rel, sources[rel], index, source_root_for(rel, pathset), trees.get(rel)
        )
        graph.edges.extend(edges)
        for name in unresolved:
            top = name.split(".")[0].split("/")[0]
            graph.unresolved[top] = graph.unresolved.get(top, 0) + 1

    return graph


def build_graph_with_overrides(
    root: str | Path, paths: list[str], overrides: dict[str, str]
) -> DependencyGraph:
    """Same as build_graph, but `overrides` supplies source text for selected
    paths instead of reading from disk.

    The gatekeeper needs this: it must build the graph from the STAGED content
    of modified files, not from the working tree, which may have moved on.
    """
    return build_graph(root, paths, overrides)
