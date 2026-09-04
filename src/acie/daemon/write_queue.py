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
        rather than a job that silently never runs.

        One caller is exempt from that rejection: a repo's own writer
        thread, submitting a follow-up job from inside a job's completion
        callback (`concurrent.futures.Future.set_result()` runs registered
        done-callbacks synchronously, on whichever thread calls it -- for a
        write-queue job that's the writer thread itself, still inside
        `_RepoWriter._run`'s loop -- see `BootstrapCoordinator.
        _run_indexing_pass`'s `on_job_done` -> `_mark_ready`/
        `_mark_cross_file_migration_done`). Rejecting that submit outright
        wouldn't be a fix, just a differently-silent drop (its Future's
        exception is never checked by that caller either) -- SALTMDB
        bf5a5a98.

        Enqueueing that follow-up isn't enough by itself, though: `_STOP`
        is a plain FIFO item, and `close()` can legally put it the instant
        *any* worker's queue looks momentarily empty -- including the
        window between a job being accepted here and the writer thread
        ever dequeuing it, well before that job's own completion callback
        has run. A first attempt at this fix serialized submit()'s enqueue
        against close()'s `_STOP`-put with a per-worker lock held only
        *during* a job's execution -- that closes the narrower "callback
        racing close() while its job is still running" window, but not
        this wider one, and was caught failing >50% of the time under
        stress once instrumented (2026-09-04 stress run, see the
        SALTMDB writeup this fix is documented in). `worker.enqueue_lock`
        below (paired with `_RepoWriter._in_flight`/`wait_until_quiescent`
        in `close()`) is the actual, verified fix: every accepted submit
        increments `_in_flight` in the same locked step that enqueues the
        job, and it's only decremented once that job (and, transitively,
        any reentrant follow-up its own resolution enqueues) has fully
        resolved -- so `close()` can safely put `_STOP` only once it has
        observed `_in_flight == 0` under that same lock, which is the
        first point no more causally-connected work can still land.
        """
        future: "Future[T]" = Future()
        existing_worker = self._workers.get(repo_key)
        is_reentrant = existing_worker is not None and threading.current_thread() is existing_worker.thread
        if self._closed.is_set() and not is_reentrant:
            future.set_exception(RuntimeError(f"WriteQueue is closed, cannot submit for {repo_key!r}"))
            return future
        try:
            worker = self._worker_for(repo_key)
        except BaseException as exc:  # noqa: BLE001 -- failure is reported through submit's Future contract.
            future.set_exception(exc)
            return future
        with worker.enqueue_lock:
            if self._closed.is_set() and not is_reentrant:
                future.set_exception(RuntimeError(f"WriteQueue is closed, cannot submit for {repo_key!r}"))
                return future
            worker._in_flight += 1
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
        deadline = None if timeout is None else time.monotonic() + timeout
        for _repo_key, worker in workers:
            # See submit()'s docstring: wait_until_quiescent (bounded by
            # this worker's share of the shutdown budget, same as
            # thread.join below -- a stuck job must not make close() wait
            # forever here either) only returns once no job for this
            # worker is in flight, which is the first point _STOP can be
            # enqueued without a causally-connected follow-up landing
            # behind it.
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            with worker.enqueue_lock:
                worker.wait_until_quiescent(remaining)
                worker.queue.put(_STOP)
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
        # Guards `queue.put()` (both submit()'s enqueue and close()'s
        # _STOP) and `_in_flight` together, so incrementing/enqueueing and
        # decrementing/checking-quiescent can never interleave in a way
        # that lets _STOP land ahead of a job already accepted before
        # close() began -- see submit()'s and _in_flight's own docstrings
        # (SALTMDB bf5a5a98). Reentrant: a job's own reentrant follow-up
        # submit (from within its completion callback, on this worker's
        # own thread) legitimately re-acquires it while `close()` itself
        # may be waiting on `_quiescent` (which releases the lock while
        # waiting, per Condition's contract) -- a plain Lock isn't needed
        # here since nothing holds this lock across a blocking wait on
        # itself from the same thread, but RLock costs nothing and keeps
        # this safe against a future change that does.
        self.enqueue_lock = threading.RLock()
        self._quiescent = threading.Condition(self.enqueue_lock)
        # Jobs accepted (enqueued, whether still sitting in the queue,
        # currently running, or resolving their own completion-callback
        # chain) but not yet *fully* resolved. Incremented in the same
        # locked step as the enqueue (submit()); decremented only after
        # future.set_result()/set_exception() has completely returned
        # (_run below) -- which includes any reentrant submit() a done-
        # callback makes, since that submit's own increment necessarily
        # happens *before* the outer job's decrement is reached. So
        # `_in_flight == 0`, observed under `enqueue_lock`, is a true
        # "no more causally-connected work can still arrive" signal --
        # unlike just checking `queue.empty()`, which says nothing about
        # a job that's been dequeued but hasn't resolved (and thus
        # possibly reentrantly resubmitted) yet.
        self._in_flight = 0

    def start(self) -> None:
        self.thread.start()
        self._started.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def wait_until_quiescent(self, timeout: float | None) -> bool:
        """Blocks until no job is in flight for this worker (see `_in_flight`).

        Caller must already hold `enqueue_lock` -- close() takes it right
        before calling this and keeps holding it through the `_STOP` put
        that follows, so nothing can slip a new job in between "confirmed
        quiescent" and "STOP enqueued". Returns whether it actually
        reached quiescence within `timeout` (None = unbounded); a stuck
        job (see `test_close_is_bounded_and_warns_when_a_writer_thread_is_
        stuck`) must not be able to make this wait forever any more than
        `thread.join()` may -- close() still puts `_STOP` and proceeds to
        `join()` either way, matching its existing bounded-shutdown
        contract.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._in_flight != 0:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._quiescent.wait(timeout=remaining)
        return True

    def _mark_resolved(self) -> None:
        """Decrements `_in_flight` for one fully-resolved job and wakes
        any `wait_until_quiescent()` call that might now be satisfied."""
        with self.enqueue_lock:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._quiescent.notify_all()

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
                    self._mark_resolved()
                    continue
                try:
                    result = fn(conn)
                except BaseException as exc:  # noqa: BLE001 -- propagated to the caller via the Future, never swallowed.
                    future.set_exception(exc)
                else:
                    # May synchronously run done-callbacks (e.g.
                    # BootstrapCoordinator's on_job_done) that call
                    # WriteQueue.submit() again for this same repo_key --
                    # _mark_resolved() below only runs, and _in_flight
                    # only drops, once any such reentrant follow-up has
                    # already incremented it back up -- see _in_flight's
                    # own docstring.
                    future.set_result(result)
                self._mark_resolved()
        finally:
            conn.close()
