"""Active Pre-Commit Gatekeeper (SRS 4.4).

Runs on the fast path: staged files only, one indexed baseline lookup each,
and no full-repository graph rebuild unless the cycle check is enabled and the
repository is small enough to afford it. The budget is 1.5s (SRS 5.1.1).

Judgement is made against the STAGED content of each file, obtained via
`git show :path`, not against the working tree -- the developer may have kept
editing after `git add`, and the commit will contain what was staged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..analysis.complexity import FileMetrics, analyze_source
from ..analysis.cycles import find_cycles, format_cycle
from ..analysis.depgraph import build_graph_with_overrides
from ..config import load_config
from ..db.store import Store
from ..ingest.git_log import (
    open_repo,
    source_files,
    staged_blob,
    staged_source_files,
)

# Building the dependency graph is the one genuinely expensive step on this
# path. Above this file count the cycle check is skipped to protect the 1.5s
# budget; `repohealth scan` still reports cycles for such repositories.
CYCLE_CHECK_FILE_LIMIT = 800


@dataclass
class Violation:
    kind: str              # "complexity_spike" | "new_cycle"
    path: str
    message: str
    detail: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    unparsed: list[tuple[str, str]] = field(default_factory=list)
    skipped_reason: str | None = None
    elapsed: float = 0.0
    checked_baselines: int = 0


def _spike_detail(m: FileMetrics, baseline: float, current: int, pct: float, limit: float) -> list[str]:
    """The specific functions a developer should look at first."""
    lines = [
        f"complexity {current} vs baseline {baseline:.1f}  (+{pct:.1f}%, limit +{limit:.0f}%)"
    ]
    worst = sorted(
        (h for h in m.hotspots if h["kind"] == "complexity"),
        key=lambda h: h["value"],
        reverse=True,
    )[:3]
    for h in worst:
        lines.append(
            f"line {h['line']}: {h['name']}() complexity {h['value']} (limit {h['limit']})"
        )
    deep = sorted(
        (h for h in m.hotspots if h["kind"] == "nesting"),
        key=lambda h: h["value"],
        reverse=True,
    )[:2]
    for h in deep:
        lines.append(
            f"line {h['line']}: {h['name']}() nested {h['value']} deep (limit {h['limit']})"
        )
    if not worst and not deep:
        lines.append("no single function exceeds its limit -- the rise is spread across the file")
    return lines


def run_gate(repo_path: str | Path = ".", spike_override: float | None = None) -> GateResult:
    started = time.perf_counter()

    repo = open_repo(repo_path)
    root = Path(repo.working_tree_dir)
    cfg = load_config(root)
    gk = cfg["gatekeeper"]
    result = GateResult(passed=True)

    if not gk.get("enabled", True):
        result.skipped_reason = "gatekeeper disabled in config.json"
        result.elapsed = time.perf_counter() - started
        return result

    languages = set(cfg.get("languages") or []) or None
    staged = staged_source_files(repo, cfg["exclude_paths"], languages)
    result.staged = staged
    if not staged:
        result.skipped_reason = "no staged source files"
        result.elapsed = time.perf_counter() - started
        return result

    max_staged = gk.get("max_staged_files", 50)
    if len(staged) > max_staged:
        result.skipped_reason = (
            f"{len(staged)} staged files exceeds max_staged_files={max_staged}; "
            "skipping to protect commit latency"
        )
        result.elapsed = time.perf_counter() - started
        return result

    # --- read staged content once; reused by both checks ---
    sources: dict[str, str] = {}
    for rel in staged:
        blob = staged_blob(repo, rel)
        if blob is not None:
            sources[rel] = blob

    db_path = root / cfg["db_path"]
    if not db_path.exists():
        result.skipped_reason = (
            "no baseline database -- run `repohealth scan` to establish baselines"
        )
        result.elapsed = time.perf_counter() - started
        return result

    spike_limit = spike_override if spike_override is not None else gk["complexity_spike_pct"]
    window = gk.get("baseline_window", 5)
    min_baseline = gk.get("min_baseline_cc", 5)
    allow_unbaselined = gk.get("allow_unbaselined", True)

    with Store(db_path) as store:
        repo_id = store.upsert_repo(str(root), root.name)

        # --- check 1: complexity spike against rolling baseline (SRS 4.4.1.2/.3) ---
        metrics: dict[str, FileMetrics] = {}
        for rel, src in sources.items():
            m = analyze_source(rel, src, cfg["thresholds"])
            metrics[rel] = m

            if not m.parsed:
                # A file that does not parse cannot be measured. Report it but
                # do not block -- it may be intentional (templates, fixtures).
                result.unparsed.append((rel, m.parse_error or "parse failed"))
                continue

            baseline = store.baseline_cc(rel, window)
            if baseline is None:
                if not allow_unbaselined:
                    result.violations.append(
                        Violation(
                            "complexity_spike", rel,
                            "no baseline on record and allow_unbaselined is false",
                        )
                    )
                continue

            result.checked_baselines += 1

            # Below this, percentage swings are noise: 2 -> 3 is +50%.
            if baseline < min_baseline:
                continue

            pct = (m.cc_total - baseline) / baseline * 100.0
            if pct > spike_limit:
                result.violations.append(
                    Violation(
                        "complexity_spike",
                        rel,
                        f"complexity rose {pct:.1f}% above its baseline",
                        _spike_detail(m, baseline, m.cc_total, pct, spike_limit),
                    )
                )

        # --- check 2: newly introduced import cycles ---
        if gk.get("block_new_cycles", True):
            tracked = source_files(repo, cfg["exclude_paths"], languages)
            if len(tracked) > CYCLE_CHECK_FILE_LIMIT:
                pass  # too large for this path; `scan` still covers it
            else:
                graph = build_graph_with_overrides(root, tracked, sources)
                now_cycles = find_cycles(graph.adjacency())
                last_run = store.latest_run_id(repo_id)
                known = store.cycle_signatures(last_run) if last_run else set()

                for cyc in now_cycles:
                    if frozenset(cyc) in known:
                        continue  # pre-existing; not this commit's fault
                    touched = [p for p in cyc if p in sources]
                    result.violations.append(
                        Violation(
                            "new_cycle",
                            touched[0] if touched else cyc[0],
                            "this commit introduces a circular import",
                            [format_cycle(cyc)],
                        )
                    )

    result.passed = not result.violations
    result.elapsed = time.perf_counter() - started
    return result
