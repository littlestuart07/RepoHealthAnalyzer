"""Configurable thresholds (SRS 5.4.3 — maintainability)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "db_path": ".repohealth/repohealth.db",
    "exclude_paths": [
        ".venv/", "venv/", "env/", "node_modules/", ".git/",
        "build/", "dist/", "__pycache__/", ".repohealth/",
        "migrations/", "tests/fixtures/",
    ],
    "history_limit": 500,
    # Which languages to analyse. Empty means every language RepoHealth
    # supports; name a subset ("Python", "Java") to narrow a mixed repository
    # down to the part a team actually owns.
    "languages": [],
    "gatekeeper": {
        "enabled": True,
        "complexity_spike_pct": 20.0,
        "baseline_window": 5,
        "min_baseline_cc": 5,
        "block_new_cycles": True,
        "max_staged_files": 50,
        # A file with no recorded history has nothing to compare against.
        # Passing is deliberate: blocking a developer's first commit to a new
        # file would be hostile, and the baseline exists by the second commit.
        "allow_unbaselined": True,
    },
    "thresholds": {
        "max_cc_per_function": 10,
        "max_nesting_depth": 4,
        "max_class_loc": 200,
        "high_churn_percentile": 0.9,
    },
    "scoring": {
        "weight_churn_complexity": 0.4,
        "weight_cycles": 0.4,
        "weight_blast_radius": 0.2,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(repo_root: str | Path) -> dict[str, Any]:
    path = Path(repo_root) / "config.json"
    if not path.exists():
        return dict(DEFAULTS)
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json is not valid JSON: {exc}")
    return _deep_merge(DEFAULTS, user)


def is_excluded(rel_path: str, exclude: list[str]) -> bool:
    norm = rel_path.replace("\\", "/")
    return any(pat.rstrip("/") in norm.split("/") or norm.startswith(pat) for pat in exclude)
