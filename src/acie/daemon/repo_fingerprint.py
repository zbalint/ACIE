"""Cheap, conservative fingerprints for a repository working tree."""

import hashlib
import os
import subprocess
from collections.abc import Iterable

def compute_repo_fingerprint(repo_root: str) -> str | None:
    """Return a git-native fingerprint of ``repo_root``'s working-tree state.

    The fingerprint covers the current ``HEAD``, tracked content differences
    from ``HEAD``, and every untracked file's path, modification time, and
    size. Any git-command failure returns ``None`` so callers fail toward a
    reconciliation pass rather than incorrectly treating the repository as
    unchanged.
    """
    head = _run_git(repo_root, "rev-parse", "HEAD")
    if head is None:
        return None

    # shortcut: git diff HEAD also notices tracked non-Python changes that
    # walk_repo() will not enrich; keep the safe false-positive direction.
    # Upgrade trigger: narrow this input if real repositories show frequent,
    # materially wasteful passes caused by unrelated tracked files.
    diff = _run_git(repo_root, "diff", "HEAD")
    if diff is None:
        return None

    status = _run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
    if status is None:
        return None

    untracked: list[tuple[str, int, int]] = []
    for line in status.splitlines():
        if not line.startswith("??"):
            continue
        rel_path = line[3:]
        try:
            stat_result = os.stat(os.path.join(repo_root, rel_path))
        except OSError:
            continue
        untracked.append((rel_path, stat_result.st_mtime_ns, stat_result.st_size))
    untracked.sort()

    hasher = hashlib.sha256()
    hasher.update(head.strip().encode("utf-8"))
    hasher.update(diff.encode("utf-8"))
    for rel_path, mtime_ns, size in untracked:
        hasher.update(f"{rel_path}\0{mtime_ns}\0{size}\0".encode("utf-8"))
    return hasher.hexdigest()


def compute_repo_head_sha(repo_root: str) -> str | None:
    """Return the current commit SHA used as an index baseline."""
    head = _run_git(repo_root, "rev-parse", "HEAD")
    return None if head is None else head.strip()


def compute_changed_relpaths(
    repo_root: str,
    *,
    previous_head_sha: str | None = None,
    previous_fingerprint: str | None = None,
    baseline_relpaths: Iterable[str] = (),
) -> list[str] | None:
    """Return paths changed since a seeded index snapshot."""
    current_fingerprint = compute_repo_fingerprint(repo_root)
    if current_fingerprint is None:
        return None

    root = os.path.realpath(repo_root)
    missing_seeded = {
        path for path in baseline_relpaths if not os.path.isfile(os.path.join(root, path))
    }
    if previous_fingerprint is not None and current_fingerprint == previous_fingerprint and not missing_seeded:
        return []
    if previous_head_sha is None:
        return None

    committed = _run_git(repo_root, "diff", "--name-only", previous_head_sha, "HEAD")
    working_tree = _run_git(repo_root, "diff", "--name-only", "HEAD")
    status = _run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
    if committed is None or working_tree is None or status is None:
        return None

    changed: set[str] = set(missing_seeded)
    changed.update(path for path in committed.splitlines() if path)
    changed.update(path for path in working_tree.splitlines() if path)
    changed.update(line[3:] for line in status.splitlines() if line.startswith("?? "))
    return sorted(changed)


def _run_git(repo_root: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # noqa: BLE001 -- fingerprinting is deliberately best-effort.
        return None
    return result.stdout if result.returncode == 0 else None
