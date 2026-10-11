"""The analysis pipeline, in one place.

`repohealth scan` and the app's Scan button must produce byte-identical results,
so both call this. The CLI passes a printing callback; the app passes one that
updates its progress state. Neither owns the order of the phases.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable

from .analysis.blast import blast_radius, rank_fragile, risk_level
from .analysis.complexity import analyze_source
from .analysis.cycles import find_cycles
from .analysis.depgraph import (
    DependencyGraph, build_index, extract_imports_any, parse_all, source_root_for,
)
from .analysis.scoring import compute_health
from .db.store import Store
from .ingest.git_log import iter_commits, open_repo, source_files

# phase key -> human-readable label, in execution order
PHASES = [
    ("clone", "Downloading the repository"),
    ("history", "Reading commit history"),
    ("complexity", "Measuring complexity"),
    ("graph", "Building the dependency graph"),
    ("cycles", "Detecting circular imports"),
    ("risk", "Ranking risk"),
]

Progress = Callable[[str, str], None]


def _noop(phase: str, message: str) -> None:
    pass


def run_scan(repo_path: str | Path, on_progress: Progress | None = None) -> dict[str, Any]:
    """Full analysis. Returns a summary dict; raises on a genuinely broken repo."""
    report = on_progress or _noop
    repo = open_repo(repo_path)
    root = Path(repo.working_tree_dir)

    from .config import load_config
    cfg = load_config(root)

    with Store(root / cfg["db_path"]) as store:
        repo_id = store.upsert_repo(str(root), root.name)

        report("history", "Reading commit history")
        commits = list(
            iter_commits(repo, limit=cfg["history_limit"], exclude=cfg["exclude_paths"])
        )
        new_commits = store.insert_commits(repo_id, commits)

        languages = set(cfg.get("languages") or []) or None
        files = source_files(repo, cfg["exclude_paths"], languages)
        run_id = store.start_run(repo_id, store.latest_commit_sha(repo_id), kind="full")

        # Read and parse each file exactly once, with the parser its language
        # needs, then hand the same tree to both engines. Parsing is about half
        # the cost of a scan, and doing it separately in each engine was the
        # single largest expense on a large repository.
        report("complexity", f"Measuring complexity across {len(files)} files")
        sources, trees = parse_all(root, files)
        metrics = []
        for rel in files:
            if rel not in sources:
                m = analyze_source(rel, "", cfg["thresholds"])
                m.parsed = False
                m.parse_error = "unreadable"
                metrics.append(m)
                continue
            metrics.append(
                analyze_source(rel, sources[rel], cfg["thresholds"], tree=trees.get(rel))
            )

        store.insert_file_metrics(run_id, [m.as_row() for m in metrics])
        unparsed = [(m.path, m.parse_error) for m in metrics if not m.parsed]

        report("graph", "Building the dependency graph")
        index = build_index(files, sources, trees)
        pathset = {p.replace("\\", "/") for p in files}
        graph = DependencyGraph(nodes=sorted(files))
        for rel in files:
            if rel not in sources:
                continue
            edges, _ = extract_imports_any(
                rel, sources[rel], index, source_root_for(rel, pathset), trees.get(rel)
            )
            graph.edges.extend(edges)
        trees.clear()   # a few thousand parse trees is real memory; drop them
        sources.clear()
        store.insert_dependencies(run_id, graph.edges)
        adj, rev = graph.adjacency(), graph.reverse_adjacency()

        report("cycles", "Detecting circular imports")
        cycles = find_cycles(adj)
        store.insert_cycles(run_id, cycles)

        report("risk", "Ranking risk")
        blast = blast_radius(rev)
        churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
        cc = {m.path: m.cc_total for m in metrics}
        ranked = rank_fragile(blast, churn, cc, top=len(files) or 1)
        scores = [int(r["risk_score"]) for r in ranked]
        for r in ranked:
            r["risk_level"] = risk_level(int(r["risk_score"]), scores)
        store.insert_risk(run_id, ranked)

        breakdown = compute_health(
            [m.as_row() for m in metrics], churn,
            {r["path"]: r["blast_radius"] for r in ranked}, cycles, cfg,
        )
        store.insert_health(run_id, breakdown.as_dict())
        store.finish_run(run_id)

    by_language: dict[str, int] = {}
    for m in metrics:
        by_language[m.language] = by_language.get(m.language, 0) + 1

    return {
        "run_id": run_id,
        "languages": dict(sorted(by_language.items(), key=lambda kv: -kv[1])),
        "commits_scanned": len(commits),
        "commits_new": new_commits,
        "files": len(files),
        "unparsed": unparsed,
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "cycles": cycles,
        "health": breakdown,
    }
