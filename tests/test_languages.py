"""Multi-language measurement and dependency resolution.

Each language gets the same two questions asked of it: are the complexity
numbers right, and does an import resolve to the file it actually names. The
resolution cases are the ones that are easy to get wrong -- a quoted C++
include relative to the including file, a Java class found by package rather
than directory, a JavaScript directory standing for its index file.
"""

from __future__ import annotations

import pytest

from repohealth import lang
from repohealth.analysis.complexity import analyze_source
from repohealth.analysis.cycles import find_cycles
from repohealth.analysis.depgraph import build_graph

pytestmark = pytest.mark.skipif(
    lang.spec_for("x.java") is None, reason="language specs unavailable"
)


def _needs_grammars():
    from repohealth.lang import treesitter as ts
    return ts.parse(lang.spec_for("x.java"), "class A {}") is None


skip_no_grammar = pytest.mark.skipif(
    _needs_grammars(), reason="tree-sitter grammars not installed"
)


# --------------------------------------------------------------- registry

def test_extension_maps_to_language():
    assert lang.label_for("src/main.cpp") == "C++"
    assert lang.label_for("src/core.h") == "C++"
    assert lang.label_for("com/acme/Invoice.java") == "Java"
    assert lang.label_for("web/app.js") == "JavaScript"
    assert lang.label_for("web/app.tsx") == "TypeScript"
    assert lang.label_for("pipeline.py") == "Python"
    assert lang.label_for("README.md") == "unknown"


def test_unknown_extensions_are_not_sources():
    assert not lang.is_source("notes.txt")
    assert not lang.is_source("image.png")
    assert lang.is_source("a.cpp")


def test_language_filter_narrows():
    assert lang.is_source("a.java", {"Java"})
    assert not lang.is_source("a.java", {"Python"})


# --------------------------------------------------------------- complexity

@skip_no_grammar
def test_java_complexity_counts_short_circuit_operators():
    simple = analyze_source("A.java", "class A { int f(int a) { return a; } }")
    withand = analyze_source(
        "A.java", "class A { int f(int a, int b) { return a > 0 && b > 0 ? 1 : 0; } }"
    )
    # one ternary + one && over the baseline of 1
    assert withand.cc_total == simple.cc_total + 2


@skip_no_grammar
def test_nesting_depth_restarts_at_each_function():
    """Two shallow functions must not add up to one deep one."""
    src = """
    class A {
      void f() { if (1 > 0) { while (true) { break; } } }
      void g() { if (1 > 0) { while (true) { break; } } }
    }
    """
    m = analyze_source("A.java", src)
    assert m.max_nesting == 2
    assert m.num_functions == 2


@skip_no_grammar
def test_function_names_reach_the_hotspot_report():
    src = """
    class A {
      int tangled(int a, int b) {
        if (a > 0) { for (int i = 0; i < a; i++) { while (b > 0) { try { b--; }
          catch (Exception e) { if (a > 1) { a--; } } } } }
        return a;
      }
    }
    """
    m = analyze_source("A.java", src, {"max_nesting_depth": 3})
    deep = [h for h in m.hotspots if h["kind"] == "nesting"]
    assert deep and deep[0]["name"] == "tangled"
    assert deep[0]["line"] > 0


@skip_no_grammar
def test_broken_syntax_still_yields_metrics():
    """tree-sitter recovers; a half-broken file must not abort the scan."""
    m = analyze_source("A.java", "class A { int f( { if (1) { } } ")
    assert m.parsed
    assert "recovered" in (m.parse_error or "")


@skip_no_grammar
def test_cpp_and_js_measure_their_own_constructs():
    cpp = analyze_source("a.cpp", "int f(int a){ return a ? 1 : 0; }")
    js = analyze_source("a.js", "function f(a){ return a ?? 1; }")
    assert cpp.language == "C++" and cpp.cc_total == 2
    assert js.language == "JavaScript" and js.cc_total == 2


# --------------------------------------------------------------- resolution

@skip_no_grammar
def test_cpp_quoted_include_resolves_next_to_the_including_file(tmp_path):
    """Two headers share a name; the one in the same directory must win."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "util.h").write_text("int a();")
    (tmp_path / "b" / "util.h").write_text("int b();")
    (tmp_path / "b" / "use.cpp").write_text('#include "util.h"\nint main(){return 0;}')

    g = build_graph(tmp_path, ["a/util.h", "b/util.h", "b/use.cpp"])
    assert [(e.from_path, e.to_path) for e in g.edges] == [("b/use.cpp", "b/util.h")]


@skip_no_grammar
def test_cpp_system_include_is_not_an_edge(tmp_path):
    (tmp_path / "m.cpp").write_text("#include <vector>\nint main(){return 0;}")
    g = build_graph(tmp_path, ["m.cpp"])
    assert g.edges == []


@skip_no_grammar
def test_java_resolves_by_package_not_by_directory(tmp_path):
    """The file sits in a src/main/java tree; the import names only the package."""
    deep = tmp_path / "src" / "main" / "java" / "com" / "acme"
    deep.mkdir(parents=True)
    (deep / "Ledger.java").write_text("package com.acme;\npublic class Ledger {}")
    (deep / "Invoice.java").write_text(
        "package com.acme;\nimport com.acme.Ledger;\npublic class Invoice {}"
    )
    paths = [
        "src/main/java/com/acme/Ledger.java",
        "src/main/java/com/acme/Invoice.java",
    ]
    g = build_graph(tmp_path, paths)
    assert [(e.from_path, e.to_path) for e in g.edges] == [(paths[1], paths[0])]


@skip_no_grammar
def test_java_stdlib_import_is_not_an_edge(tmp_path):
    (tmp_path / "A.java").write_text("import java.util.List;\npublic class A {}")
    g = build_graph(tmp_path, ["A.java"])
    assert g.edges == []


@skip_no_grammar
def test_js_directory_import_resolves_to_index(tmp_path):
    (tmp_path / "util").mkdir()
    (tmp_path / "util" / "index.js").write_text("export const x = 1;")
    (tmp_path / "app.js").write_text('import { x } from "./util";')
    g = build_graph(tmp_path, ["util/index.js", "app.js"])
    assert [(e.from_path, e.to_path) for e in g.edges] == [("app.js", "util/index.js")]


@skip_no_grammar
def test_js_require_and_bare_specifier(tmp_path):
    (tmp_path / "cart.js").write_text("module.exports = {};")
    (tmp_path / "app.js").write_text(
        'const c = require("./cart.js");\nconst react = require("react");'
    )
    g = build_graph(tmp_path, ["cart.js", "app.js"])
    # the relative require is an edge; "react" lives in node_modules and is not
    assert [(e.from_path, e.to_path) for e in g.edges] == [("app.js", "cart.js")]


@skip_no_grammar
def test_edges_never_cross_languages(tmp_path):
    """A Java class and a C++ header with the same stem must stay unconnected."""
    (tmp_path / "Ledger.java").write_text("public class Ledger {}")
    (tmp_path / "Ledger.h").write_text("#pragma once")
    (tmp_path / "use.cpp").write_text('#include "Ledger.h"')
    g = build_graph(tmp_path, ["Ledger.java", "Ledger.h", "use.cpp"])
    assert [(e.from_path, e.to_path) for e in g.edges] == [("use.cpp", "Ledger.h")]


@skip_no_grammar
def test_cycle_detection_works_per_language(tmp_path):
    (tmp_path / "a.js").write_text('import "./b.js";')
    (tmp_path / "b.js").write_text('import "./a.js";')
    g = build_graph(tmp_path, ["a.js", "b.js"])
    cycles = find_cycles(g.adjacency())
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a.js", "b.js"}


def test_concurrent_opens_survive_the_language_migration(tmp_path):
    """The dashboard opens seven connections at once on page load.

    All of them read the pre-migration schema, all of them try to add the
    `language` column, and only one can win. The losers must treat
    "duplicate column name" as success rather than taking the request down.
    """
    import sqlite3
    import threading

    from repohealth.db.store import Store

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE file_metrics (metric_id INTEGER PRIMARY KEY,"
        " run_id INTEGER, path TEXT, cc_total INTEGER);"
    )
    c.commit()
    c.close()

    errors: list[str] = []

    def open_once():
        try:
            Store(db).close()
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=open_once) for _ in range(7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with Store(db) as s:
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(file_metrics)")}
    assert "language" in cols
