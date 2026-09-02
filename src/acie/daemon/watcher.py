"""Tier 1 of ARCHITECTURE.md's incremental-indexing precedence: an
OS-level filesystem watcher that reindexes a repo's changed files with
zero cooperation from any editor/agent/tool.

See the watcher/incremental-indexing grilling session (SALTMDB decision
f4bdfc9d) for the full set of locked decisions this module implements:
- decision 1: hybrid mtime-then-hash staleness check (_make_watch_job).
- decision 2: ~500ms debounce/coalescing window (_DebouncedEventHandler).
- decision 5: a watcher starts lazily on a repo's first register() call
  and lives for the daemon's whole process life, same as WriteQueue's
  writer threads -- no idle/teardown built here either.
- decision 6: no cross-repo routing table -- one watcher instance per
  repo, its closures already know their own repo_id by construction.
- decision 7: no cross-tier dedup against tier 2/3 -- a duplicate
  single-file reindex is cheap/idempotent, so this isn't built.
- decision 13: delete/rename reuse index_file(path, "") as-is (already
  diff-tombstone-capable, see indexer.py) -- a rename is just two touched
  paths (old + new), handled by the exact same per-path job.

See also decision 10's follow-up fix (SALTMDB f4bdfc9d, grilled to a
locked plan 2026-09-02): WatcherRegistry keys on repo_root (RepoWatcher's
own write-queue submissions use the canonical repo_id instead) so two
worktrees of one repo get two Observers sharing one write-queue worker,
and two spellings of one worktree never get a duplicate Observer.
"""

import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from acie.daemon import ignore
from acie.daemon.write_queue import WriteQueue
from acie.indexer import index_file
from acie.storage.file_state_store import FileStateStore
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore

# v0 is Python-only (ARCHITECTURE.md) -- same extension bootstrap's
# _read_source_files already scopes to.
_SOURCE_EXTENSION = ".py"
_GITIGNORE_FILENAME = ".gitignore"

# decision 2: ~500ms -- short enough a plain human edit (the case with no
# tier-2/tier-3 accelerant active) doesn't feel laggy, long enough to
# coalesce a burst of rapid-fire events into one reindex per file.
_DEBOUNCE_SECONDS = 0.5


def _make_watch_job(repo_root: str, rel_path: str) -> Callable[[sqlite3.Connection], None]:
    """One write-queue job for one touched repo-relative path.

    Same job for a create, an edit, a delete, or either half of a rename --
    the on-disk state of `rel_path` at the moment this job actually runs
    (not at the moment the fs event fired) is the only thing that matters,
    so a rapid create-then-delete within one debounce window collapses
    correctly with no special-case event-type branching.
    """

    def job(conn: sqlite3.Connection) -> None:
        file_state_store = FileStateStore(conn=conn)
        abs_path = os.path.join(repo_root, rel_path)
        observed_at = datetime.now(timezone.utc).isoformat()

        try:
            stat_result = os.stat(abs_path)
        except OSError:
            # Gone (deleted, or renamed away) -- tombstone whatever was
            # last indexed for this path, if anything was. Gated on
            # SymbolStore, not file_state_store: bootstrap indexes files
            # directly (index_file) without ever writing FileStateStore
            # (only this watcher's own create/modify path does), so a
            # bootstrap-only-indexed file's delete would otherwise be
            # silently ignored (codex review, 2026-09-02). SymbolStore is
            # the actual source of truth for "was this path ever indexed
            # by anything"; file_state_store is merely this watcher's own
            # staleness cache.
            symbol_store = SymbolStore(conn=conn)
            if not symbol_store.list_by_path(rel_path):
                file_state_store.delete(rel_path)
                return
            index_file(
                path=rel_path, source_text="", observed_at=observed_at,
                symbol_store=symbol_store, relation_store=RelationStore(conn=conn),
                index_meta_store=IndexMetaStore(conn=conn),
            )
            # index_file's own diff-tombstone logic (indexer.py) removes
            # every symbol/relation that extraction no longer produces --
            # but extract_symbols always yields one module-kind symbol
            # even for an empty source (same stable id regardless of
            # content), so it survives that diff untouched. A deleted file
            # has no module either; clear whatever's left explicitly.
            for symbol in symbol_store.list_by_path(rel_path):
                symbol_store.delete(symbol.id, observed_at=observed_at)
            file_state_store.delete(rel_path)
            return

        mtime_ns = stat_result.st_mtime_ns
        prior = file_state_store.get(rel_path)
        if prior is not None and prior.mtime_ns == mtime_ns:
            # decision 1's cheap check: mtime unchanged means no reindex,
            # no read, no hash -- this is the common case for the vast
            # majority of watcher events (most fs notifications settle on
            # an unchanged file, e.g. an editor's atomic-save temp-file
            # dance touching the directory but not this exact path again).
            return

        try:
            with open(abs_path, encoding="utf-8") as f:
                source_text = f.read()
        except (OSError, UnicodeDecodeError):
            # Unreadable right now (mid-write, permissions, binary
            # content) -- not fatal, no state recorded, so the next real
            # event for this path (if any) retries from scratch.
            return

        content_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if prior is not None and prior.content_hash == content_hash:
            # mtime moved but the bytes didn't (e.g. a `touch`, or a save
            # that rewrote identical content) -- record the new mtime so
            # future checks stay cheap, but skip the pointless reindex.
            file_state_store.set(rel_path, mtime_ns=mtime_ns, content_hash=content_hash)
            return

        index_file(
            path=rel_path, source_text=source_text, observed_at=observed_at,
            symbol_store=SymbolStore(conn=conn), relation_store=RelationStore(conn=conn),
            index_meta_store=IndexMetaStore(conn=conn),
        )
        file_state_store.set(rel_path, mtime_ns=mtime_ns, content_hash=content_hash)

    return job


class _DebouncedEventHandler(FileSystemEventHandler):
    """Coalesces raw watchdog events into one on_paths_changed(set[str])
    call per debounce window -- watchdog itself has no built-in debounce.
    """

    def __init__(
        self, repo_root: str, on_paths_changed: Callable[[set[str]], None],
        debounce_seconds: float = _DEBOUNCE_SECONDS,
    ) -> None:
        self._repo_root = repo_root
        self._on_paths_changed = on_paths_changed
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._timer: threading.Timer | None = None

    def _touch(self, abs_path: str) -> None:
        rel_path = os.path.relpath(abs_path, self._repo_root).replace(os.sep, "/")
        if rel_path == "." or rel_path.startswith(".."):
            return  # outside repo_root -- shouldn't happen, defensive only.

        if os.path.basename(rel_path) == _GITIGNORE_FILENAME:
            # A .gitignore's own content just changed -- recompile before
            # deciding whether anything (including this file itself) is
            # ignored, or a just-loosened/tightened rule would use stale
            # matcher state for this very event.
            ignore.invalidate(self._repo_root)

        if ignore.get_ignore_matcher(self._repo_root).matches(rel_path):
            return
        if not rel_path.endswith(_SOURCE_EXTENSION):
            return

        with self._lock:
            self._pending.add(rel_path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = self._pending
            self._pending = set()
            self._timer = None
        if paths:
            self._on_paths_changed(paths)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._touch(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._touch(event.src_path)

    def on_deleted(self, event) -> None:
        # shortcut: a directory-delete event (event.is_directory True) is
        # not decomposed into per-file tombstone jobs for everything that
        # was under it -- inotify/watchdog's own per-file delete events
        # for files inside a recursively-removed directory are relied on
        # instead, which is not guaranteed on every backend/timing. Upgrade
        # trigger: if a real repo shows stale symbols surviving a `rm -rf`
        # of an indexed subdirectory, walk FileStateStore for the deleted
        # prefix here and tombstone each tracked path explicitly.
        if not event.is_directory:
            self._touch(event.src_path)

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._touch(event.src_path)
            self._touch(event.dest_path)


class RepoWatcher:
    """One OS-level watch on one repo's root, submitting a write-queue job
    per touched path once its debounce window closes. decision 6: this
    instance's closures already know their own repo_id, so no separate
    path -> repo routing table is needed anywhere.
    """

    def __init__(
        self, repo_root: str, repo_id: str, write_queue: WriteQueue,
        *, debounce_seconds: float = _DEBOUNCE_SECONDS,
    ) -> None:
        self._repo_root = repo_root
        self._repo_id = repo_id
        self._write_queue = write_queue
        self._handler = _DebouncedEventHandler(
            repo_root, self._on_paths_changed, debounce_seconds=debounce_seconds
        )
        self._observer = Observer()
        self._observer.schedule(self._handler, repo_root, recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def _on_paths_changed(self, rel_paths: set[str]) -> None:
        for rel_path in rel_paths:
            self._write_queue.submit(self._repo_id, _make_watch_job(self._repo_root, rel_path))

    def stop(self, timeout: float | None = None) -> None:
        self._observer.stop()
        self._observer.join(timeout=timeout)


class WatcherRegistry:
    """Lazily creates and owns one RepoWatcher per repo_root -- same shape
    as WriteQueue/BootstrapCoordinator (decision 5: no idle/teardown, a
    watcher lives for the daemon's whole process life once created).

    Keyed by repo_root, not repo_id (decision 10, SALTMDB f4bdfc9d/
    repo_path-vs-resolve_repo_id keying fix): repo_root is the actual
    directory an Observer watches, and two genuinely different worktree
    directories legitimately need two separate Observers even though they
    share one repo_id -- but repo_root is already realpath'd/canonical
    (repo_id.py's resolve_repo_root), so two different repo_path spellings
    of the *same* worktree (a symlink vs its realpath'd twin) still
    correctly collapse to one Observer here. Each RepoWatcher's own
    write-queue submissions use the given repo_id, not its registry key, so
    every worktree's live edits land in the one shared write-queue worker.
    """

    def __init__(self, write_queue: WriteQueue) -> None:
        self._write_queue = write_queue
        self._lock = threading.Lock()
        self._watchers: dict[str, RepoWatcher] = {}

    def register(self, repo_id: str, repo_root: str) -> None:
        with self._lock:
            if repo_root in self._watchers:
                return
            self._watchers[repo_root] = RepoWatcher(repo_root, repo_id, self._write_queue)

    def close(self, timeout: float | None = None) -> None:
        with self._lock:
            watchers = list(self._watchers.values())
        for watcher in watchers:
            watcher.stop(timeout=timeout)
