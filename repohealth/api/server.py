"""Local application server.

Binds to 127.0.0.1 only. There is no authentication because there is no network
surface: the server is unreachable from outside the machine, which is the same
guarantee the rest of the system makes about source code never leaving local
storage.

Read endpoints serve the last completed run from SQLite. Write endpoints -- the
ones the app's buttons call -- run a scan, install or remove the commit hook,
and save thresholds.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..analysis.scoring import compute_health, refactor_estimate
from ..config import load_config
from ..db.store import Store
from ..hooks import installer
from ..ingest.git_log import NotAGitRepo
from ..ingest.remote import CloneFailed, clone, is_shallow, looks_like_url
from ..pipeline import PHASES, run_scan
from ..resources import bundled
from .state import AppState, load_recents

WEB_DIR = bundled("web")


def create_app(repo_path: str | None = None) -> FastAPI:
    state = AppState(repo_path)
    app = FastAPI(title="RepoHealth Analyzer", version="1.0.0", docs_url="/api/docs")

    # ---------------- helpers ----------------

    def require_repo() -> Path:
        if not state.is_open:
            raise HTTPException(409, "No repository is open.")
        return state.root  # type: ignore[return-value]

    def store() -> Store:
        require_repo()
        if not state.has_data:
            raise HTTPException(409, "This repository has not been analysed yet.")
        return Store(state.db_path)  # type: ignore[arg-type]

    def latest(s: Store) -> tuple[int, int]:
        repo_id = s.upsert_repo(str(state.root), state.root.name)  # type: ignore[union-attr]
        run_id = s.latest_run_id(repo_id)
        if run_id is None:
            raise HTTPException(409, "No completed analysis runs.")
        return repo_id, run_id

    # ---------------- app shell ----------------

    @app.get("/api/session")
    def session() -> dict[str, Any]:
        """Everything the UI needs to decide what to render on load."""
        hook = "unavailable"
        if state.is_open:
            try:
                hook = installer.status(state.repo())
            except Exception:
                hook = "unavailable"
        return {
            "repo": (
                {"name": state.root.name, "path": str(state.root),
                 "shallow": is_shallow(state.root)}
                if state.root else None
            ),
            "has_data": state.has_data,
            "hook": hook,
            "scan": state.job.as_dict(),
            "recents": load_recents(),
            "phases": [{"key": k, "label": l} for k, l in PHASES],
            "error": state.error,
        }

    @app.post("/api/repo")
    def open_repository(payload: dict = Body(...)) -> dict[str, Any]:
        raw = (payload or {}).get("path", "").strip()
        if not raw:
            raise HTTPException(400, "A repository path or URL is required.")

        # A URL is cloned and then analysed in one background job, so pasting a
        # GitHub link goes straight to results.
        if looks_like_url(raw):
            if not state.begin():
                raise HTTPException(409, "Something is already running.")

            def work() -> None:
                try:
                    state.progress("clone", f"Downloading {raw}")
                    local = clone(raw)
                    state.open(local)
                    summary = run_scan(local, state.progress)
                    state.finish(summary["run_id"])
                except CloneFailed as exc:
                    state.fail(str(exc))
                except Exception as exc:
                    state.fail(f"{type(exc).__name__}: {exc}")

            threading.Thread(target=work, daemon=True).start()
            return {"cloning": True, "url": raw}

        target = Path(raw).expanduser()
        if not target.exists():
            raise HTTPException(404, f"No such folder: {target}")
        try:
            root = state.open(target)
        except NotAGitRepo:
            raise HTTPException(
                400, f"{target} is not a Git repository. Initialise it with `git init` first."
            )
        return {"repo": {"name": root.name, "path": str(root)}, "has_data": state.has_data}

    # ---------------- scanning ----------------

    @app.post("/api/scan")
    def start_scan() -> dict[str, Any]:
        root = require_repo()
        if not state.begin():
            raise HTTPException(409, "A scan is already running.")

        def work() -> None:
            try:
                summary = run_scan(root, state.progress)
                state.finish(summary["run_id"])
            except Exception as exc:
                state.fail(f"{type(exc).__name__}: {exc}")

        # A daemon thread: a scan should never keep the server alive after the
        # user closes the app.
        threading.Thread(target=work, daemon=True).start()
        return state.job.as_dict()

    @app.get("/api/scan")
    def scan_status() -> dict[str, Any]:
        return state.job.as_dict()

    # ---------------- the commit gate ----------------

    @app.get("/api/hook")
    def hook_status() -> dict[str, Any]:
        require_repo()
        return {"status": installer.status(state.repo()), "has_data": state.has_data}

    @app.post("/api/hook")
    def hook_change(payload: dict = Body(...)) -> dict[str, Any]:
        require_repo()
        action = (payload or {}).get("action")
        repo = state.repo()

        if action == "install":
            hook, result = installer.install(repo, force=bool(payload.get("force")))
            if result == "skipped":
                raise HTTPException(
                    409,
                    "This repository already has a different pre-commit hook. "
                    "Installing will back it up first.",
                )
            installer.ensure_gitignored(repo, state.cfg["db_path"])
            return {"status": installer.status(repo), "action": result, "path": str(hook)}

        if action == "uninstall":
            installer.uninstall(repo)
            return {"status": installer.status(repo), "action": "uninstalled"}

        raise HTTPException(400, "action must be 'install' or 'uninstall'")

    # ---------------- analysis results ----------------

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        with store() as s:
            repo_id, run_id = latest(s)
            row = s.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            commits = s.conn.execute(
                "SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?", (repo_id,)
            ).fetchone()["n"]
            langs = [
                {"label": r["language"], "files": r["n"]}
                for r in s.conn.execute(
                    """SELECT language, COUNT(*) AS n FROM file_metrics
                       WHERE run_id = ? GROUP BY language ORDER BY n DESC""",
                    (run_id,),
                )
            ]
        return {
            "languages": langs,
            "repo": {"name": state.root.name, "path": str(state.root)},  # type: ignore[union-attr]
            "run": {
                "id": run_id,
                "commit_sha": row["commit_sha"],
                "completed_at": row["completed_at"],
            },
            "commits_ingested": commits,
        }

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with store() as s:
            repo_id, run_id = latest(s)
            metrics = [dict(m) for m in s.metrics_for_run(run_id)]
            churn = {r["path"]: r["churn"] for r in s.churn(repo_id)}
            cycles = [json.loads(r["path_json"]) for r in s.cycles_for_run(run_id)]
            blast = {r["path"]: r["blast_radius"] for r in s.risk_for_run(run_id)}
        return compute_health(metrics, churn, blast, cycles, state.cfg).as_dict()

    @app.get("/api/health/trend")
    def health_trend() -> list[dict[str, Any]]:
        """The score recomputed for every stored run.

        Timestamped runs have been accumulating since the first scan; this is
        what turns 'how bad is it now' into 'is it getting better or worse'.
        """
        out = []
        with store() as s:
            repo_id, _ = latest(s)
            churn = {r["path"]: r["churn"] for r in s.churn(repo_id)}
            runs = s.conn.execute(
                "SELECT run_id, completed_at FROM runs WHERE repo_id = ? "
                "AND status='completed' ORDER BY run_id", (repo_id,),
            ).fetchall()
            for r in runs:
                rid = r["run_id"]
                metrics = [dict(m) for m in s.metrics_for_run(rid)]
                if not metrics:
                    continue
                cycles = [json.loads(c["path_json"]) for c in s.cycles_for_run(rid)]
                blast = {x["path"]: x["blast_radius"] for x in s.risk_for_run(rid)}
                bd = compute_health(metrics, churn, blast, cycles, state.cfg)
                out.append({
                    "run_id": rid,
                    "at": r["completed_at"],
                    "score": round(bd.score, 1),
                    "cycles": len(cycles),
                    "files": bd.facts.get("files", 0),
                })
        return out

    @app.get("/api/files")
    def files(limit: int = 500) -> list[dict[str, Any]]:
        with store() as s:
            repo_id, run_id = latest(s)
            metrics = {m["path"]: dict(m) for m in s.metrics_for_run(run_id)}
            risk = [dict(r) for r in s.risk_for_run(run_id)]
            churn_rows = {r["path"]: dict(r) for r in s.churn(repo_id)}
            cycles = [json.loads(r["path_json"]) for r in s.cycles_for_run(run_id)]

        in_cycle = {p for c in cycles for p in c}
        out = []
        for r in risk[:limit]:
            m = metrics.get(r["path"], {})
            ch = churn_rows.get(r["path"], {})
            out.append({
                "path": r["path"],
                "churn": r["churn"],
                "authors": ch.get("authors", 0),
                "last_touched": ch.get("last_touched"),
                "cc_total": m.get("cc_total", 0),
                "cc_max": m.get("cc_max", 0),
                "max_nesting": m.get("max_nesting", 0),
                "loc": m.get("loc", 0),
                "blast_radius": r["blast_radius"],
                "risk_score": r["risk_score"],
                "risk_level": r["risk_level"],
                "in_cycle": r["path"] in in_cycle,
                "refactor": refactor_estimate(
                    {**m, "path": r["path"]}, state.cfg["thresholds"], r["path"] in in_cycle
                ),
            })
        return out

    @app.get("/api/graph")
    def graph() -> dict[str, Any]:
        with store() as s:
            repo_id, run_id = latest(s)
            edges = [
                {"from": d["from_path"], "to": d["to_path"], "kind": d["import_kind"]}
                for d in s.dependencies_for_run(run_id)
            ]
            metrics = {m["path"]: dict(m) for m in s.metrics_for_run(run_id)}
            risk = {r["path"]: dict(r) for r in s.risk_for_run(run_id)}
            churn = {r["path"]: r["churn"] for r in s.churn(repo_id)}
            cycles = [json.loads(r["path_json"]) for r in s.cycles_for_run(run_id)]

        in_cycle = {p for c in cycles for p in c}
        nodes = [
            {
                "id": p, "label": p.split("/")[-1],
                "cc": m.get("cc_total", 0), "loc": m.get("loc", 0),
                "churn": churn.get(p, 0),
                "blast_radius": risk.get(p, {}).get("blast_radius", 0),
                "risk_level": risk.get(p, {}).get("risk_level", "low"),
                "in_cycle": p in in_cycle,
            }
            for p, m in metrics.items()
        ]
        return {"nodes": nodes, "edges": edges, "cycles": cycles}

    @app.get("/api/cycles")
    def cycles_endpoint() -> list[dict[str, Any]]:
        with store() as s:
            _, run_id = latest(s)
            rows = s.cycles_for_run(run_id)
        return [{"length": r["length"], "chain": json.loads(r["path_json"])} for r in rows]

    @app.get("/api/suggestions")
    def suggestions_endpoint() -> list[dict[str, Any]]:
        """How to break each circular import.

        Computed on demand rather than stored: it needs the parse trees, and
        re-parsing when somebody actually opens the panel is cheaper than
        carrying a suggestions table through every scan. Flask takes about a
        second; the panel loads after the rest of the page either way.
        """
        require_repo()
        from ..analysis.cycles import find_cycles
        from ..analysis.depgraph import build_graph, parse_all
        from ..analysis.refactor import recommend
        from ..ingest.git_log import source_files

        root = state.root
        languages = set(state.cfg.get("languages") or []) or None
        files = source_files(state.repo(), state.cfg["exclude_paths"], languages)
        graph = build_graph(root, files)
        _, trees = parse_all(root, files)
        cycles = find_cycles(graph.adjacency(runtime_only=True))
        return [s.as_dict() for s in recommend(graph, cycles, trees, limit=25)]

    # ---------------- configuration ----------------

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        require_repo()
        return state.cfg

    @app.put("/api/config")
    def put_config(payload: dict = Body(...)) -> dict[str, Any]:
        root = require_repo()
        allowed = {"gatekeeper", "thresholds", "scoring", "exclude_paths", "history_limit"}
        unknown = set(payload) - allowed
        if unknown:
            raise HTTPException(400, f"unknown config keys: {sorted(unknown)}")

        target = root / "config.json"
        current: dict[str, Any] = {}
        if target.exists():
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
        for k, v in payload.items():
            if isinstance(v, dict) and isinstance(current.get(k), dict):
                current[k].update(v)
            else:
                current[k] = v
        target.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        state.cfg = load_config(root)
        return {"written": str(target), "config": state.cfg}

    # ---------------- the app itself ----------------

    if WEB_DIR.is_dir() and (WEB_DIR / "index.html").exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(WEB_DIR / "index.html"))
    else:
        @app.get("/")
        def index_missing():
            return JSONResponse({"error": "dashboard files are missing from the install"})

    return app


def serve(repo_path: str | None = None, host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(repo_path), host=host, port=port, log_level="warning")
