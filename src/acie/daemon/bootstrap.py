"""Bootstrap indexing and the daemon's per-repo readiness flag.

See DAEMON.md "Bootstrap Indexing & INDEX_NOT_READY". `BootstrapCoordinator`
is the real seam behind dispatch.py's `repo_ready` parameter (a fake in
Slice 2's tests) and is `WriteQueue`'s first real caller (write_queue.py's
own docstring named this as deferred to a later slice).

Readiness is a daemon-process-lifetime concept: DAEMON.md's on-disk layout
names no separate "bootstrap complete" marker, and tiers 1-3 of incremental
indexing (watcher/git-hooks/agent-hooks) are out of this phase's scope --
so a repo that already has an index.sqlite from any prior run is trusted as
ready immediately, and only a repo with no index.sqlite yet gets a walk.
"""

import os
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable

from acie.daemon.write_queue import WriteQueue
from acie.indexer import index_file
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore


class BootstrapCoordinator:
    """Tracks per-repo readiness and drives a repo's first walk-and-index pass.

    `db_path_for` (repo_id -> index.sqlite path) and `walk_repo` (repo_root
    -> discovered files) are injected exactly like `WriteQueue`'s own
    `db_path_for` -- production wiring (resolving a raw `repo_path` to the
    canonical `repo_id`/`repo_root` pair register() takes, and reading real
    files off disk) belongs to the daemon-server slice that constructs the
    real callables; tests here supply fakes so no real git repo or
    filesystem walk is needed.
    """

    def __init__(
        self,
        write_queue: WriteQueue,
        db_path_for: Callable[[str], str],
        walk_repo: Callable[[str], Iterable[tuple[str, str]]],
    ) -> None:
        self._write_queue = write_queue
        self._db_path_for = db_path_for
        self._walk_repo = walk_repo
        self._lock = threading.Lock()
        self._ready: set[str] = set()
        self._in_progress: set[str] = set()
        self._migration_checked: set[str] = set()

    def repo_ready(self, repo_id: str) -> bool:
        """The exact `repo_ready` callable dispatch.dispatch_request requires.

        The disk-existence check only ever applies to a repo_id this
        coordinator has never touched (no prior register() call this
        process lifetime): opening the write queue's per-repo connection
        creates repo_id's sqlite file on disk immediately, well before
        that connection's first write is committed, so while a bootstrap
        for repo_id is in flight this must trust `_in_progress`, not the
        file's mere existence, or a concurrent caller could observe a
        just-created, still-empty (or partially written) index as ready.
        """
        with self._lock:
            if repo_id in self._ready:
                return True
            if repo_id in self._in_progress:
                return False
        if os.path.exists(self._db_path_for(repo_id)):
            with self._lock:
                self._ready.add(repo_id)
            return True
        return False

    def register(self, repo_id: str, repo_root: str) -> None:
        """Idempotent: starts repo_id's bootstrap walk if it hasn't already.

        Two params, not one: readiness/write-queue bookkeeping is keyed on
        `repo_id` (the canonical, worktree-collapsing identity -- decision
        10, SALTMDB f4bdfc9d/repo_path-vs-resolve_repo_id keying fix), but
        `walk_repo` needs an actual on-disk directory to walk, which a
        repo_id hash cannot be reversed back into -- so the caller (already
        holding both, having resolved them together) passes `repo_root`
        through explicitly rather than this class hiding a second lookup.

        Returns immediately -- per DAEMON.md, bootstrap indexing "kicks off
        a full walk-and-index pass in the background ... rather than
        blocking the caller". The walk itself runs on its own thread (not
        the write-queue's writer thread, which only ever does DB work), and
        each discovered file becomes one write-queue job, matching
        `index_file`'s existing one-file-per-write-queue-item granularity.

        For a repo that's *already* ready (an existing index.sqlite this
        coordinator never walked itself this process lifetime -- most
        commonly an installation upgraded to a version with cross-file call
        resolution), this instead schedules the one-time migration catch-up
        pass (see `_maybe_schedule_cross_file_migration`) rather than
        returning as a pure no-op -- codex review finding: without this, an
        already-indexed repo would never gain cross-file call resolution
        except by chance, one edited file at a time.
        """
        if self.repo_ready(repo_id):
            self._maybe_schedule_cross_file_migration(repo_id, repo_root)
            return
        with self._lock:
            if repo_id in self._in_progress:
                return
            self._in_progress.add(repo_id)
        threading.Thread(target=self._run_bootstrap, args=(repo_id, repo_root), daemon=True).start()

    def _run_bootstrap(self, repo_id: str, repo_root: str) -> None:
        try:
            files = list(self._walk_repo(repo_root))
        except BaseException:
            with self._lock:
                self._in_progress.discard(repo_id)
            raise

        if not files:
            # A genuinely empty repo never gets a write_queue submission at
            # all (see repo_ready()'s own docstring: opening the per-repo
            # connection creates index.sqlite on disk immediately) -- so
            # there is no index.sqlite to persist the migration flag into,
            # and nothing to migrate anyway. Skip it; a future daemon
            # restart will just cheaply re-walk-and-immediately-ready again.
            self._mark_ready(repo_id)
            return

        def on_bootstrap_done() -> None:
            self._mark_ready(repo_id)
            # A from-scratch bootstrap already gets the full two-pass
            # treatment below -- mark the migration flag done too, so a
            # later daemon restart's register() call never redundantly
            # re-walks this repo a third time via _maybe_schedule_cross_file_migration.
            self._mark_cross_file_migration_done(repo_id)

        # Second pass: os.walk's file-discovery order (this class's own
        # `_walk_repo` contract) is arbitrary, not dependency-ordered, so a
        # file indexed before something it cross-file-calls/inherits/overrides
        # into leaves that edge unresolved (see extract_relations.py's
        # DeferredImportCall/DeferredImportInherit/DeferredImportOverride --
        # indexer.py's _resolve_deferred/_resolve_deferred_overrides -- no
        # retarget-in-place primitive exists to fix it after the fact). Re-running every
        # file's index_file once more, now that the full repo's SymbolStore
        # is populated from the first pass, resolves whatever the first
        # pass's arbitrary order missed -- a bounded, one-time cost at
        # registration, not a steady-state one. Only the initial
        # registration walk gets this; tier 1-3 incremental reindexing
        # (watcher/git-hooks/agent-hooks) doesn't re-run this second pass.
        self._run_indexing_pass(
            repo_id, files, on_pass_done=lambda: self._run_indexing_pass(repo_id, files, on_pass_done=on_bootstrap_done)
        )

    def _maybe_schedule_cross_file_migration(self, repo_id: str, repo_root: str) -> None:
        """One-time catch-up for a repo that was already `repo_ready()`
        before this call -- either indexed under pre-cross-file-resolution
        code, or (harmlessly redundant but still correct) one this
        coordinator already fully bootstrapped itself this process
        lifetime. `_migration_checked` only dedupes *within* this process;
        the actual "already done" answer is `IndexMetaStore.
        cross_file_pass_done()`, persisted to the repo's own index.sqlite so
        it survives a daemon restart -- checked from a write-queue job (not
        a fresh ad hoc connection) to stay on the one connection/thread each
        repo's writer already owns, same discipline as every other write in
        this codebase.
        """
        with self._lock:
            if repo_id in self._migration_checked:
                return
            self._migration_checked.add(repo_id)
        threading.Thread(
            target=self._run_cross_file_migration_if_needed, args=(repo_id, repo_root), daemon=True
        ).start()

    def _run_cross_file_migration_if_needed(self, repo_id: str, repo_root: str) -> None:
        def check_job(conn) -> bool:
            return IndexMetaStore(conn=conn).cross_file_pass_done()

        already_done = self._write_queue.submit(repo_id, check_job).result()
        if already_done:
            return

        try:
            files = list(self._walk_repo(repo_root))
        except BaseException:
            # Best-effort catch-up: repo_ready() was already true before
            # this ran, so a failed walk here must not affect it -- unlike
            # _run_bootstrap, there is no INDEX_NOT_READY state to fall
            # back into. A later register() call will simply retry (this
            # process's own _migration_checked guard only blocks a *second*
            # concurrent attempt, not a future one, since this method
            # returns without ever marking the flag done on failure).
            with self._lock:
                self._migration_checked.discard(repo_id)
            return

        if not files:
            self._mark_cross_file_migration_done(repo_id)
            return
        self._run_indexing_pass(repo_id, files, on_pass_done=lambda: self._mark_cross_file_migration_done(repo_id))

    def _run_indexing_pass(
        self, repo_id: str, files: list[tuple[str, str]], *, on_pass_done: Callable[[], None]
    ) -> None:
        remaining = [len(files)]
        remaining_lock = threading.Lock()

        def on_job_done(future) -> None:
            # shortcut: a failing file -- or one whose submission never
            # even reached a writer thread (e.g. a writer-startup failure
            # in WriteQueue.submit()) -- still counts toward "done" so one
            # bad file can't wedge the repo in INDEX_NOT_READY forever, but
            # its exception is never surfaced anywhere else since
            # bootstrap doesn't retain per-file futures. Upgrade trigger:
            # add error aggregation/logging once silent per-file bootstrap
            # failures need visibility.
            #
            # Attached via add_done_callback rather than embedded in the
            # job closure's own finally block: a concurrent.futures.Future
            # guarantees this fires exactly once whether the job ran to
            # completion, raised, or WriteQueue.submit() failed before the
            # job was ever enqueued at all (that Future comes back already
            # failed, and add_done_callback on an already-done Future
            # still calls its callback immediately).
            with remaining_lock:
                remaining[0] -= 1
                done = remaining[0] == 0
            if done:
                on_pass_done()

        for path, source_text in files:
            self._write_queue.submit(repo_id, _make_index_job(path, source_text)).add_done_callback(on_job_done)

    def _mark_ready(self, repo_id: str) -> None:
        with self._lock:
            self._in_progress.discard(repo_id)
            self._ready.add(repo_id)

    def _mark_cross_file_migration_done(self, repo_id: str) -> None:
        def mark_job(conn) -> None:
            IndexMetaStore(conn=conn).mark_cross_file_pass_done()

        self._write_queue.submit(repo_id, mark_job)


def _make_index_job(path: str, source_text: str):
    def job(conn):
        index_file(
            path=path, source_text=source_text,
            observed_at=datetime.now(timezone.utc).isoformat(),
            symbol_store=SymbolStore(conn=conn),
            relation_store=RelationStore(conn=conn),
            index_meta_store=IndexMetaStore(conn=conn),
        )
    return job
