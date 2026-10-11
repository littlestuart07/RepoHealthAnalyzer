"""Application state for the desktop app.

The CLI is stateless -- every command takes a repository path. The app is not:
it has a *currently open* repository that the user switches from the interface,
and a scan that runs in the background so the page stays responsive while a
large repository is analysed.

This module owns both, plus the short list of recently opened repositories kept
in the user's home directory so the app remembers where they work.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_config
from ..ingest.git_log import NotAGitRepo, open_repo

RECENTS_PATH = Path.home() / ".repohealth" / "recent.json"
MAX_RECENTS = 8


# ---------------- recent repositories ----------------

def load_recents() -> list[dict[str, Any]]:
    try:
        data = json.loads(RECENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    # Drop entries whose folder has since been deleted or moved.
    return [r for r in data if isinstance(r, dict) and r.get("path") and Path(r["path"]).is_dir()]


def remember(path: Path) -> None:
    entries = [r for r in load_recents() if r["path"] != str(path)]
    entries.insert(0, {"path": str(path), "name": path.name, "opened_at": time.time()})
    RECENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        RECENTS_PATH.write_text(
            json.dumps(entries[:MAX_RECENTS], indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # a missing recents list is cosmetic, never fatal


# ---------------- scan job ----------------

@dataclass
class ScanJob:
    """A scan running in a worker thread.

    Progress is coarse by design -- five named phases rather than a percentage,
    because the phases take wildly different times on different repositories
    and a fake percentage that jumps from 10% to 90% is worse than none.
    """

    status: str = "idle"          # idle | running | done | error
    phase: str = ""
    message: str = ""
    started_at: float = 0.0
    elapsed: float = 0.0
    run_id: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        elapsed = (
            time.perf_counter() - self.started_at
            if self.status == "running" and self.started_at
            else self.elapsed
        )
        return {
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "elapsed": round(elapsed, 2),
            "run_id": self.run_id,
            "error": self.error,
        }


class AppState:
    """The repository the app currently has open, and the scan in flight."""

    def __init__(self, repo_path: str | Path | None = None):
        self._lock = threading.Lock()
        self.job = ScanJob()
        self.root: Path | None = None
        self.cfg: dict[str, Any] = {}
        self.error: str | None = None
        if repo_path is not None:
            try:
                self.open(repo_path)
            except NotAGitRepo as exc:
                self.error = str(exc)

    # -- repository --

    def open(self, repo_path: str | Path) -> Path:
        """Point the app at a repository. Raises NotAGitRepo if it isn't one."""
        repo = open_repo(repo_path)
        root = Path(repo.working_tree_dir)
        with self._lock:
            self.root = root
            self.cfg = load_config(root)
            self.error = None
            # Clearing the job is right when the user opens a repository, but
            # the clone-then-scan job opens one midway through itself -- and
            # resetting to idle there would tell the polling UI the work had
            # stopped.
            if self.job.status != "running":
                self.job = ScanJob()
        remember(root)
        return root

    @property
    def is_open(self) -> bool:
        return self.root is not None

    @property
    def db_path(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / self.cfg["db_path"]

    @property
    def has_data(self) -> bool:
        p = self.db_path
        return p is not None and p.exists()

    def repo(self):
        if self.root is None:
            raise NotAGitRepo("no repository is open")
        return open_repo(self.root)

    # -- scan job --

    def begin(self) -> bool:
        """Claim the job slot. False if a scan is already running."""
        with self._lock:
            if self.job.status == "running":
                return False
            self.job = ScanJob(
                status="running", phase="starting",
                message="Preparing", started_at=time.perf_counter(),
            )
            return True

    def progress(self, phase: str, message: str) -> None:
        with self._lock:
            self.job.phase = phase
            self.job.message = message

    def finish(self, run_id: int) -> None:
        with self._lock:
            self.job.elapsed = time.perf_counter() - self.job.started_at
            self.job.status = "done"
            self.job.phase = "complete"
            self.job.message = "Analysis complete"
            self.job.run_id = run_id

    def fail(self, error: str) -> None:
        with self._lock:
            self.job.elapsed = time.perf_counter() - self.job.started_at
            self.job.status = "error"
            self.job.phase = "failed"
            self.job.message = "Analysis failed"
            self.job.error = error
