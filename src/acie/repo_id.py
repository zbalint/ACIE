import hashlib
import os
import subprocess

# Truncation length for the repo-id hash: 16 hex chars = 64 bits of the
# sha256 digest. Plenty of collision headroom for the number of repos any
# one machine's ACIE daemon will ever index; kept short for a manageable
# ~/.acie/repos/<repo-id>/ directory name.
_REPO_ID_HEX_LENGTH = 16


def resolve_git_common_dir(repo_path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    raw = result.stdout.strip()
    resolved = os.path.normpath(os.path.join(repo_path, raw))
    return os.path.realpath(resolved)


def resolve_repo_root(repo_path: str) -> str | None:
    """The top-level working-directory root repo_path sits inside.

    Unlike resolve_git_common_dir (which collapses a worktree to the
    shared main .git dir), this resolves to the worktree's own root when
    repo_path is inside a worktree -- the daemon dispatch layer needs the
    actual on-disk directory to walk for source files, not the shared
    metadata dir. None if repo_path isn't inside a git repo.
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    return os.path.realpath(result.stdout.strip())


def resolve_repo_id(repo_path: str) -> str | None:
    common_dir = resolve_git_common_dir(repo_path)
    if common_dir is None:
        return None

    digest = hashlib.sha256(common_dir.encode("utf-8")).hexdigest()
    return digest[:_REPO_ID_HEX_LENGTH]


def resolve_repo_state_dir(repo_path: str, base_dir: str | None = None) -> str | None:
    """~/.acie/repos/<repo-id>/ for repo_path, created if it doesn't exist yet.

    None if repo_path isn't inside a git repo (mirrors resolve_repo_id).
    base_dir overrides the ~/.acie root -- tests pass a tmp_path so they
    never touch the real user home directory; production callers omit it.
    """
    repo_id = resolve_repo_id(repo_path)
    if repo_id is None:
        return None

    root = base_dir or os.path.expanduser("~/.acie")
    state_dir = os.path.join(root, "repos", repo_id)
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def resolve_index_db_path(repo_path: str, base_dir: str | None = None) -> str | None:
    """Path to repo_path's index.sqlite under its resolved state dir.

    None if repo_path isn't inside a git repo. The parent directory is
    created (via resolve_repo_state_dir) but the sqlite file itself is not
    -- SymbolStore/RelationStore create it on first connect.
    """
    state_dir = resolve_repo_state_dir(repo_path, base_dir=base_dir)
    if state_dir is None:
        return None
    return os.path.join(state_dir, "index.sqlite")


def to_repo_relative(file_path: str, repo_root: str) -> str | None:
    """Resolves file_path (absolute or already repo-relative) against
    repo_root, rejecting anything that escapes it -- None for an absolute
    path outside repo_root or a relative path containing a ".." that
    walks above it. No filesystem I/O: pure path arithmetic, so this is
    safe to call before repo_root is even known to exist.

    Extracted from notify_hook.py's private _to_repo_relative (codex
    review, 2026-09-02: an unvalidated "../outside.py" diff-header path
    let a crafted agent-hook payload index/tombstone a file outside the
    repo) once staleness.py needed the identical containment check for
    tier 4's file params -- the same untrusted-input shape (a caller-
    supplied path string), the same fix.
    """
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(repo_root, file_path)
    rel_path = os.path.relpath(abs_path, repo_root).replace(os.sep, "/")
    if rel_path == ".." or rel_path.startswith("../"):
        return None
    return rel_path
