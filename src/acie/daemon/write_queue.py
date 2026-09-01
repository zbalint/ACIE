"""Per-repo write-queue concurrency for the daemon.

See DAEMON.md "Write-Queue Concurrency". One dedicated writer thread plus
one FIFO queue per repo -- never a single global thread/queue for the
whole daemon -- so one repo's heavy reindex never makes an unrelated
repo's writes wait behind it. Request-handling threads never touch a
repo's write connection directly: they submit a transaction closure via
`WriteQueue.submit` and block on the returned Future for the result.

This module owns write concurrency only. dispatch.py's fresh-per-call
stores (the 8 read-only tools) are a deliberately separate, simpler path
per DAEMON.md's "Store lifecycle: fresh-per-call" -- nothing here is
wired into request dispatch yet; that wiring, and the bootstrap-indexing
pass that will be this queue's first real caller, are later slices.
"""

import queue
import sqlite3
import threading
from concurrent.futures import Future
from typing import Callable, TypeVar

T = TypeVar("T")

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

    def submit(self, repo_key: str, fn: Callable[[sqlite3.Connection], T]) -> "Future[T]":
        """Enqueues fn to run on repo_key's writer thread; returns its Future.

        Creation is lazy and on-demand, per DAEMON.md "Creation": a repo
        key's writer thread and queue spin up on its first submit, never
        pre-created for repos with no write yet. There is deliberately no
        teardown and no cap -- see DAEMON.md's named shortcut on this.
        """
        future: "Future[T]" = Future()
        try:
            worker = self._worker_for(repo_key)
        except BaseException as exc:  # noqa: BLE001 -- failure is reported through submit's Future contract.
            future.set_exception(exc)
            return future
        worker.queue.put((fn, future))
        return future

    def _worker_for(self, repo_key: str) -> "_RepoWriter":
        with self._lock:
            worker = self._workers.get(repo_key)
            if worker is None:
                worker = _RepoWriter(self._db_path_for(repo_key))
                worker.start()
                self._workers[repo_key] = worker
            return worker

    def close(self, timeout: float | None = None) -> None:
        """Drains every repo's queued jobs to completion, then stops its thread.

        DAEMON.md's "Teardown: none" governs a repo going idle during
        normal daemon operation -- this is the separate drain-to-completion
        path "Shutdown Semantics" names for the daemon process's own exit,
        and it's also what test fixtures need to avoid leaking threads.
        """
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.queue.put(_STOP)
        for worker in workers:
            worker.thread.join(timeout=timeout)


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
            conn = sqlite3.connect(db_path)
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
