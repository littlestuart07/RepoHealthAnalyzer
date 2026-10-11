"""Refactoring advice for circular imports (SRS 4.3.2.3, extension).

Detection tells you a loop exists. That is the easy half. The useful half is
answering the question a developer actually has in front of the report: which
of these four imports do I remove, and what do I replace it with?

This module answers it from data the scan already produces -- the cycle, the
symbols crossing each edge, and where in the importing file those symbols are
used. Nothing here rewrites code. It proposes an edit and shows the diff a
person would make; a tool that silently reorganises someone's modules is a
tool nobody runs twice.

The three repairs, cheapest first:

  type_only   every use is in a type annotation, so the import can move under
              `if TYPE_CHECKING:` and the runtime loop disappears. No code
              moves at all.
  lazy        the symbols are only used inside function bodies, so the import
              can move into the function that needs it. One line moves.
  extract     something is needed at module level, so the shared symbols have
              to come out into a module both sides can depend on. This is the
              real refactor, and the only one that needs judgement.

Confidence is reported honestly: the first two are mechanical and safe, the
third is a proposal a human has to agree with.
"""

from __future__ import annotations

import ast
import posixpath
from dataclasses import dataclass, field
from typing import Any

from .depgraph import DependencyGraph, Edge


@dataclass
class Suggestion:
    cycle: list[str]
    edge_from: str
    edge_to: str
    symbols: list[str]
    strategy: str                 # type_only | lazy | extract
    confidence: str               # high | medium
    rationale: str
    steps: list[str] = field(default_factory=list)
    new_module: str | None = None
    before: str = ""
    after: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------- usage

@dataclass
class _Usage:
    """Where an imported name is actually used in the importing file."""

    at_module_level: bool = False     # needs the import to run at import time
    in_annotations: bool = False
    in_function_bodies: bool = False
    functions: list[str] = field(default_factory=list)


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """id() of every node that sits inside a type annotation.

    Annotations are the one place an import is needed by the type checker and
    not by the interpreter, which is what makes the TYPE_CHECKING repair safe.
    """
    out: set[int] = set()

    def mark(node):
        if node is None:
            return
        for n in ast.walk(node):
            out.add(id(n))

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            mark(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mark(node.returns)
            args = node.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                      args.vararg, args.kwarg]:
                if a is not None:
                    mark(a.annotation)
    return out


def analyse_usage(tree: ast.AST, names: list[str]) -> dict[str, _Usage]:
    """Classify each imported name by where the importing file uses it."""
    wanted = set(names)
    usage = {n: _Usage() for n in wanted}
    if not wanted:
        return usage

    ann = _annotation_nodes(tree)

    # Walk with a stack so each Name can be attributed to its enclosing
    # function, or to module level when there is none.
    stack: list[tuple[Any, list[str]]] = [(tree, [])]
    while stack:
        node, fns = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A decorator or a default value runs at import time even
                # though it is written on a function.
                for d in child.decorator_list:
                    _record(d, wanted, usage, ann, fns, module_level=not fns)
                for d in [*child.args.defaults, *child.args.kw_defaults]:
                    if d is not None:
                        _record(d, wanted, usage, ann, fns, module_level=not fns)
                stack.append((child, fns + [child.name]))
            elif isinstance(child, ast.ClassDef):
                # Base classes are evaluated at import time.
                for b in [*child.bases, *child.keywords]:
                    _record(b, wanted, usage, ann, fns, module_level=not fns)
                stack.append((child, fns))
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                continue                       # the import statement itself
            else:
                _record(child, wanted, usage, ann, fns, module_level=not fns)
                stack.append((child, fns))
    return usage


def _record(node, wanted, usage, ann, fns, module_level: bool) -> None:
    for n in ast.walk(node):
        if not isinstance(n, ast.Name) or n.id not in wanted:
            continue
        u = usage[n.id]
        if id(n) in ann:
            u.in_annotations = True
            continue
        if module_level:
            u.at_module_level = True
        else:
            u.in_function_bodies = True
            if fns and fns[-1] not in u.functions:
                u.functions.append(fns[-1])


# --------------------------------------------------------------- scoring

def _cycle_edges(cycle: list[str], by_pair: dict[tuple[str, str], Edge]) -> list[Edge]:
    out = []
    for i, src in enumerate(cycle):
        dst = cycle[(i + 1) % len(cycle)]
        e = by_pair.get((src, dst))
        if e is not None:
            out.append(e)
    return out


def _shared_module_for(a: str, b: str) -> str:
    """A path both files can import without either importing the other."""
    pa, pb = posixpath.dirname(a).split("/"), posixpath.dirname(b).split("/")
    common = []
    for x, y in zip(pa, pb):
        if x != y:
            break
        common.append(x)
    base = "/".join(c for c in common if c)
    return f"{base}/_shared.py" if base else "_shared.py"


def _plan(edge: Edge, tree: ast.AST | None) -> tuple[str, str, str, list[str]]:
    """(strategy, confidence, rationale, functions) for cutting one edge."""
    if edge.kind == "type_checking":
        # Already deferred by whoever wrote it. There is nothing left to
        # advise, and suggesting it again is how a tool loses trust.
        return ("already_deferred", "high",
                "this import is already under `if TYPE_CHECKING:` and never runs", [])
    if tree is None or not edge.names:
        return ("extract", "medium",
                "the import names a whole module, so the symbols it needs have to "
                "be identified by hand before anything can be moved", [])

    usage = analyse_usage(tree, edge.names)
    live = [n for n in edge.names if n in usage]
    if not live:
        return ("lazy", "medium",
                "no use of these names was found at module level", [])

    module_level = [n for n in live if usage[n].at_module_level]
    annotated = [n for n in live if usage[n].in_annotations]
    fns: list[str] = []
    for n in live:
        for f in usage[n].functions:
            if f not in fns:
                fns.append(f)

    if not module_level and annotated and all(
        usage[n].in_annotations and not usage[n].in_function_bodies for n in live
    ):
        return ("type_only", "high",
                "every use is inside a type annotation, so the interpreter never "
                "needs this import at run time", fns)

    if not module_level:
        where = ", ".join(f"{f}()" for f in fns[:3]) or "function bodies"
        return ("lazy", "high",
                f"these names are only used inside {where}, so the import can move "
                f"into the function that needs it", fns)

    return ("extract", "medium",
            f"{', '.join(module_level[:3])} "
            f"{'is' if len(module_level) == 1 else 'are'} needed at module level, "
            f"so the import cannot be deferred -- the shared code has to move", fns)


# the cost of cutting an edge: fewer symbols and a deferrable import are cheaper
_COST = {"type_only": 0, "lazy": 1, "extract": 10, "already_deferred": 99}


def suggest_for_cycle(
    cycle: list[str],
    by_pair: dict[tuple[str, str], Edge],
    trees: dict[str, Any],
) -> Suggestion | None:
    """One recommendation per cycle: the cheapest edge to cut, and how."""
    edges = _cycle_edges(cycle, by_pair)
    if not edges:
        return None

    scored = []
    for e in edges:
        strategy, confidence, rationale, fns = _plan(e, trees.get(e.from_path))
        cost = _COST[strategy] * 100 + len(e.names or [1])
        scored.append((cost, e, strategy, confidence, rationale, fns))
    scored.sort(key=lambda t: t[0])
    _, edge, strategy, confidence, rationale, fns = scored[0]

    names = edge.names or ["<the names it uses>"]
    joined = ", ".join(names)
    stem = posixpath.basename(edge.to_path).removesuffix(".py")
    # A relative import when the two files share a package, which is how it
    # will already be spelled in the file the developer is about to edit.
    same_pkg = posixpath.dirname(edge.from_path) == posixpath.dirname(edge.to_path)
    target_mod = f".{stem}" if same_pkg else stem
    s = Suggestion(
        cycle=list(cycle), edge_from=edge.from_path, edge_to=edge.to_path,
        symbols=list(names), strategy=strategy, confidence=confidence,
        rationale=rationale,
    )

    if strategy == "already_deferred":
        s.steps = [
            "No action. Every import in this loop is already guarded, so the "
            "cycle exists for a type checker and never at run time.",
        ]
        return s

    if strategy == "type_only":
        s.before = f"from {target_mod} import {joined}"
        s.after = (
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n"
            f"    from {target_mod} import {joined}"
        )
        s.steps = [
            f"In {edge.from_path}, move the import under `if TYPE_CHECKING:`.",
            "Quote the annotations that use it, or add "
            "`from __future__ import annotations` at the top of the file.",
            "Nothing else changes; the loop is gone at run time.",
        ]
    elif strategy == "lazy":
        target_fn = (fns[0] + "()") if fns else "the function that uses it"
        s.before = f"from {target_mod} import {joined}      # at the top of the file"
        s.after = f"def ...:\n    from {target_mod} import {joined}   # inside {target_fn}"
        s.steps = [
            f"In {edge.from_path}, delete the module-level import of {joined}.",
            f"Add the same import as the first line inside {target_fn}.",
            "Re-run `repohealth cycles .` to confirm the loop is gone.",
        ]
    else:
        new_mod = _shared_module_for(edge.from_path, edge.to_path)
        s.new_module = new_mod
        new_name = posixpath.basename(new_mod).removesuffix(".py")
        s.before = (
            f"{edge.from_path}:  from {target_mod} import {joined}\n"
            f"{edge.to_path}:  (imports back from {posixpath.basename(edge.from_path)})"
        )
        s.after = (
            f"{new_mod}:  defines {joined}\n"
            f"{edge.from_path}:  from {new_name} import {joined}\n"
            f"{edge.to_path}:  from {new_name} import {joined}"
        )
        s.steps = [
            f"Create {new_mod}.",
            f"Move the definition of {joined} out of {edge.to_path} into it.",
            f"In {edge.to_path}, import {joined} from {new_name} if it still needs it.",
            f"In {edge.from_path}, import {joined} from {new_name} instead of "
            f"from {target_mod}.",
            f"{new_mod} must import neither file, or the loop simply moves.",
        ]
    return s


def recommend(
    graph: DependencyGraph,
    cycles: list[list[str]],
    trees: dict[str, Any],
    limit: int = 20,
) -> list[Suggestion]:
    """One suggestion per cycle, cheapest repairs first."""
    by_pair = {(e.from_path, e.to_path): e for e in graph.edges}
    out = []
    for cycle in cycles:
        s = suggest_for_cycle(cycle, by_pair, trees)
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: (_COST[s.strategy], len(s.cycle)))
    return out[:limit]
