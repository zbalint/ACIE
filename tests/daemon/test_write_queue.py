import logging
import sqlite3
import threading
import time

import pytest

from acie.daemon.write_queue import WriteQueue


def test_submit_runs_fn_against_a_connection_and_returns_its_result_via_the_future():
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")

    future = wq.submit("repo-a", lambda conn: 1 + 1)

    assert future.result(timeout=1) == 2
    wq.close()


def test_db_path_is_not_resolved_until_the_first_submit_for_that_repo_key():
    resolved = []

    def db_path_for(repo_key):
        resolved.append(repo_key)
        return ":memory:"

    wq = WriteQueue(db_path_for=db_path_for)
    assert resolved == []

    wq.submit("repo-a", lambda conn: None).result(timeout=1)
    assert resolved == ["repo-a"]
    wq.close()


def test_same_repo_key_reuses_one_connection_opened_once_at_thread_creation():
    # :memory: is destroyed the instant its connection closes -- if two
    # submits for the same repo_key got separate connections, the second
    # job's SELECT would fail with "no such table" instead of seeing the
    # first job's INSERT.
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")

    def create_and_insert(conn):
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()

    def read_back(conn):
        return conn.execute("SELECT v FROM t").fetchone()[0]

    wq.submit("repo-a", create_and_insert).result(timeout=1)
    result = wq.submit("repo-a", read_back).result(timeout=1)

    assert result == 42
    wq.close()


def test_jobs_for_the_same_repo_key_run_in_fifo_submission_order():
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    order = []
    lock = threading.Lock()

    def make_job(n):
        def job(conn):
            with lock:
                order.append(n)
        return job

    futures = [wq.submit("repo-a", make_job(n)) for n in range(20)]
    for future in futures:
        future.result(timeout=1)

    assert order == list(range(20))
    wq.close()


def test_different_repo_keys_get_isolated_connections_and_independent_progress():
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    repo_a_blocked = threading.Event()
    repo_a_may_proceed = threading.Event()

    def slow_job(conn):
        repo_a_blocked.set()
        repo_a_may_proceed.wait(timeout=2)
        return "a-done"

    def fast_job(conn):
        return "b-done"

    future_a = wq.submit("repo-a", slow_job)
    assert repo_a_blocked.wait(timeout=1), "repo-a's job never started"

    # repo-b must make progress without waiting on repo-a's still-blocked job.
    future_b = wq.submit("repo-b", fast_job)
    assert future_b.result(timeout=1) == "b-done"

    repo_a_may_proceed.set()
    assert future_a.result(timeout=1) == "a-done"
    wq.close()


def test_exception_raised_by_fn_is_propagated_through_the_future_not_swallowed():
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")

    def failing_job(conn):
        raise ValueError("boom")

    future = wq.submit("repo-a", failing_job)

    with pytest.raises(ValueError, match="boom"):
        future.result(timeout=1)
    wq.close()


def test_submit_reports_writer_connection_startup_failure_and_a_later_submit_can_retry(tmp_path):
    attempts = []
    working_db_path = str(tmp_path / "working.sqlite")

    def db_path_for(repo_key):
        attempts.append(repo_key)
        if len(attempts) == 1:
            return str(tmp_path / "missing-directory" / "index.sqlite")
        return working_db_path

    wq = WriteQueue(db_path_for=db_path_for)

    failed = wq.submit("repo-a", lambda conn: None)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        failed.result(timeout=1)

    retried = wq.submit("repo-a", lambda conn: "connected")
    assert retried.result(timeout=1) == "connected"
    assert attempts == ["repo-a", "repo-a"]
    wq.close()


def test_slow_writer_startup_for_one_repo_does_not_block_a_different_repos_first_submit():
    # Regression: _worker_for held the single process-wide lock for the
    # entire first-submit path, including worker.start() blocking on
    # sqlite3.connect() -- so one repo's slow/contended disk stalled every
    # other repo's first write, contradicting this module's own docstring
    # guarantee of per-repo independence. repo_a_release's long timeout
    # (5s, well past this test's 1s patience) makes a lock-scoping
    # regression show up as repo-b's submit thread still being alive after
    # a short join, rather than the test merely running slower.
    repo_a_started = threading.Event()
    repo_a_release = threading.Event()

    def db_path_for(repo_key):
        if repo_key == "repo-a":
            repo_a_started.set()
            repo_a_release.wait(timeout=5)
        return ":memory:"

    wq = WriteQueue(db_path_for=db_path_for)
    result_a: dict = {}
    result_b: dict = {}

    def submit_a():
        result_a["future"] = wq.submit("repo-a", lambda conn: "a-done")

    def submit_b():
        result_b["future"] = wq.submit("repo-b", lambda conn: "b-done")

    thread_a = threading.Thread(target=submit_a, daemon=True)
    thread_a.start()
    assert repo_a_started.wait(timeout=1), "repo-a's worker creation never started"

    thread_b = threading.Thread(target=submit_b, daemon=True)
    thread_b.start()
    thread_b.join(timeout=1)
    assert not thread_b.is_alive(), "repo-b's submit is blocked behind repo-a's still-in-flight worker creation"
    assert result_b["future"].result(timeout=1) == "b-done"

    repo_a_release.set()
    thread_a.join(timeout=2)
    assert result_a["future"].result(timeout=1) == "a-done"
    wq.close()


def test_close_drains_every_queued_job_to_completion_before_returning():
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    completed = []
    lock = threading.Lock()

    def make_job(n):
        def job(conn):
            time.sleep(0.01)
            with lock:
                completed.append(n)
        return job

    futures = [wq.submit("repo-a", make_job(n)) for n in range(10)]

    wq.close(timeout=2)

    assert completed == list(range(10))
    for future in futures:
        assert future.done()


def test_close_is_bounded_and_warns_when_a_writer_thread_is_stuck(caplog):
    # SALTMDB ebff13f5's follow-up fix: close() used to be called from
    # runtime.py's on_shutdown() with timeout=None -- an unconditionally
    # unbounded thread.join() -- so a single job stuck forever (a hung
    # subprocess, a wedged syscall) could hang the daemon's shutdown()
    # forever too. Prove close() itself now always returns within budget,
    # and logs which repo's writer it gave up waiting on.
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    release = threading.Event()
    wq.submit("stuck-repo", lambda conn: release.wait())  # never released before close()

    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING):
        wq.close(timeout=0.2)
    elapsed = time.monotonic() - t0

    assert elapsed < 2, f"close() should return within its budget, took {elapsed:.2f}s"
    assert "did not finish draining" in caplog.text
    assert "stuck-repo" in caplog.text

    release.set()  # let the stuck job/writer thread actually finish, no leak past this test


def test_close_does_not_drop_a_job_submitted_by_an_in_flight_completion_callback():
    # SALTMDB bf5a5a98: a Future's done-callbacks run synchronously on
    # whichever thread calls set_result() -- for a write-queue job, that's
    # the repo's own writer thread, still inside _run()'s loop, *before* it
    # loops back to fetch its next item. A caller that infers "the queue is
    # done" from the first job's own effect and immediately calls close()
    # (BootstrapCoordinator's on_job_done -> _mark_ready/
    # _mark_cross_file_migration_done -> write_queue.submit() is exactly
    # this shape) can race close()'s STOP-sentinel enqueue against that
    # callback's own submit() for the very same worker's queue. If STOP
    # lands first, the writer dequeues it and returns before the
    # callback's follow-up job is ever processed -- silently dropped, not
    # errored, not logged.
    #
    # This test doesn't rely on timing luck to reproduce that: it forces
    # the worst-case ordering explicitly (close() call started and given a
    # head start before the completion callback is allowed to submit) so
    # only a real ordering guarantee -- not chance -- can make the
    # assertion below pass.
    #
    # first_job_may_finish also removes a *second*, independent race that
    # isn't this test's target: concurrent.futures.Future.add_done_callback
    # on an ALREADY-done future runs the callback immediately, synchronously,
    # on whichever thread calls add_done_callback -- not the writer thread --
    # per CPython's own Future implementation. For a trivial job (like a
    # bare `lambda conn: None`), the writer thread can finish it before this
    # test's main thread even reaches `.add_done_callback()`, which would
    # make on_first_job_done run on the wrong thread and turn this into a
    # test of a different (much narrower -- real index_file() work is never
    # this fast) race than the one this test documents. Blocking first_job
    # until add_done_callback is confirmed attached pins the callback to the
    # writer thread, as SALTMDB bf5a5a98's reproduction actually observed.
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    callback_may_submit = threading.Event()
    follow_up_ran = threading.Event()
    first_job_may_finish = threading.Event()

    def first_job(conn):
        first_job_may_finish.wait(timeout=5)
        return None

    def on_first_job_done(_future):
        # Runs synchronously on the writer thread (Future contract).
        callback_may_submit.wait(timeout=5)
        wq.submit("repo-a", lambda conn: follow_up_ran.set())

    wq.submit("repo-a", first_job).add_done_callback(on_first_job_done)
    first_job_may_finish.set()

    close_thread = threading.Thread(target=wq.close, daemon=True)
    close_thread.start()
    time.sleep(0.05)  # give close() every chance to enqueue STOP first
    callback_may_submit.set()
    close_thread.join(timeout=5)

    assert not close_thread.is_alive(), "close() never returned"
    assert follow_up_ran.wait(timeout=1), "completion callback's own submit() was silently dropped"


def test_close_racing_immediately_after_submit_still_waits_for_that_jobs_own_follow_up():
    # A first fix attempt for bf5a5a98 held a per-worker lock only *during*
    # a job's execution (fn(conn) through future.set_result()) -- that
    # closes the narrower window the test above targets, but not this
    # wider one: close() can also race in and enqueue _STOP the instant a
    # job is accepted, *before the writer thread has even dequeued it*
    # (verified failing >50% of the time under stress once instrumented,
    # 2026-09-04). The fix that actually holds is _RepoWriter._in_flight,
    # incremented atomically with the enqueue at submit() time rather than
    # at dequeue/execution time -- this test pins that earlier race point
    # directly by starting close() as early as physically possible, right
    # after the very first submit(), with no artificial head start needed.
    #
    # first_job_may_finish blocks the job itself (same technique as the
    # test above, same reason: add_done_callback on an already-done future
    # runs the callback on whichever thread calls add_done_callback, not
    # necessarily the writer thread -- irrelevant to *this* test's target
    # race, so it's pinned out rather than left to chance).
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    callback_may_submit = threading.Event()
    follow_up_ran = threading.Event()
    first_job_may_finish = threading.Event()

    def first_job(conn):
        first_job_may_finish.wait(timeout=5)
        return None

    def on_first_job_done(_future):
        callback_may_submit.wait(timeout=5)
        wq.submit("repo-a", lambda conn: follow_up_ran.set())

    first_future = wq.submit("repo-a", first_job)
    close_thread = threading.Thread(target=wq.close, daemon=True)
    close_thread.start()  # started immediately -- no window for the writer to even begin the first job
    first_future.add_done_callback(on_first_job_done)
    first_job_may_finish.set()
    callback_may_submit.set()
    close_thread.join(timeout=5)

    assert not close_thread.is_alive(), "close() never returned"
    assert follow_up_ran.wait(timeout=1), "completion callback's own submit() was silently dropped"


def test_submit_after_close_fails_fast_instead_of_enqueueing_into_a_dead_worker():
    # codex review, 2026-09-02: _workers' entries are never removed by
    # close(), so a submit() landing after a repo's writer thread has
    # already consumed its _STOP sentinel and exited would otherwise
    # silently reuse that dead worker's queue -- the job would sit
    # forever, unprocessed, rather than erroring in any visible way.
    wq = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    wq.submit("repo-a", lambda conn: None).result(timeout=1)  # create + use the worker once
    wq.close(timeout=2)

    future = wq.submit("repo-a", lambda conn: None)

    with pytest.raises(RuntimeError, match="closed"):
        future.result(timeout=1)
