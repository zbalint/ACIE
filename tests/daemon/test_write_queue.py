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
