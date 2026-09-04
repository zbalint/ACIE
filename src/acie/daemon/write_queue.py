"""Per-repo write-queue concurrency for the daemon.

See DAEMON.md "Write-Queue Concurrency". One dedicated writer thread plus
one FIFO queue per repo -- never a single global thread/queue for the
whole daemon -- so one repo's heavy reindex never makes an unrelated
repo's writes wait behind it. Request-handling threads never touch a
repo's write connection directly: they submit a transaction closure via
`WriteQueue.submit` and block on the returned Future for the result.

This module owns write concurrency only. dispatch.py's fresh-per-call
stores (the 9 read-only tools) are a deliberately separate, simpler path
per DAEMON.md's "Store lifecycle: fresh-per-call" -- nothing here is
wired into request dispatch yet; that wiring, and the bootstrap-indexing
pass that will be this queue's first real caller, are later slices.
"""

import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from typing import Callable, TypeVar

from acie.storage.connection import open_connection

T = TypeVar("T")
_logger = logging.getLogger(__name__)

# Sentinel enqueued by close() to stop a repo's writer thread once it has
# drained every job submitted before the close.
_STOP = object()


class WriteQueue:
    """Lazily creates and owns one writer thread + FIFO queue per repo key.

    `db_path_for` resolves a repo key (whatever the daemon uses to name a
    repo -- `repo_path` in DAEMON.md's envelope) to that repo's
    index.sqlite path. Injected rather than hardcoded to
    `resolve_index_db_path` so tests can point repo keys directly at
    `:memory:` or tmp sqlite files without a real git repo.
    """

    def __init__(self, db_path_for: Callable[[str], str]) -> None:
        self._db_path_for = db_path_for
        self._lock = threading.Lock()
        self._workers: dict[str, "_RepoWriter"] = {}
        # One creation lock per repo key, guarding the (potentially slow --
        # db_path_for and _RepoWriter.start()'s blocking sqlite3.connect())
        # first-worker-creation path for that key only. Never garbage
        # collected, same "no teardown, no cap" shortcut this class's own
        # docstring already accepts for _workers.
        self._creation_locks: dict[str, threading.Lock] = {}
        # Set once close() starts (codex review, 2026-09-02): _workers'
        # entries are never removed on close, so a submit() landing after
        # a repo's writer thread has already consumed its _STOP sentinel
        # and exited would otherwise reuse that dead worker's queue
        # silently -- the job would sit forever, never processed, instead
        # of erroring. See submit()'s own check below.
        self._closed = threading.Event()

    def submit(self, repo_key: str, fn: Callable[[sqlite3.Connection], T]) -> "Future[T]":
        """Enqueues fn to run on repo_key's writer thread; returns its Future.

        Creation is lazy and on-demand, per DAEMON.md "Creation": a repo
        key's writer thread and queue spin up on its first submit, never
        pre-created for repos with no write yet. There is deliberately no
        teardown and no cap -- see DAEMON.md's named shortcut on this.

        Once `close()` has started, this fails fast with the Future's
        exception set instead of enqueueing -- a late caller (e.g. a
        RepoWatcher's debounce timer racing shutdown) gets a clear error
        rather than a job that silently never runs. This check and the
        actual enqueue below aren't under one atomic lock together
        (`close()`'s own fast path deliberately never takes `self._lock`
        either -- see `_worker_for`'s docstring), so a submit landing in
        the exact instant between this check and `worker.queue.put()`
        below, concurrently with `close()`'s own STOP-sentinel loop, is
        not fully closed by this alone -- shortcut: accepted as a
        vanishingly narrow window against an already-shutting-down
        daemon, not worth a per-worker lock shared with close() unless a
        real occurrence shows up.
        """
        future: "Future[T]" = Future()
        if self._closed.is_set():
            future.set_exception(RuntimeError(f"WriteQueue is closed, cannot submit for {repo_key!r}"))
            return future
        try:
            worker = self._worker_for(repo_key)
        except BaseException as exc:  # noqa: BLE001 -- failure is reported through submit's Future contract.
            future.set_exception(exc)
            return future
        worker.queue.put((fn, future))
        return future

    def _worker_for(self, repo_key: str) -> "_RepoWriter":
        # Double-checked locking: the fast path (worker already exists)
        # never touches a lock at all. A first-ever call for repo_key
        # briefly holds the global lock only to fetch/create that key's own
        # creation lock -- the slow work (db_path_for, worker.start()'s
        # blocking sqlite3.connect()) then runs under that per-repo lock,
        # never the global one, so a slow repo never stalls an unrelated
        # repo's own first submit.
        worker = self._workers.get(repo_key)
        if worker is not None:
            return worker
        with self._lock:
            creation_lock = self._creation_locks.setdefault(repo_key, threading.Lock())
        with creation_lock:
            worker = self._workers.get(repo_key)
            if worker is None:
                worker = _RepoWriter(self._db_path_for(repo_key))
                worker.start()
                with self._lock:
                    self._workers[repo_key] = worker
            return worker

    def close(self, timeout: float | None = None) -> None:
        """Drains every repo's queued jobs to completion, then stops its thread.

        DAEMON.md's "Teardown: none" governs a repo going idle during
        normal daemon operation -- this is the separate drain-to-completion
        path "Shutdown Semantics" names for the daemon process's own exit,
        and it's also what test fixtures need to avoid leaking threads.

        `timeout`, unlike `submit`'s per-job unboundedness, must actually
        bound this call *overall* in production (see runtime.py's
        `on_shutdown`, SALTMDB ebff13f5 + its codex-review follow-up,
        2026-09-02): a writer thread stuck on a single stuck job (a hung
        `git` subprocess, a wedged syscall) must not be able to block the
        daemon's `shutdown()` forever, and M stuck writers must not each
        get their own full `timeout` (that would make the total bound
        M * `timeout`, not `timeout`) -- a deadline computed once and
        re-diffed against the clock before each writer's own
        `thread.join()` means a slow/stuck writer eats into the budget
        remaining for the rest. A thread still alive after its share of
        the deadline is logged (naming its repo key) and left running in
        the background rather than retried or escalated.
        """
        self._closed.set()
        with self._lock:
            workers = list(self._workers.items())
        for _repo_key, worker in workers:
            worker.queue.put(_STOP)
        deadline = None if timeout is None else time.monotonic() + timeout
        for repo_key, worker in workers:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            worker.thread.join(timeout=remaining)
            if worker.thread.is_alive():
                _logger.warning(
                    "WriteQueue writer thread for repo %r did not finish "
                    "draining within its shutdown budget -- proceeding "
                    "with shutdown anyway; it is left running in the "
                    "background.",
                    repo_key,
                )


class _RepoWriter:
    def __init__(self, db_path: str) -> None:
        self.queue: "queue.Queue" = queue.Queue()
        self.thread = threading.Thread(target=self._run, args=(db_path,), daemon=True)
        self._started = threading.Event()
        self._startup_error: BaseException | None = None

    def start(self) -> None:
        self.thread.start()
        self._started.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _run(self, db_path: str) -> None:
        # Opened once at thread creation and reused for every job this
        # repo ever submits -- the deliberate contrast with dispatch.py's
        # fresh-per-call read-path stores (see this module's docstring).
        try:
            conn = open_connection(db_path)
        except BaseException as exc:  # noqa: BLE001 -- returned via submit's Future, never stranded in this thread.
            self._startup_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                item = self.queue.get()
                if item is _STOP:
                    return
                fn, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(conn)
                except BaseException as exc:  # noqa: BLE001 -- propagated to the caller via the Future, never swallowed.
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            conn.close()
