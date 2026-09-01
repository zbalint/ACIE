import os
import sqlite3
import threading
import time
from concurrent.futures import Future

from acie.daemon.bootstrap import BootstrapCoordinator
from acie.daemon.write_queue import WriteQueue
from acie.storage.symbol_store import SymbolStore

_OBSERVED_AT = "2026-09-01T00:00:00Z"


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _make_coordinator(tmp_path, files_by_repo, db_paths=None):
    db_paths = db_paths if db_paths is not None else {}

    def db_path_for(repo_key):
        return db_paths.setdefault(repo_key, str(tmp_path / f"{repo_key}.sqlite"))

    write_queue = WriteQueue(db_path_for=db_path_for)
    coordinator = BootstrapCoordinator(
        write_queue=write_queue,
        db_path_for=db_path_for,
        walk_repo=lambda repo_key: files_by_repo.get(repo_key, []),
    )
    return coordinator, write_queue, db_path_for


def test_repo_ready_is_false_for_a_repo_never_registered_with_no_index_sqlite_yet(tmp_path):
    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={})

    assert coordinator.repo_ready("repo-a") is False
    write_queue.close()


def test_repo_ready_is_true_immediately_when_index_sqlite_already_exists_on_disk(tmp_path):
    walk_calls = []
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo={})
    # Simulate a repo already fully indexed by a prior daemon run.
    sqlite3.connect(db_path_for("repo-a")).close()
    coordinator._walk_repo = lambda repo_key: walk_calls.append(repo_key) or []

    assert coordinator.repo_ready("repo-a") is True
    assert walk_calls == []  # pre-existing index.sqlite is trusted, never re-walked
    write_queue.close()


def test_repo_ready_stays_false_during_an_in_flight_bootstrap_even_once_its_sqlite_file_exists(tmp_path):
    # Regression: WriteQueue's writer thread creates repo-a.sqlite the
    # instant it opens its connection (sqlite3.connect() on a fresh path
    # creates the file), long before any write in it is committed. A naive
    # repo_ready() that trusts bare file existence would flip true on a
    # bootstrap still in flight and race dispatch into reading partial data.
    db_path = str(tmp_path / "repo-a.sqlite")
    started = threading.Event()
    release = threading.Event()

    class SlowWriteQueue:
        def submit(self, repo_key, fn):
            future = Future()

            def run():
                conn = sqlite3.connect(db_path)  # creates the file on disk now
                started.set()
                release.wait(timeout=2)
                try:
                    result = fn(conn)
                except BaseException as exc:  # noqa: BLE001 -- propagated via the Future, matching WriteQueue's real contract.
                    future.set_exception(exc)
                else:
                    future.set_result(result)
                conn.close()

            threading.Thread(target=run, daemon=True).start()
            return future

    coordinator = BootstrapCoordinator(
        write_queue=SlowWriteQueue(),
        db_path_for=lambda repo_key: db_path,
        walk_repo=lambda repo_key: [("pkg/mod.py", "def foo():\n    pass\n")],
    )

    coordinator.register("repo-a")

    assert started.wait(timeout=2), "writer thread never started"
    assert os.path.exists(db_path), "test setup assumption broken: file should already exist"
    assert coordinator.repo_ready("repo-a") is False

    release.set()
    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))


def test_register_walks_and_indexes_every_file_then_flips_repo_ready_to_true(tmp_path):
    files = {
        "repo-a": [
            ("pkg/mod.py", "def foo():\n    pass\n"),
            ("pkg/other.py", "def bar():\n    pass\n"),
        ]
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)

    assert coordinator.repo_ready("repo-a") is False
    coordinator.register("repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a")), "repo never became ready"

    store = SymbolStore(db_path_for("repo-a"))
    assert {s.qualname for s in store.search(qualname_substring="")} == {"", "foo", "bar"}
    write_queue.close()


def test_register_on_a_repo_with_no_files_becomes_ready_without_any_write_queue_submission(tmp_path):
    submitted = []
    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={"repo-a": []})
    real_submit = write_queue.submit
    write_queue.submit = lambda repo_key, fn: submitted.append(repo_key) or real_submit(repo_key, fn)

    coordinator.register("repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))
    assert submitted == []
    write_queue.close()


def test_register_is_idempotent_and_never_double_walks_a_repo_already_in_progress(tmp_path):
    walk_calls = []

    def walk_repo(repo_key):
        walk_calls.append(repo_key)
        return [("pkg/mod.py", "def foo():\n    pass\n")]

    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={})
    coordinator._walk_repo = walk_repo

    coordinator.register("repo-a")
    coordinator.register("repo-a")
    coordinator.register("repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))
    assert walk_calls == ["repo-a"]
    write_queue.close()


def test_register_is_a_no_op_once_the_repo_is_already_ready(tmp_path):
    walk_calls = []
    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={"repo-a": []})
    coordinator.register("repo-a")
    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))

    coordinator._walk_repo = lambda repo_key: walk_calls.append(repo_key) or []
    coordinator.register("repo-a")

    assert walk_calls == []
    write_queue.close()


def test_concurrent_registers_from_multiple_threads_only_walk_once(tmp_path):
    walk_calls = []
    lock = threading.Lock()

    def walk_repo(repo_key):
        with lock:
            walk_calls.append(repo_key)
        return [(f"pkg/mod{i}.py", f"def f{i}():\n    pass\n") for i in range(5)]

    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={})
    coordinator._walk_repo = walk_repo

    threads = [threading.Thread(target=coordinator.register, args=("repo-a",)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))
    assert walk_calls == ["repo-a"]
    write_queue.close()


def test_register_still_becomes_ready_when_every_files_write_queue_submission_fails(tmp_path):
    # Regression: WriteQueue.submit() returns an already-failed Future
    # without ever invoking the job closure when writer startup itself
    # fails (e.g. a sqlite3.connect() error). Previously the "counts
    # toward done" bookkeeping lived only inside the job closure's own
    # finally block, so a repo whose writer never even starts stayed
    # wedged in INDEX_NOT_READY forever.
    class AlwaysFailingWriteQueue:
        def submit(self, repo_key, fn):
            future = Future()
            future.set_exception(RuntimeError("writer startup failed"))
            return future

    coordinator = BootstrapCoordinator(
        write_queue=AlwaysFailingWriteQueue(),
        db_path_for=lambda repo_key: str(tmp_path / f"{repo_key}.sqlite"),
        walk_repo=lambda repo_key: [
            ("pkg/mod.py", "def foo():\n    pass\n"),
            ("pkg/other.py", "def bar():\n    pass\n"),
        ],
    )

    coordinator.register("repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a")), "repo stayed wedged in INDEX_NOT_READY"


def test_different_repos_bootstrap_independently(tmp_path):
    files = {
        "repo-a": [("pkg/mod.py", "def foo():\n    pass\n")],
        "repo-b": [("pkg/mod.py", "def baz():\n    pass\n")],
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)

    coordinator.register("repo-a")
    coordinator.register("repo-b")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a") and coordinator.repo_ready("repo-b"))

    store_a = SymbolStore(db_path_for("repo-a"))
    store_b = SymbolStore(db_path_for("repo-b"))
    assert {s.qualname for s in store_a.search(qualname_substring="")} == {"", "foo"}
    assert {s.qualname for s in store_b.search(qualname_substring="")} == {"", "baz"}
    write_queue.close()
