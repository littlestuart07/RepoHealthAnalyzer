"""Per-language grammar facts.

Everything that differs between C++, Java and JavaScript is a set of node-type
names. The measurement algorithm itself is identical for all three, so it lives
once in treesitter.py and reads these tables.

The names are tree-sitter node types, taken from the grammars themselves rather
than guessed -- `node.type` strings as the parser actually emits them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguageSpec:
    name: str                       # tree-sitter grammar name
    label: str                      # what a human calls it
    extensions: tuple[str, ...]

    # A node that owns a callable body. Complexity is reported per function,
    # and nesting depth restarts at each one.
    functions: frozenset[str]

    # A node that adds one independent path through the code. This is the
    # cyclomatic complexity count: 1 + the number of these inside a function.
    decisions: frozenset[str]

    # A node that opens a new level of nesting. Narrower than `decisions`:
    # a ternary adds a path but not a level, and a `case` label sits inside
    # the switch that already counted.
    nesting: frozenset[str]

    classes: frozenset[str] = frozenset()

    # Short-circuit operators count as decisions, but they arrive as a generic
    # binary_expression whose operator token has to be inspected.
    boolean_ops: frozenset[str] = frozenset()
    binary_node: str = "binary_expression"

    # Nodes carrying a dependency on another file.
    imports: frozenset[str] = frozenset()

    comment_nodes: frozenset[str] = field(
        default_factory=lambda: frozenset({"comment", "line_comment", "block_comment"})
    )


CPP = LanguageSpec(
    name="cpp",
    label="C++",
    extensions=(".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h", ".c"),
    functions=frozenset({"function_definition", "lambda_expression"}),
    decisions=frozenset({
        "if_statement", "for_statement", "for_range_loop", "while_statement",
        "do_statement", "case_statement", "catch_clause", "conditional_expression",
        "seh_except_clause",
    }),
    nesting=frozenset({
        "if_statement", "for_statement", "for_range_loop", "while_statement",
        "do_statement", "switch_statement", "try_statement", "catch_clause",
    }),
    classes=frozenset({"class_specifier", "struct_specifier"}),
    boolean_ops=frozenset({"&&", "||", "and", "or"}),
    imports=frozenset({"preproc_include"}),
)

JAVA = LanguageSpec(
    name="java",
    label="Java",
    extensions=(".java",),
    functions=frozenset({
        "method_declaration", "constructor_declaration", "compact_constructor_declaration",
        "lambda_expression",
    }),
    decisions=frozenset({
        "if_statement", "for_statement", "enhanced_for_statement", "while_statement",
        "do_statement", "switch_label", "catch_clause", "ternary_expression",
    }),
    nesting=frozenset({
        "if_statement", "for_statement", "enhanced_for_statement", "while_statement",
        "do_statement", "switch_expression", "switch_statement", "try_statement",
        "catch_clause", "synchronized_statement",
    }),
    classes=frozenset({
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration",
    }),
    boolean_ops=frozenset({"&&", "||"}),
    imports=frozenset({"import_declaration"}),
)

JAVASCRIPT = LanguageSpec(
    name="javascript",
    label="JavaScript",
    extensions=(".js", ".jsx", ".mjs", ".cjs"),
    functions=frozenset({
        "function_declaration", "function_expression", "function", "arrow_function",
        "method_definition", "generator_function", "generator_function_declaration",
    }),
    decisions=frozenset({
        "if_statement", "for_statement", "for_in_statement", "for_of_statement",
        "while_statement", "do_statement", "switch_case", "catch_clause",
        "ternary_expression",
    }),
    nesting=frozenset({
        "if_statement", "for_statement", "for_in_statement", "for_of_statement",
        "while_statement", "do_statement", "switch_statement", "try_statement",
        "catch_clause",
    }),
    classes=frozenset({"class_declaration", "class"}),
    # ?? is a decision in the same sense as || -- it introduces a second path.
    boolean_ops=frozenset({"&&", "||", "??"}),
    imports=frozenset({"import_statement", "export_statement", "call_expression"}),
)

TYPESCRIPT = LanguageSpec(
    name="typescript",
    label="TypeScript",
    extensions=(".ts", ".mts", ".cts"),
    functions=JAVASCRIPT.functions | frozenset({"function_signature"}),
    decisions=JAVASCRIPT.decisions,
    nesting=JAVASCRIPT.nesting,
    classes=JAVASCRIPT.classes,
    boolean_ops=JAVASCRIPT.boolean_ops,
    imports=JAVASCRIPT.imports,
)

TSX = LanguageSpec(
    name="tsx",
    label="TypeScript",
    extensions=(".tsx",),
    functions=TYPESCRIPT.functions,
    decisions=TYPESCRIPT.decisions,
    nesting=TYPESCRIPT.nesting,
    classes=TYPESCRIPT.classes,
    boolean_ops=TYPESCRIPT.boolean_ops,
    imports=TYPESCRIPT.imports,
)

SPECS: tuple[LanguageSpec, ...] = (CPP, JAVA, JAVASCRIPT, TYPESCRIPT, TSX)
