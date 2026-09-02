"""Shared .gitignore-aware ignore-rule matching, used by both bootstrap's
walk (dispatch.py's _read_source_files) and the filesystem watcher
(watcher.py) -- see the watcher/incremental-indexing grilling decisions 3
and 9: a single shared predicate so the two never silently disagree about
which files are in a repo's indexable scope.

Composes every .gitignore in a repo (root plus nested), not just the root
file, with real git precedence: a pattern only applies within its own
directory's subtree, and among all patterns that apply to a given path,
the one closest to that path (deepest directory, latest line within a
file) wins -- so a nested .gitignore can both narrow and re-include what a
shallower one decided, exactly like real git.
"""

import os
import threading

from pathspec.patterns.gitignore.basic import GitIgnoreBasicPattern

_GITIGNORE_FILENAME = ".gitignore"


class IgnoreMatcher:
    """Compiled once per repo_root at construction time -- see
    get_ignore_matcher/invalidate below for the process-lifetime cache that
    makes recompilation happen only when a .gitignore file actually changes,
    not on every match() call.
    """

    def __init__(self, repo_root: str) -> None:
        self._repo_root = repo_root
        # Ordered root-to-leaf (os.walk's default topdown=True visits a
        # directory before recursing into its children), each entry scoped
        # to the repo-root-relative directory its .gitignore lives in (""
        # for the root itself). Within one .gitignore, lines keep their own
        # file order. "Last matching pattern in this combined order wins,
        # with negation flipping the outcome" is exactly real git's own
        # precedence rule -- deeper/later patterns naturally override
        # shallower/earlier ones without any separate override-tracking.
        self._ordered_patterns: list[tuple[str, GitIgnoreBasicPattern]] = []
        self._compile()

    def _compile(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self._repo_root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if _GITIGNORE_FILENAME not in filenames:
                continue
            rel_dir = os.path.relpath(dirpath, self._repo_root).replace(os.sep, "/")
            if rel_dir == ".":
                rel_dir = ""
            gitignore_path = os.path.join(dirpath, _GITIGNORE_FILENAME)
            try:
                with open(gitignore_path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                self._ordered_patterns.append((rel_dir, GitIgnoreBasicPattern(line)))

    def matches(self, rel_path: str) -> bool:
        posix_path = rel_path.replace(os.sep, "/")
        if posix_path == ".git" or posix_path.startswith(".git/"):
            return True

        ignored = False
        for dir_prefix, pattern in self._ordered_patterns:
            if dir_prefix:
                if posix_path != dir_prefix and not posix_path.startswith(dir_prefix + "/"):
                    continue
                scoped_path = posix_path[len(dir_prefix) + 1 :]
            else:
                scoped_path = posix_path
            if not scoped_path:
                continue
            if pattern.match_file(scoped_path) is not None:
                ignored = bool(pattern.include)
        return ignored


_cache: dict[str, IgnoreMatcher] = {}
_lock = threading.Lock()


def get_ignore_matcher(repo_root: str) -> IgnoreMatcher:
    """Returns repo_root's cached IgnoreMatcher, compiling it on first use."""
    with _lock:
        matcher = _cache.get(repo_root)
        if matcher is None:
            matcher = IgnoreMatcher(repo_root)
            _cache[repo_root] = matcher
        return matcher


def invalidate(repo_root: str) -> None:
    """Drops repo_root's cached matcher -- the watcher calls this whenever
    it sees any .gitignore (root or nested) change, get added, or get
    deleted, so the next get_ignore_matcher call recompiles from disk.
    """
    with _lock:
        _cache.pop(repo_root, None)
