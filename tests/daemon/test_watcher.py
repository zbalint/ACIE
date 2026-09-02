import logging
import os
import sqlite3
import threading
import time

from acie.daemon import ignore
from acie.daemon.watcher import RepoWatcher, WatcherRegistry, _DebouncedEventHandler, make_reindex_job
from acie.daemon.write_queue import WriteQueue
from acie.storage.file_state_store import FileStateStore
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.symbol_store import SymbolStore

_SHORT_DEBOUNCE = 0.05
_WAIT_PAST_DEBOUNCE = 0.3


def _write(repo_root, rel_path, content):
    abs_path = os.path.join(repo_root, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)


# -- _DebouncedEventHandler: coalescing/ignore/extension-filter behavior --


def test_rapid_touches_of_distinct_paths_coalesce_into_one_flush_call(tmp_path):
    repo_root = str(tmp_path)
    flushes = []
    handler = _DebouncedEventHandler(repo_root, flushes.append, debounce_seconds=_SHORT_DEBOUNCE)

    handler._touch(os.path.join(repo_root, "a.py"))
    handler._touch(os.path.join(repo_root, "b.py"))
    handler._touch(os.path.join(repo_root, "a.py"))  # duplicate touch, same debounce window
    time.sleep(_WAIT_PAST_DEBOUNCE)

    assert flushes == [{"a.py", "b.py"}]


def test_touches_further_apart_than_the_debounce_window_flush_separately(tmp_path):
    repo_root = str(tmp_path)
    flushes = []
    handler = _DebouncedEventHandler(repo_root, flushes.append, debounce_seconds=_SHORT_DEBOUNCE)

    handler._touch(os.path.join(repo_root, "a.py"))
    time.sleep(_WAIT_PAST_DEBOUNCE)
    handler._touch(os.path.join(repo_root, "b.py"))
    time.sleep(_WAIT_PAST_DEBOUNCE)

    assert flushes == [{"a.py"}, {"b.py"}]


def test_non_python_files_are_never_flushed(tmp_path):
    repo_root = str(tmp_path)
    flushes = []
    handler = _DebouncedEventHandler(repo_root, flushes.append, debounce_seconds=_SHORT_DEBOUNCE)

    handler._touch(os.path.join(repo_root, "README.md"))
    time.sleep(_WAIT_PAST_DEBOUNCE)

    assert flushes == []


def test_ignored_paths_are_never_flushed(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, ".gitignore", "generated.py\n")
    flushes = []
    handler = _DebouncedEventHandler(repo_root, flushes.append, debounce_seconds=_SHORT_DEBOUNCE)

    handler._touch(os.path.join(repo_root, "generated.py"))
    time.sleep(_WAIT_PAST_DEBOUNCE)

    assert flushes == []


def test_touching_gitignore_itself_invalidates_the_cached_matcher(tmp_path):
    repo_root = str(tmp_path)
    matcher_before = ignore.get_ignore_matcher(repo_root)
    assert matcher_before.matches("generated.py") is False
    _write(repo_root, ".gitignore", "generated.py\n")
    handler = _DebouncedEventHandler(repo_root, lambda paths: None, debounce_seconds=_SHORT_DEBOUNCE)

    handler._touch(os.path.join(repo_root, ".gitignore"))

    matcher_after = ignore.get_ignore_matcher(repo_root)
    assert matcher_after is not matcher_before
    assert matcher_after.matches("generated.py") is True


# -- make_reindex_job: the per-file reindex/staleness decision --


def _fresh_conn():
    return sqlite3.connect(":memory:")


def test_watch_job_for_a_new_file_indexes_it_and_records_its_state(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()

    job = make_reindex_job(repo_root, "mod.py")
    job(conn)

    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("mod.py")] == ["", "foo"]
    state = FileStateStore(conn=conn).get("mod.py")
    assert state is not None
    assert state.content_hash != ""


def test_watch_job_is_a_no_op_when_mtime_is_unchanged(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    job = make_reindex_job(repo_root, "mod.py")
    job(conn)
    generation_after_first = IndexMetaStore(conn=conn).current_generation()

    job(conn)  # same file, same on-disk mtime -- must not reindex again

    assert IndexMetaStore(conn=conn).current_generation() == generation_after_first


def test_watch_job_updates_recorded_mtime_but_skips_reindex_when_content_is_unchanged(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    job = make_reindex_job(repo_root, "mod.py")
    job(conn)
    generation_after_first = IndexMetaStore(conn=conn).current_generation()
    state_after_first = FileStateStore(conn=conn).get("mod.py")

    # Simulate a "touch" -- rewrite identical bytes, which changes mtime_ns
    # but not content -- by forcing a distinct mtime_ns explicitly (a same-
    # second rewrite can otherwise land on an identical mtime on some
    # filesystems, making the test flaky).
    abs_path = os.path.join(repo_root, "mod.py")
    new_mtime_ns = state_after_first.mtime_ns + 1_000_000_000
    os.utime(abs_path, ns=(new_mtime_ns, new_mtime_ns))

    job(conn)

    assert IndexMetaStore(conn=conn).current_generation() == generation_after_first
    assert FileStateStore(conn=conn).get("mod.py").mtime_ns == new_mtime_ns


def test_watch_job_reindexes_when_content_actually_changes(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    job = make_reindex_job(repo_root, "mod.py")
    job(conn)

    abs_path = os.path.join(repo_root, "mod.py")
    state = FileStateStore(conn=conn).get("mod.py")
    _write(repo_root, "mod.py", "def bar():\n    pass\n")
    os.utime(abs_path, ns=(state.mtime_ns + 1_000_000_000,) * 2)

    job(conn)

    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("mod.py")] == ["", "bar"]


def test_watch_job_for_a_deleted_file_tombstones_its_prior_symbols(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    job = make_reindex_job(repo_root, "mod.py")
    job(conn)
    os.remove(os.path.join(repo_root, "mod.py"))

    job(conn)

    assert SymbolStore(conn=conn).list_by_path("mod.py") == []
    assert FileStateStore(conn=conn).get("mod.py") is None


def test_watch_job_for_a_deleted_file_that_was_only_ever_bootstrap_indexed_still_tombstones_it(tmp_path):
    # Regression (codex review, 2026-09-02): bootstrap.py's make_job calls
    # index_file directly and never touches FileStateStore -- gating the
    # delete branch on "does file_state_store have a row for this path"
    # meant a bootstrap-only-indexed file's delete was silently ignored.
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    # Simulate bootstrap: index directly, bypassing make_reindex_job (and
    # therefore FileStateStore) entirely, exactly like bootstrap.py does.
    from datetime import datetime, timezone

    from acie.indexer import index_file
    from acie.storage.relation_store import RelationStore

    index_file(
        path="mod.py", source_text="def foo():\n    pass\n",
        observed_at=datetime.now(timezone.utc).isoformat(),
        symbol_store=SymbolStore(conn=conn), relation_store=RelationStore(conn=conn),
        index_meta_store=IndexMetaStore(conn=conn),
    )
    assert FileStateStore(conn=conn).get("mod.py") is None  # bootstrap never sets this
    os.remove(os.path.join(repo_root, "mod.py"))

    make_reindex_job(repo_root, "mod.py")(conn)

    assert SymbolStore(conn=conn).list_by_path("mod.py") == []


def test_watch_job_for_a_delete_never_indexed_before_is_a_no_op(tmp_path):
    repo_root = str(tmp_path)
    conn = _fresh_conn()
    generation_before = IndexMetaStore(conn=conn).current_generation()

    job = make_reindex_job(repo_root, "never_existed.py")
    job(conn)  # must not raise

    assert IndexMetaStore(conn=conn).current_generation() == generation_before


def test_rename_decomposes_into_tombstoning_the_old_path_and_indexing_the_new_one(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "old_name.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    make_reindex_job(repo_root, "old_name.py")(conn)

    os.rename(os.path.join(repo_root, "old_name.py"), os.path.join(repo_root, "new_name.py"))

    make_reindex_job(repo_root, "old_name.py")(conn)
    make_reindex_job(repo_root, "new_name.py")(conn)

    assert SymbolStore(conn=conn).list_by_path("old_name.py") == []
    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("new_name.py")] == ["", "foo"]


# -- RepoWatcher / WatcherRegistry: real Observer, real write queue --


def test_repo_watcher_end_to_end_indexes_a_newly_created_file(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    db_path = tmp_path / "index.sqlite"
    write_queue = WriteQueue(db_path_for=lambda repo_id: str(db_path))
    watcher = RepoWatcher(str(repo_root), "repo-id", write_queue, debounce_seconds=_SHORT_DEBOUNCE)
    try:
        _write(str(repo_root), "mod.py", "def foo():\n    pass\n")
        deadline = time.monotonic() + 2
        symbols = []
        while time.monotonic() < deadline:
            conn = sqlite3.connect(str(db_path))
            symbols = SymbolStore(conn=conn).list_by_path("mod.py")
            conn.close()
            if symbols:
                break
            time.sleep(0.05)
        assert [s.qualname for s in symbols] == ["", "foo"]
    finally:
        watcher.stop(timeout=2)
        write_queue.close(timeout=2)


class _FakeInstantObserver:
    """A trivial Observer double whose stop()/join() both return
    immediately -- for tests that need RepoWatcher.stop()'s surrounding
    behavior (e.g. debounce-timer flushing) without a real watchdog thread
    or the fake-hang behavior of _FakeHangingObserver below.
    """

    def stop(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass


class _RecordingWriteQueue:
    """A minimal WriteQueue double that just records which repo_key each
    submit() call was for -- used where a test cares only about whether/
    what RepoWatcher submitted, not real write-queue execution.
    """

    def __init__(self) -> None:
        self.submitted_repo_keys: list[str] = []

    def submit(self, repo_key, fn):  # noqa: ANN001 -- test double, matches WriteQueue.submit's shape loosely.
        self.submitted_repo_keys.append(repo_key)


def test_repo_watcher_stop_flushes_a_pending_debounce_timer_before_returning():
    # codex review, 2026-09-02: a debounce Timer already scheduled from an
    # edit observed just before shutdown is a separate object from the
    # Observer and untouched by Observer.stop()/.join() -- without
    # RepoWatcher.stop() explicitly flushing it, it would fire later on
    # its own schedule, possibly after write_queue.close() has already
    # stopped that repo's writer thread, silently dropping the edit.
    write_queue = _RecordingWriteQueue()
    watcher = RepoWatcher.__new__(RepoWatcher)
    watcher._repo_root = "/fake/repo"
    watcher._repo_id = "repo-id"
    watcher._write_queue = write_queue
    watcher._handler = _DebouncedEventHandler(
        "/fake/repo", watcher._on_paths_changed, debounce_seconds=10.0
    )
    watcher._observer = _FakeInstantObserver()

    watcher._handler._touch("/fake/repo/mod.py")  # schedules a 10s timer -- won't fire on its own during this test
    assert watcher._handler._timer is not None

    watcher.stop(timeout=2)

    assert watcher._handler._timer is None  # cancelled, not left to fire independently
    assert watcher._handler._pending == set()  # flushed, not silently dropped
    assert write_queue.submitted_repo_keys == ["repo-id"]  # actually submitted before stop() returned


class _FakeHangingObserver:
    """Simulates watchdog's own Observer.stop() blocking forever --
    BaseObserver.unschedule_all()'s _clear_emitters() joins each emitter
    thread with no timeout of its own -- without any real OS-level watch
    or watchdog thread (codex review, 2026-09-02: a real Observer whose
    real .stop() is monkeypatched away never actually stops -- its real
    background thread's run() loop keeps checking a _stopped_event that
    only the real .stop() ever sets, so RepoWatcher.stop()'s helper thread
    blocks on Observer.join() forever and leaks a real inotify watch for
    the rest of the test process). This fake has no thread of its own at
    all: .stop() blocks on an Event only the test itself can release.
    """

    def __init__(self) -> None:
        self._released = threading.Event()

    def stop(self) -> None:
        self._released.wait()

    def join(self, timeout: float | None = None) -> None:
        pass


def test_repo_watcher_stop_is_bounded_even_if_observer_teardown_hangs(caplog):
    # SALTMDB ebff13f5 (live incident: an orphaned daemon process,
    # permanently un-killable by SIGTERM) + its follow-up fix: RepoWatcher
    # .stop() must bound the *whole* stop()+join() sequence from the
    # outside, not just hand a timeout to Observer.join(). Bypass
    # RepoWatcher.__init__ (which always constructs and starts a real
    # Observer) so this test never touches a real watchdog thread at all.
    watcher = RepoWatcher.__new__(RepoWatcher)
    watcher._repo_root = "/fake/repo"
    watcher._handler = _DebouncedEventHandler("/fake/repo", lambda paths: None)
    watcher._observer = _FakeHangingObserver()

    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING):
        stopped = watcher.stop(timeout=0.2)
    elapsed = time.monotonic() - t0

    assert stopped is False
    assert elapsed < 2, f"stop() should return within its budget, took {elapsed:.2f}s"
    assert "did not finish tearing down" in caplog.text

    watcher._observer._released.set()  # let the fake's blocked helper thread finish, no leak


def test_watcher_registry_creates_exactly_one_watcher_per_repo_root(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    write_queue = WriteQueue(db_path_for=lambda repo_id: ":memory:")
    registry = WatcherRegistry(write_queue)
    try:
        registry.register("repo-id", str(repo_root))
        first_watcher = registry._watchers[str(repo_root)]
        registry.register("repo-id", str(repo_root))  # idempotent second call

        assert registry._watchers[str(repo_root)] is first_watcher
    finally:
        registry.close(timeout=2)


def test_watcher_registry_keys_on_repo_root_not_repo_id(tmp_path):
    # decision 10's fix (SALTMDB f4bdfc9d, grilled 2026-09-02): a second
    # repo_path spelling of the identical worktree directory (e.g. a
    # symlink vs its realpath'd twin) resolves to the same repo_root even
    # when it resolves to a *different* repo_id would be a bug elsewhere
    # -- but this registry specifically must dedup on repo_root, since
    # that's the actual directory an Observer watches, not on whatever
    # repo_id string happens to be passed alongside it.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    write_queue = WriteQueue(db_path_for=lambda repo_id: ":memory:")
    registry = WatcherRegistry(write_queue)
    try:
        registry.register("repo-id-one", str(repo_root))
        first_watcher = registry._watchers[str(repo_root)]
        registry.register("repo-id-two", str(repo_root))  # different repo_id, same directory

        assert len(registry._watchers) == 1
        assert registry._watchers[str(repo_root)] is first_watcher
    finally:
        registry.close(timeout=2)
