"""RepoHealth Analyzer CLI.

    repohealth scan [PATH]      ingest history, analyze complexity + dependencies
    repohealth report [PATH]    churn / complexity / risk summary
    repohealth cycles [PATH]    list circular import chains
    repohealth graph [PATH]     export the dependency graph as JSON
    repohealth init [PATH]      install the pre-commit gatekeeper
    repohealth check --staged   run the gatekeeper (invoked by the hook)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .analysis.blast import blast_radius, direct_dependents
from .analysis.cycles import find_cycles, format_cycle
from .analysis.depgraph import build_graph
from .analysis.refactor import recommend
from .analysis.scoring import compute_health, refactor_estimate
from .pipeline import run_scan
from .config import load_config
from .db.store import Store
from .hooks import installer
from .hooks.gate import run_gate
from .ingest.git_log import NotAGitRepo, open_repo, source_files
from .ingest.remote import CloneFailed, clone, looks_like_url


class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"

    @classmethod
    def off(cls) -> None:
        for k in list(vars(cls)):
            if k.isupper():
                setattr(cls, k, "")


def _resolve(path: str):
    # A URL is cloned into the cache first, so every command accepts one.
    if looks_like_url(path):
        print(f"{C.CYAN}Cloning{C.RESET} {path}")
        try:
            path = str(clone(path))
        except CloneFailed as exc:
            raise SystemExit(f"{C.RED}error:{C.RESET} {exc}")
        print(f"{C.DIM}  into {path}{C.RESET}")
    repo = open_repo(path)
    root = Path(repo.working_tree_dir)
    cfg = load_config(root)
    return repo, root, cfg


def _require_db(root: Path, cfg) -> Path:
    db_path = root / cfg["db_path"]
    if not db_path.exists():
        print(f"{C.RED}No database.{C.RESET} Run `repohealth scan` first.")
        raise SystemExit(1)
    return db_path


def cmd_scan(args) -> int:
    started = time.perf_counter()
    repo, root, cfg = _resolve(args.path)

    def report(phase: str, message: str) -> None:
        if not args.quiet:
            print(f"{C.DIM}·{C.RESET} {message}")

    summary = run_scan(root, report)

    print(f"{C.CYAN}Ingested{C.RESET} {summary['commits_new']} new commits "
          f"({summary['commits_scanned']} scanned)")
    mix = summary.get("languages") or {}
    detail = ", ".join(f"{n} {label}" for label, n in mix.items())
    print(f"{C.CYAN}Analyzed{C.RESET} {summary['files']} source files "
          f"({len(summary['unparsed'])} unparsed)")
    if detail:
        print(f"  {C.DIM}{detail}{C.RESET}")
    for path, err in summary["unparsed"][:5]:
        print(f"  {C.YELLOW}skipped{C.RESET} {path}: {err}")
    print(f"{C.CYAN}Graph{C.RESET} {summary['nodes']} nodes, "
          f"{summary['edges']} internal edges")

    cycles = summary["cycles"]
    if cycles:
        print(f"{C.RED}Cycles{C.RESET} {len(cycles)} circular import chain(s) detected")
        for c in cycles[:3]:
            print(f"  {C.RED}!{C.RESET} {format_cycle(c)}")
        if len(cycles) > 3:
            print(f"  {C.DIM}... {len(cycles) - 3} more (run `repohealth cycles`){C.RESET}")
    else:
        print(f"{C.GREEN}Cycles{C.RESET} none detected")

    bd = summary["health"]
    colour = C.GREEN if bd.score >= 80 else C.YELLOW if bd.score >= 60 else C.RED
    print(f"{C.BOLD}Health{C.RESET} {colour}{bd.score:.0f}/100 ({bd.grade}){C.RESET}")
    print(f"{C.DIM}Run #{summary['run_id']} completed in "
          f"{time.perf_counter() - started:.2f}s{C.RESET}")
    return 0


def cmd_report(args) -> int:
    repo, root, cfg = _resolve(args.path)
    with Store(_require_db(root, cfg)) as store:
        repo_id = store.upsert_repo(str(root), root.name)
        run_id = store.latest_run_id(repo_id)
        if run_id is None:
            print("No completed runs.")
            return 1
        risk = store.risk_for_run(run_id)
        metrics = {m["path"]: m["cc_total"] for m in store.metrics_for_run(run_id)}
        cycle_rows = store.cycles_for_run(run_id)

    print(f"\n{C.BOLD}Risk ranking (run #{run_id}, top {args.top}){C.RESET}")
    print(f"{'file':<44} {'churn':>6} {'cc':>5} {'blast':>6} {'score':>7} {'level':>7}")
    print("-" * 80)
    for r in risk[: args.top]:
        colour = {"high": C.RED, "medium": C.YELLOW}.get(r["risk_level"], "")
        cc = metrics.get(r["path"], 0)
        print(f"{colour}{r['path'][:44]:<44} {r['churn']:>6} {cc:>5} "
              f"{r['blast_radius']:>6} {int(r['risk_score']):>7} {r['risk_level']:>7}{C.RESET}")

    if cycle_rows:
        print(f"\n{C.RED}{len(cycle_rows)} circular dependency chain(s){C.RESET}")
        for row in cycle_rows[: args.top]:
            print(f"  {format_cycle(json.loads(row['path_json']))}")
    print()
    return 0


def cmd_cycles(args) -> int:
    repo, root, cfg = _resolve(args.path)
    with Store(_require_db(root, cfg)) as store:
        repo_id = store.upsert_repo(str(root), root.name)
        run_id = store.latest_run_id(repo_id)
        rows = store.cycles_for_run(run_id) if run_id else []

    if not rows:
        print(f"{C.GREEN}No circular imports detected.{C.RESET}")
        return 0
    print(f"\n{C.RED}{C.BOLD}{len(rows)} circular import chain(s){C.RESET}\n")
    for i, row in enumerate(rows, 1):
        chain = json.loads(row["path_json"])
        print(f"{C.RED}[{i}]{C.RESET} length {row['length']}")
        for step in chain:
            print(f"      {step}")
        print(f"      {C.DIM}-> back to {chain[0]}{C.RESET}\n")
    return 1 if args.strict else 0


def cmd_graph(args) -> int:
    repo, root, cfg = _resolve(args.path)
    languages = set(cfg.get("languages") or []) or None
    files = source_files(repo, cfg["exclude_paths"], languages)
    graph = build_graph(root, files)
    rev = graph.reverse_adjacency()
    payload = graph.to_json()
    payload["blast_radius"] = blast_radius(rev)
    payload["direct_dependents"] = direct_dependents(rev)
    payload["cycles"] = find_cycles(graph.adjacency())

    text = json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out} ({len(graph.nodes)} nodes, {len(graph.edges)} edges)")
    else:
        print(text)
    return 0


def cmd_init(args) -> int:
    repo, root, cfg = _resolve(args.path)

    if args.uninstall:
        if installer.uninstall(repo):
            print(f"{C.GREEN}Removed{C.RESET} the RepoHealth pre-commit hook.")
        else:
            print("No RepoHealth hook was installed.")
        return 0

    hook, action = installer.install(repo, force=args.force)

    if action == "skipped":
        print(f"{C.YELLOW}A different pre-commit hook already exists.{C.RESET}")
        print(f"  {hook}")
        print("  Re-run with --force to back it up and replace it.")
        return 1

    print(f"{C.GREEN}Hook {action}{C.RESET} at {hook}")
    if action == "replaced":
        print(f"  {C.DIM}previous hook saved as pre-commit.pre-repohealth{C.RESET}")

    if installer.ensure_gitignored(repo, cfg["db_path"]):
        print(f"  {C.DIM}added .repohealth/ to .gitignore{C.RESET}")

    db_path = root / cfg["db_path"]
    if db_path.exists():
        print(f"{C.DIM}Baselines present. The gate is live on your next commit.{C.RESET}")
    else:
        print(f"{C.YELLOW}No baselines yet.{C.RESET} Run `repohealth scan` to establish them —")
        print(f"  {C.DIM}until then the gate passes everything through.{C.RESET}")
    print(f"{C.DIM}Bypass a single commit with: git commit --no-verify{C.RESET}")
    return 0


def cmd_check(args) -> int:
    result = run_gate(args.path, spike_override=args.spike)

    if result.skipped_reason:
        if args.verbose:
            print(f"{C.DIM}repohealth: {result.skipped_reason}{C.RESET}")
        return 0

    for path, err in result.unparsed:
        print(f"{C.YELLOW}repohealth: could not parse {path} ({err}) — not blocking{C.RESET}")

    if result.passed:
        if not args.quiet:
            n = len(result.staged)
            print(
                f"{C.GREEN}repohealth: OK{C.RESET} {C.DIM}"
                f"{n} staged file{'s' if n != 1 else ''}, "
                f"{result.checked_baselines} checked against baseline, "
                f"{result.elapsed:.2f}s{C.RESET}"
            )
        return 0

    spikes = [v for v in result.violations if v.kind == "complexity_spike"]
    newcyc = [v for v in result.violations if v.kind == "new_cycle"]

    print()
    print(f"{C.RED}{C.BOLD}  COMMIT BLOCKED — RepoHealth Gatekeeper{C.RESET}")
    print(f"{C.RED}  {'─' * 56}{C.RESET}")

    for v in spikes:
        print(f"\n  {C.RED}✖{C.RESET} {C.BOLD}{v.path}{C.RESET}")
        print(f"    {v.message}")
        for line in v.detail:
            print(f"    {C.DIM}·{C.RESET} {line}")

    for v in newcyc:
        print(f"\n  {C.RED}✖{C.RESET} {C.BOLD}new circular import{C.RESET}")
        for line in v.detail:
            print(f"    {C.DIM}·{C.RESET} {line}")

    print(f"\n{C.RED}  {'─' * 56}{C.RESET}")
    if spikes:
        print(f"  {C.DIM}Extract the flagged functions, or split the file.{C.RESET}")
    if newcyc:
        print(f"  {C.DIM}Break the loop: move the shared code into a third module.{C.RESET}")
    print(f"  {C.DIM}Intentional? Commit with: git commit --no-verify{C.RESET}")
    print(f"  {C.DIM}Checked in {result.elapsed:.2f}s{C.RESET}\n")
    return 1


def cmd_score(args) -> int:
    repo, root, cfg = _resolve(args.path)
    with Store(_require_db(root, cfg)) as store:
        repo_id = store.upsert_repo(str(root), root.name)
        run_id = store.latest_run_id(repo_id)
        if run_id is None:
            print("No completed runs.")
            return 1
        metrics = [dict(m) for m in store.metrics_for_run(run_id)]
        churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
        cycles = [json.loads(r["path_json"]) for r in store.cycles_for_run(run_id)]
        risk = [dict(r) for r in store.risk_for_run(run_id)]
        blast = {r["path"]: r["blast_radius"] for r in risk}

    bd = compute_health(metrics, churn, blast, cycles, cfg)
    colour = C.GREEN if bd.score >= 80 else C.YELLOW if bd.score >= 60 else C.RED

    print(f"\n  {C.BOLD}Repository Health{C.RESET}  {colour}{C.BOLD}{bd.score:.0f}/100  "
          f"grade {bd.grade}{C.RESET}\n")
    print(f"  {'signal':<20} {'penalty':>8} {'weight':>7} {'points lost':>12}")
    print(f"  {'-' * 50}")
    for k in bd.penalties:
        print(f"  {k:<20} {bd.penalties[k]:>8.2f} {bd.weights[k]:>7.2f} "
              f"{bd.contributions[k]:>12.1f}")
    f = bd.facts
    print(f"\n  {C.DIM}{f['files']} files · {f['hotspot_files']} churn+complexity hotspots · "
          f"{f['cycle_count']} cycles touching {f['files_in_cycles']} files · "
          f"{f['fragile_files']} fragile{C.RESET}")

    if args.effort:
        mt = {m["path"]: m for m in metrics}
        in_cycle = {p for c in cycles for p in c}
        ests = [
            refactor_estimate(mt[r["path"]], cfg["thresholds"], r["path"] in in_cycle)
            for r in risk if r["path"] in mt
        ]
        ests = [e for e in ests if e["hours"] > 0]
        ests.sort(key=lambda e: e["hours"], reverse=True)
        if ests:
            print(f"\n  {C.BOLD}Refactoring candidates{C.RESET}  "
                  f"{C.DIM}(~{sum(e['hours'] for e in ests):.0f}h total, heuristic){C.RESET}\n")
            for e in ests[: args.top]:
                print(f"  {e['hours']:>5.1f}h  {e['path'][:46]:<46} {C.DIM}{e['band']}{C.RESET}")
                for d in e["drivers"]:
                    print(f"         {C.DIM}· {d}{C.RESET}")
    print()
    return 0


def cmd_serve(args) -> int:
    try:
        from .api.server import serve
    except ImportError:
        # The dashboard is an optional extra -- the CLI and the commit hook
        # work without a web server, so a missing FastAPI is a normal state,
        # not a broken install.
        print(f"{C.YELLOW}The dashboard needs FastAPI, which isn't installed.{C.RESET}")
        print(f"  {C.DIM}pip install 'repohealth-analyzer[dashboard]'{C.RESET}")
        print(f"  {C.DIM}or: pip install fastapi uvicorn{C.RESET}")
        return 1

    repo, root, cfg = _resolve(args.path)
    _require_db(root, cfg)
    print(f"{C.CYAN}RepoHealth{C.RESET} serving {root.name} at "
          f"{C.BOLD}http://{args.host}:{args.port}{C.RESET}")
    print(f"{C.DIM}API docs at /api/docs · Ctrl+C to stop{C.RESET}")
    serve(args.path, host=args.host, port=args.port)
    return 0


def cmd_app(args) -> int:
    """Start the server and open the app in the default browser."""
    try:
        from .api.server import serve
    except ImportError:
        print(f"{C.YELLOW}The app needs FastAPI, which isn't installed.{C.RESET}")
        print(f"  {C.DIM}pip install \'repohealth-analyzer[dashboard]\'{C.RESET}")
        return 1

    import socket
    import threading

    from .launcher import open_window

    # Pick a free port if the default is taken, so a second instance or an
    # unrelated dev server does not stop the app from starting.
    port = args.port
    for candidate in range(args.port, args.port + 20):
        with socket.socket() as probe:
            if probe.connect_ex((args.host, candidate)) != 0:
                port = candidate
                break
    else:
        print(f"{C.RED}No free port in range {args.port}-{args.port + 19}.{C.RESET}")
        return 1

    url = f"http://{args.host}:{port}"
    repo_path = None
    if args.path:
        try:
            repo_path = str(Path(open_repo(args.path).working_tree_dir))
        except NotAGitRepo:
            print(f"{C.YELLOW}{args.path} is not a Git repository — "
                  f"open one from the app.{C.RESET}")

    print(f"{C.CYAN}RepoHealth{C.RESET} running at {C.BOLD}{url}{C.RESET}")
    print(f"{C.DIM}Ctrl+C to stop{C.RESET}")

    if args.no_browser:
        try:
            serve(repo_path, host=args.host, port=port)
        except KeyboardInterrupt:
            print(f"\n{C.DIM}Stopped.{C.RESET}")
        return 0

    if args.window:
        # pywebview must own the main thread, so the server goes to a daemon
        # thread and the window call blocks here until the user closes it.
        server = threading.Thread(
            target=lambda: serve(repo_path, host=args.host, port=port), daemon=True
        )
        server.start()
        import time
        time.sleep(1.0)
        route = open_window(url)
        if route == "chrome":
            print(f"{C.DIM}Opened in an app window. Close this terminal to stop.{C.RESET}")
            try:
                server.join()
            except KeyboardInterrupt:
                print(f"\n{C.DIM}Stopped.{C.RESET}")
        elif route == "browser":
            print(f"{C.DIM}No Chromium-family browser found — opened a normal "
                  f"browser tab instead.{C.RESET}")
            try:
                server.join()
            except KeyboardInterrupt:
                print(f"\n{C.DIM}Stopped.{C.RESET}")
        return 0

    threading.Timer(1.0, lambda: __import__('webbrowser').open(url)).start()
    try:
        serve(repo_path, host=args.host, port=port)
    except KeyboardInterrupt:
        print(f"\n{C.DIM}Stopped.{C.RESET}")
    return 0


def cmd_suggest(args) -> int:
    """Recommend how to break each circular import."""
    from .analysis.depgraph import build_graph, parse_all

    repo, root, cfg = _resolve(args.path)
    languages = set(cfg.get("languages") or []) or None
    files = source_files(repo, cfg["exclude_paths"], languages)
    graph = build_graph(root, files)
    _, trees = parse_all(root, files)

    cycles = find_cycles(graph.adjacency(runtime_only=not args.include_type_only))
    if not cycles:
        print(f"{C.GREEN}No circular imports to fix.{C.RESET}")
        return 0

    recs = recommend(graph, cycles, trees, limit=args.top)
    print(f"{C.CYAN}{len(cycles)}{C.RESET} circular import(s); "
          f"showing {len(recs)} recommendation(s)\n")

    for i, s in enumerate(recs, 1):
        chain = " -> ".join(s.cycle) + f" -> {s.cycle[0]}"
        print(f"{C.BOLD}{i}. {chain}{C.RESET}")
        print(f"   {C.DIM}cut{C.RESET} {s.edge_from} {C.DIM}->{C.RESET} {s.edge_to}"
              + (f" ({', '.join(s.symbols[:5])})" if s.symbols else ""))
        tag = {"type_only": "move under TYPE_CHECKING",
               "lazy": "import inside the function",
               "extract": "extract a shared module",
               "already_deferred": "no action needed"}[s.strategy]
        colour = C.GREEN if s.confidence == "high" else C.YELLOW
        print(f"   {colour}{tag}{C.RESET} {C.DIM}({s.confidence} confidence){C.RESET}")
        print(f"   {C.DIM}{s.rationale}{C.RESET}")
        for n, step in enumerate(s.steps, 1):
            print(f"     {n}. {step}")
        if args.diff and s.before:
            print()
            for ln in s.before.splitlines():
                print(f"     {C.RED}- {ln}{C.RESET}")
            for ln in s.after.splitlines():
                print(f"     {C.GREEN}+ {ln}{C.RESET}")
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repohealth", description=__doc__)
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="ingest history, analyze complexity and dependencies")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--quiet", action="store_true", help="suppress phase messages")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("report", help="risk summary of the latest run")
    r.add_argument("path", nargs="?", default=".")
    r.add_argument("--top", type=int, default=15)
    r.set_defaults(func=cmd_report)

    cy = sub.add_parser("cycles", help="list circular import chains")
    cy.add_argument("path", nargs="?", default=".")
    cy.add_argument("--strict", action="store_true", help="exit 1 if any cycle exists")
    cy.set_defaults(func=cmd_cycles)

    g = sub.add_parser("graph", help="export dependency graph as JSON")
    g.add_argument("path", nargs="?", default=".")
    g.add_argument("--out", help="write to file instead of stdout")
    g.set_defaults(func=cmd_graph)

    i = sub.add_parser("init", help="install the pre-commit gatekeeper")
    i.add_argument("path", nargs="?", default=".")
    i.add_argument("--force", action="store_true", help="back up and replace an existing hook")
    i.add_argument("--uninstall", action="store_true")
    i.set_defaults(func=cmd_init)

    sc = sub.add_parser("score", help="repository health score with breakdown")
    sc.add_argument("path", nargs="?", default=".")
    sc.add_argument("--effort", action="store_true", help="list refactoring estimates")
    sc.add_argument("--top", type=int, default=10)
    sc.set_defaults(func=cmd_score)

    ap = sub.add_parser("app", help="open the RepoHealth app in your browser")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--window", action="store_true",
                    help="open in its own window instead of a browser tab")
    ap.add_argument("--no-browser", action="store_true",
                    help="start the server only")
    ap.set_defaults(func=cmd_app)

    sv = sub.add_parser("serve", help="run the server without opening a browser")
    sv.add_argument("path", nargs="?", default=".")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    sg = sub.add_parser("suggest", help="recommend how to break circular imports")
    sg.add_argument("path", nargs="?", default=".")
    sg.add_argument("--top", type=int, default=10)
    sg.add_argument("--diff", action="store_true", help="show the before/after")
    sg.add_argument("--include-type-only", action="store_true",
                    help="also report loops that exist only for a type checker")
    sg.set_defaults(func=cmd_suggest)

    c = sub.add_parser("check", help="pre-commit gatekeeper")
    c.add_argument("path", nargs="?", default=".")
    c.add_argument("--staged", action="store_true", help="check staged files (default)")
    c.add_argument("--spike", type=float, help="override the spike threshold percentage")
    c.add_argument("--quiet", action="store_true", help="print nothing when the check passes")
    c.add_argument("--verbose", action="store_true", help="explain why a check was skipped")
    c.set_defaults(func=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_color or not sys.stdout.isatty():
        C.off()
    try:
        return args.func(args)
    except NotAGitRepo as exc:
        print(f"{C.RED}error:{C.RESET} {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
