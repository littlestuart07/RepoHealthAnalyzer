"""Git repository ingestion & parsing (SRS 4.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from git import InvalidGitRepositoryError, Repo

from .. import lang as _lang
from ..config import is_excluded


class NotAGitRepo(Exception):
    pass


def open_repo(path: str | Path) -> Repo:
    try:
        return Repo(path, search_parent_directories=True)
    except InvalidGitRepositoryError as exc:
        raise NotAGitRepo(f"{path} is not inside a Git repository") from exc


# Record and field separators chosen because they cannot occur in a commit
# subject, author name or path -- unlike newline, tab or any printable char.
_REC = "\x01"
_FLD = "\x02"


def _rename_target(path: str) -> str:
    """git numstat writes renames as `a => b` or `dir/{old => new}/f.py`.

    The post-rename path is what exists in the tree now, so that is what the
    change is attributed to.
    """
    if "=>" not in path:
        return path
    if "{" in path and "}" in path:
        pre, rest = path.split("{", 1)
        mid, post = rest.split("}", 1)
        new = mid.split("=>", 1)[1].strip()
        return (pre + new + post).replace("//", "/")
    return path.split("=>", 1)[1].strip()


def iter_commits(
    repo: Repo,
    limit: int | None = None,
    exclude: list[str] | None = None,
    python_only: bool = True,
) -> Iterator[dict[str, Any]]:
    """Walk history in ONE `git log --numstat` call.

    The obvious implementation -- iterating commits and reading
    `commit.stats.files` -- spawns a separate `git diff` per commit, which
    costs ~25s on a repository with 500 commits. A single log invocation
    parsed here does the same work in well under a second.

    Merge commits produce no numstat output by default and are therefore
    skipped; they introduce no changes of their own, so churn is unaffected.
    """
    exclude = exclude or []
    args = ["--numstat", f"--format={_REC}%H{_FLD}%an{_FLD}%ae{_FLD}%aI{_FLD}%s"]
    if limit:
        args.append(f"-n{limit}")

    try:
        out = repo.git.log(*args)
    except Exception:
        return  # empty repository, no HEAD yet

    for chunk in out.split(_REC):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        parts = lines[0].split(_FLD)
        if len(parts) < 5:
            continue
        sha, author_name, author_email, authored_at, subject = parts[:5]

        files: list[dict[str, Any]] = []
        for raw in lines[1:]:
            raw = raw.strip()
            if not raw:
                continue
            bits = raw.split("\t")
            if len(bits) != 3:
                continue
            added, deleted, path = bits
            path = _rename_target(path).replace("\\", "/")
            if python_only and not _lang.is_source(path):
                continue
            if is_excluded(path, exclude):
                continue
            files.append(
                {
                    "path": path,
                    # binary files report "-"; count them as zero-line changes
                    "lines_added": int(added) if added.isdigit() else 0,
                    "lines_deleted": int(deleted) if deleted.isdigit() else 0,
                }
            )

        if not files:
            continue
        yield {
            "sha": sha,
            "author_name": author_name,
            "author_email": author_email,
            "authored_at": authored_at,
            "message": subject[:200],
            "files": files,
        }


def source_files(
    repo: Repo, exclude: list[str] | None = None, languages: set[str] | None = None
) -> list[str]:
    """Tracked source files in the working tree, repo-relative, excludes applied.

    One `git ls-files` call covering every supported extension, rather than one
    per language: the subprocess round trip costs more than the extra output.
    `languages` is a set of labels ("Python", "Java") that narrows the result.
    """
    exclude = exclude or []
    root = Path(repo.working_tree_dir)
    patterns = [f"*{ext}" for ext in _lang.ALL_EXTENSIONS]
    out = []
    for entry in repo.git.ls_files(*patterns).splitlines():
        entry = entry.strip().replace("\\", "/")
        if not entry or is_excluded(entry, exclude):
            continue
        if not _lang.is_source(entry, languages):
            continue
        if (root / entry).is_file():
            out.append(entry)
    return sorted(out)


# The old name, kept so nothing that still calls it breaks.
python_files = source_files


def staged_source_files(
    repo: Repo, exclude: list[str] | None = None, languages: set[str] | None = None
) -> list[str]:
    """Python files staged for the pending commit -- the gatekeeper's input.

    Uses `git diff --cached --name-only --diff-filter=ACMR`, which reports
    exactly what the pending commit will contain. Deleted files are excluded
    by the filter since there is nothing left to analyse.
    """
    exclude = exclude or []
    try:
        out = repo.git.diff("--cached", "--name-only", "--diff-filter=ACMR")
    except Exception:
        return []

    root = Path(repo.working_tree_dir)
    paths = []
    for entry in out.splitlines():
        entry = entry.strip().replace("\\", "/")
        if not entry or not _lang.is_source(entry, languages):
            continue
        if is_excluded(entry, exclude):
            continue
        if (root / entry).is_file():
            paths.append(entry)
    return sorted(set(paths))


staged_python_files = staged_source_files


def staged_blob(repo: Repo, rel_path: str) -> str | None:
    """Content of a file AS STAGED, not as it sits on disk.

    These differ whenever the developer has edited a file further after
    `git add`. The gate must judge what is actually being committed.
    """
    try:
        return repo.git.show(f":{rel_path}")
    except Exception:
        return None


def head_sha(repo: Repo) -> str | None:
    try:
        return repo.head.commit.hexsha
    except Exception:
        return None
