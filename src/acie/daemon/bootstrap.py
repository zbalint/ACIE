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

    `db_path_for` and `walk_repo` are injected exactly like `WriteQueue`'s
    own `db_path_for` -- production wiring (resolving `repo_path` to an
    actual repo root and reading real files off disk) belongs to the
    daemon-server slice that constructs the real callables; tests here
    supply fakes so no real git repo or filesystem walk is needed.
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

    def repo_ready(self, repo_key: str) -> bool:
        """The exact `repo_ready` callable dispatch.dispatch_request requires.

        The disk-existence check only ever applies to a repo_key this
        coordinator has never touched (no prior register() call this
        process lifetime): opening the write queue's per-repo connection
        creates repo_key's sqlite file on disk immediately, well before
        that connection's first write is committed, so while a bootstrap
        for repo_key is in flight this must trust `_in_progress`, not the
        file's mere existence, or a concurrent caller could observe a
        just-created, still-empty (or partially written) index as ready.
        """
        with self._lock:
            if repo_key in self._ready:
                return True
            if repo_key in self._in_progress:
                return False
        if os.path.exists(self._db_path_for(repo_key)):
            with self._lock:
                self._ready.add(repo_key)
            return True
        return False

    def register(self, repo_key: str) -> None:
        """Idempotent: starts repo_key's bootstrap walk if it hasn't already.

        Returns immediately -- per DAEMON.md, bootstrap indexing "kicks off
        a full walk-and-index pass in the background ... rather than
        blocking the caller". The walk itself runs on its own thread (not
        the write-queue's writer thread, which only ever does DB work), and
        each discovered file becomes one write-queue job, matching
        `index_file`'s existing one-file-per-write-queue-item granularity.
        """
        if self.repo_ready(repo_key):
            return
        with self._lock:
            if repo_key in self._in_progress:
                return
            self._in_progress.add(repo_key)
        threading.Thread(target=self._run_bootstrap, args=(repo_key,), daemon=True).start()

    def _run_bootstrap(self, repo_key: str) -> None:
        try:
            files = list(self._walk_repo(repo_key))
        except BaseException:
            with self._lock:
                self._in_progress.discard(repo_key)
            raise

        if not files:
            self._mark_ready(repo_key)
            return

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
                self._mark_ready(repo_key)

        def make_job(path: str, source_text: str):
            def job(conn):
                index_file(
                    path=path, source_text=source_text,
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    symbol_store=SymbolStore(conn=conn),
                    relation_store=RelationStore(conn=conn),
                    index_meta_store=IndexMetaStore(conn=conn),
                )
            return job

        for path, source_text in files:
            self._write_queue.submit(repo_key, make_job(path, source_text)).add_done_callback(on_job_done)

    def _mark_ready(self, repo_key: str) -> None:
        with self._lock:
            self._in_progress.discard(repo_key)
            self._ready.add(repo_key)
