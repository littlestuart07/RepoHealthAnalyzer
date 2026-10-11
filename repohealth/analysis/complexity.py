"""Static code analysis & complexity engine (SRS 4.2).

Python is measured with the standard library's own parser and radon. Every
other language is measured with tree-sitter, in lang/treesitter.py, by the same
definitions: complexity is 1 + decision points per function, nesting depth
restarts at each function. `analyze_source` picks the engine from the file
extension; nothing above this layer knows which one ran.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit_ast

from .. import lang as _lang
from .depgraph import _STATEMENT_HOLDERS


class _StructureVisitor(ast.NodeVisitor):
    """Collects nesting depth, class sizes, and function/class counts."""

    def generic_visit(self, node: ast.AST) -> None:
        """Recurse into statements only.

        The default implementation visits every child node, including every
        name and constant in every expression. Nesting depth, class spans and
        function counts are all properties of statements, so the expression
        subtrees are pure cost -- the dominant cost, on a large repository.
        """
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, _STATEMENT_HOLDERS):
                        self.visit(item)
            elif isinstance(value, _STATEMENT_HOLDERS):
                self.visit(value)

    def __init__(self) -> None:
        self.depth = 0
        self.max_nesting = 0
        self.num_functions = 0
        self.num_classes = 0
        self.max_class_loc = 0
        self.deep_functions: list[tuple[str, int, int]] = []
        self._fn_stack: list[list[Any]] = []

    def _descend(self, node: ast.AST) -> None:
        self.depth += 1
        self.max_nesting = max(self.max_nesting, self.depth)
        if self._fn_stack:
            self._fn_stack[-1][2] = max(self._fn_stack[-1][2], self.depth)
        self.generic_visit(node)
        self.depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.num_classes += 1
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        self.max_class_loc = max(self.max_class_loc, end - node.lineno + 1)
        self.generic_visit(node)

    def _visit_function(self, node) -> None:
        self.num_functions += 1
        self._fn_stack.append([node.name, node.lineno, 0])
        saved, self.depth = self.depth, 0
        self.generic_visit(node)
        self.depth = saved
        name, lineno, depth = self._fn_stack.pop()
        self.max_nesting = max(self.max_nesting, depth)
        self.deep_functions.append((name, lineno, depth))

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_If(self, node):        self._descend(node)
    def visit_For(self, node):       self._descend(node)
    def visit_AsyncFor(self, node):  self._descend(node)
    def visit_While(self, node):     self._descend(node)
    def visit_With(self, node):      self._descend(node)
    def visit_AsyncWith(self, node): self._descend(node)
    def visit_Try(self, node):       self._descend(node)


@dataclass
class FileMetrics:
    path: str
    language: str = "Python"
    cc_total: int = 0
    cc_max: int = 0
    cc_avg: float = 0.0
    loc: int = 0
    num_functions: int = 0
    num_classes: int = 0
    max_class_loc: int = 0
    max_nesting: int = 0
    parsed: bool = True
    parse_error: str | None = None
    hotspots: list[dict[str, Any]] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.pop("hotspots", None)
        return d


def analyze_source(
    rel_path: str,
    source: str,
    thresholds: dict[str, Any] | None = None,
    tree: ast.AST | None = None,
) -> FileMetrics:
    """Measure one file.

    `tree` lets a caller that has already parsed the source hand the parse tree
    over. Parsing is roughly half the cost of analysing a file, and the
    dependency graph needs the same tree, so sharing it halves a full scan.

    The tree's type follows the language: an `ast.Module` for Python, a
    tree-sitter Tree for everything else.
    """
    spec = _lang.spec_for(rel_path)
    if spec is not None:
        return _analyze_with_grammar(rel_path, source, spec, thresholds, tree)
    # Windows editors -- PowerShell, Notepad, Visual Studio -- write UTF-8 with
    # a byte-order mark by default. Decoded, it arrives as U+FEFF and ast.parse
    # rejects the whole file. Python's own tokenizer strips it, so we do too;
    # without this, a Windows-authored repository reports zero dependencies and
    # a perfect health score while silently failing to parse every file.
    source = source.lstrip("\ufeff")
    thresholds = thresholds or {}
    max_cc = thresholds.get("max_cc_per_function", 10)
    max_depth = thresholds.get("max_nesting_depth", 4)
    max_class = thresholds.get("max_class_loc", 200)

    m = FileMetrics(path=rel_path, loc=len(source.splitlines()))

    if tree is None:
        try:
            tree = ast.parse(source, filename=rel_path)
        except (SyntaxError, ValueError, RecursionError) as exc:
            m.parsed = False
            m.parse_error = f"{type(exc).__name__}: {exc}"
            return m

    visitor = _StructureVisitor()
    try:
        visitor.visit(tree)
    except RecursionError:
        m.parsed = False
        m.parse_error = "RecursionError while walking AST"
        return m

    m.num_functions = visitor.num_functions
    m.num_classes = visitor.num_classes
    m.max_class_loc = visitor.max_class_loc
    m.max_nesting = visitor.max_nesting

    try:
        blocks = cc_visit_ast(tree)
    except Exception as exc:
        blocks = []
        m.parse_error = f"radon: {type(exc).__name__}"

    if blocks:
        scores = [b.complexity for b in blocks]
        m.cc_total = sum(scores)
        m.cc_max = max(scores)
        m.cc_avg = round(m.cc_total / len(scores), 2)
        for b in blocks:
            if b.complexity > max_cc:
                m.hotspots.append(
                    {"kind": "complexity", "name": b.name, "line": b.lineno,
                     "value": b.complexity, "limit": max_cc}
                )

    for name, lineno, depth in visitor.deep_functions:
        if depth > max_depth:
            m.hotspots.append(
                {"kind": "nesting", "name": name, "line": lineno,
                 "value": depth, "limit": max_depth}
            )
    if visitor.max_class_loc > max_class:
        m.hotspots.append(
            {"kind": "large_class", "name": "<class>", "line": 0,
             "value": visitor.max_class_loc, "limit": max_class}
        )
    return m


def _analyze_with_grammar(
    rel_path: str,
    source: str,
    spec,
    thresholds: dict[str, Any] | None,
    tree=None,
) -> FileMetrics:
    """The tree-sitter path: C++, Java, JavaScript, TypeScript.

    Thresholds are the same ones Python is held to. That is a deliberate
    choice rather than an oversight -- a function with twelve branches is hard
    to test in any language, and per-language limits would make the score
    incomparable across a mixed repository. Teams that disagree can override
    them in config.json.
    """
    from ..lang import treesitter as ts

    thresholds = thresholds or {}
    max_cc = thresholds.get("max_cc_per_function", 10)
    max_depth = thresholds.get("max_nesting_depth", 4)
    max_class = thresholds.get("max_class_loc", 200)

    m = FileMetrics(
        path=rel_path, language=spec.label, loc=len(source.splitlines())
    )

    if tree is None:
        tree = ts.parse(spec, source)
    if tree is None:
        m.parsed = False
        m.parse_error = ts.grammar_error() or "no grammar available"
        return m

    try:
        data = ts.measure(spec, tree)
    except RecursionError:
        m.parsed = False
        m.parse_error = "RecursionError while walking the parse tree"
        return m

    blocks = data["blocks"]
    m.num_functions = sum(1 for b in blocks if b[0] != "<module>")
    m.num_classes = data["num_classes"]
    m.max_class_loc = data["max_class_loc"]

    if blocks:
        scores = [b[2] for b in blocks]
        m.cc_total = sum(scores)
        m.cc_max = max(scores)
        m.cc_avg = round(m.cc_total / len(scores), 2)
        m.max_nesting = max(b[3] for b in blocks)

    for name, line, complexity, depth in blocks:
        if complexity > max_cc:
            m.hotspots.append(
                {"kind": "complexity", "name": name, "line": line,
                 "value": complexity, "limit": max_cc}
            )
        if depth > max_depth:
            m.hotspots.append(
                {"kind": "nesting", "name": name, "line": line,
                 "value": depth, "limit": max_depth}
            )
    if m.max_class_loc > max_class:
        m.hotspots.append(
            {"kind": "large_class", "name": "<class>", "line": 0,
             "value": m.max_class_loc, "limit": max_class}
        )

    # A syntax error does not stop tree-sitter -- it recovers and carries on.
    # The measurements are still usable, so the file counts as parsed; the
    # note is kept so a report can say the numbers may be understated.
    if data["has_error"]:
        m.parse_error = "recovered from syntax errors; metrics may be understated"
    return m


def analyze_file(root: str | Path, rel_path: str, thresholds: dict[str, Any] | None = None) -> FileMetrics:
    full = Path(root) / rel_path
    try:
        source = full.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return FileMetrics(path=rel_path, parsed=False, parse_error=f"unreadable: {exc}")
    return analyze_source(rel_path, source, thresholds)


def analyze_paths(root: str | Path, paths: list[str], thresholds: dict[str, Any] | None = None) -> list[FileMetrics]:
    return [analyze_file(root, p, thresholds) for p in paths]
