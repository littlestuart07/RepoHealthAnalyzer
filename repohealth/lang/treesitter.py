"""Measurement for every non-Python language, in one algorithm.

Python keeps its own engine: the standard library already ships an exact parser
for it, radon is a well-tested complexity implementation, and throwing that away
to gain uniformity would be a downgrade. Everything else goes through
tree-sitter, which supplies a real parse tree per language rather than a regular
expression pretending to be one.

The algorithm is the same one the Python engine uses, expressed over
tree-sitter node types instead of `ast` classes:

  * cyclomatic complexity of a function is 1 + the number of decision nodes
    inside it, short-circuit operators included, nested functions excluded;
  * nesting depth restarts at each function boundary;
  * a file's complexity is the sum over its functions, plus one block for
    top-level code if any exists.

One parser instance per language is created on first use and reused: building
one is expensive and they are not cheap to churn through on a large repository.
"""

from __future__ import annotations

from typing import Any

from .specs import LanguageSpec

_PARSERS: dict[str, Any] = {}
_IMPORT_ERROR: str | None = None


def parser_for(spec: LanguageSpec):
    """A cached tree-sitter parser, or None when the grammars are not installed.

    RepoHealth stays usable on Python-only repositories without the grammar
    package, so a missing import is reported rather than fatal.
    """
    global _IMPORT_ERROR
    if spec.name in _PARSERS:
        return _PARSERS[spec.name]
    if _IMPORT_ERROR is not None:
        return None
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        _IMPORT_ERROR = (
            f"{exc}. Install the grammars with: pip install tree-sitter-language-pack"
        )
        return None
    try:
        p = get_parser(spec.name)
    except Exception as exc:          # unknown grammar name, broken wheel
        _IMPORT_ERROR = f"grammar '{spec.name}' unavailable: {exc}"
        return None
    _PARSERS[spec.name] = p
    return p


def grammar_error() -> str | None:
    return _IMPORT_ERROR


def parse(spec: LanguageSpec, source: str):
    """Parse to a tree, or None if the grammars are unavailable.

    tree-sitter never raises on malformed input -- it produces a tree containing
    ERROR nodes and carries on, which is the behaviour we want: half a broken
    file still yields usable measurements.
    """
    p = parser_for(spec)
    if p is None:
        return None
    return p.parse(source.encode("utf-8", errors="replace"))


def _name_of(node) -> str:
    """Best-effort function name. Grammars disagree on where it lives."""
    for field in ("name", "declarator"):
        child = node.child_by_field_name(field)
        while child is not None:
            if child.type in ("identifier", "field_identifier", "property_identifier",
                              "type_identifier", "qualified_identifier"):
                return child.text.decode("utf-8", errors="replace")
            nxt = child.child_by_field_name("declarator")
            if nxt is None:
                nxt = next((c for c in child.children
                            if c.type.endswith("identifier")), None)
            if nxt is child:
                break
            child = nxt
    return "<anonymous>"


class _Block:
    """One measured unit: a function, or the file's top level."""

    __slots__ = ("name", "line", "complexity", "depth", "max_depth")

    def __init__(self, name: str, line: int):
        self.name = name
        self.line = line
        self.complexity = 1
        self.depth = 0
        self.max_depth = 0


def measure(spec: LanguageSpec, tree) -> dict[str, Any]:
    """Walk once, collecting every metric.

    Iterative rather than recursive: a minified JavaScript bundle nests deeply
    enough to blow the interpreter's stack, and a crash on one file would take
    the whole scan with it.
    """
    module = _Block("<module>", 1)
    blocks: list[_Block] = []
    num_classes = 0
    max_class_loc = 0

    # (node, phase) -- 0 is "entering", 1 is "leaving". The leave pass is what
    # lets nesting depth and the current function be unwound correctly.
    stack: list[tuple[Any, int]] = [(tree.root_node, 0)]
    fn_stack: list[_Block] = []

    while stack:
        node, phase = stack.pop()
        t = node.type

        if phase == 1:
            if t in spec.functions:
                done = fn_stack.pop()
                done.max_depth = max(done.max_depth, done.depth)
                blocks.append(done)
            elif t in spec.nesting:
                cur = fn_stack[-1] if fn_stack else module
                cur.depth -= 1
            continue

        current = fn_stack[-1] if fn_stack else module

        if t in spec.classes:
            num_classes += 1
            span = node.end_point[0] - node.start_point[0] + 1
            max_class_loc = max(max_class_loc, span)

        if t in spec.functions:
            blk = _Block(_name_of(node), node.start_point[0] + 1)
            fn_stack.append(blk)
            stack.append((node, 1))
            current = blk
        else:
            if t in spec.decisions:
                current.complexity += 1
            elif t == spec.binary_node and spec.boolean_ops:
                op = node.child_by_field_name("operator")
                if op is not None and op.type in spec.boolean_ops:
                    current.complexity += 1

            if t in spec.nesting:
                current.depth += 1
                current.max_depth = max(current.max_depth, current.depth)
                stack.append((node, 1))

        # Children pushed in reverse so they pop in source order; the order
        # only affects which hotspot is reported first, but a stable report is
        # worth the one-line cost.
        for child in reversed(node.children):
            if child.type in spec.comment_nodes or not child.is_named:
                continue
            stack.append((child, 0))

    while fn_stack:                                  # unbalanced tree (ERROR nodes)
        blk = fn_stack.pop()
        blk.max_depth = max(blk.max_depth, blk.depth)
        blocks.append(blk)

    if module.complexity > 1:
        blocks.append(module)

    return {
        "blocks": [(b.name, b.line, b.complexity, b.max_depth) for b in blocks],
        "num_classes": num_classes,
        "max_class_loc": max_class_loc,
        "has_error": tree.root_node.has_error,
    }
