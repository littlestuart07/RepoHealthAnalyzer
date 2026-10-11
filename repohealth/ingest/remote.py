"""Opening a repository straight from its URL.

Pasting a GitHub link is the obvious thing to try, so the app accepts one and
clones it rather than telling the user to go and do that themselves.

Clones are shallow and land in a cache folder under the user's home, never in
the working directory. Shallow matters twice over: it keeps a large repository
down to seconds, and it bounds how much of someone else's code lands on disk.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

CLONE_ROOT = Path.home() / ".repohealth" / "clones"
DEFAULT_DEPTH = 200

# https://host/owner/repo(.git), git@host:owner/repo.git, ssh://, git://
_URL = re.compile(
    r"^(https?://|git://|ssh://|git@)[\w.@:/\-~]+?(\.git)?/?$", re.IGNORECASE
)


class CloneFailed(Exception):
    pass


def looks_like_url(text: str) -> bool:
    text = text.strip()
    return bool(_URL.match(text)) and ("/" in text or ":" in text)


def repo_name(url: str) -> str:
    """Last path segment, without .git -- the folder a normal clone would make."""
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    name = re.split(r"[/:]", cleaned)[-1]
    # Never let a crafted URL escape the cache folder.
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "repo"


def local_path_for(url: str) -> Path:
    return CLONE_ROOT / repo_name(url)


def clone(url: str, depth: int = DEFAULT_DEPTH, refresh: bool = False) -> Path:
    """Shallow-clone `url` into the cache and return the local path.

    An existing clone is reused unless `refresh` is set, so re-opening a
    repository is instant and works offline.
    """
    url = url.strip()
    target = local_path_for(url)

    if target.exists():
        if not refresh and (target / ".git").is_dir():
            return target
        shutil.rmtree(target, ignore_errors=True)

    CLONE_ROOT.mkdir(parents=True, exist_ok=True)

    # Without GIT_TERMINAL_PROMPT=0, a private or misspelled URL makes git sit
    # waiting for a username -- which, from inside the app, looks like a hang
    # with no way to answer it.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}

    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", str(depth), "--quiet", url, str(target)],
            capture_output=True, text=True, timeout=300, env=env,
        )
    except FileNotFoundError:
        raise CloneFailed("Git is not installed, or not on PATH.")
    except subprocess.TimeoutExpired:
        shutil.rmtree(target, ignore_errors=True)
        raise CloneFailed("The download took longer than five minutes and was stopped.")

    if proc.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise CloneFailed(_explain(url, (proc.stderr or proc.stdout or "")))

    return target


def _explain(url: str, stderr: str) -> str:
    """Turn git's output into something a user can act on."""
    text = stderr.lower()
    if "could not read username" in text or "authentication failed" in text:
        return (f"Could not open {url}. It is either private or the address is "
                f"wrong - RepoHealth can only download public repositories.")
    if "not found" in text or "repository not found" in text:
        return f"No repository found at {url}. Check the address."
    if "could not resolve host" in text or "unable to access" in text:
        return "Could not reach the network. Check your connection, or use a local folder."

    last = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return f"Could not download {url}" + (f" - {last[-1]}" if last else "")


def is_shallow(path: str | Path) -> bool:
    """A shallow clone has incomplete history, so churn is understated."""
    return (Path(path) / ".git" / "shallow").exists()
