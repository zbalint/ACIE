import os
import sqlite3
import time

from acie.daemon import ignore
from acie.daemon.watcher import RepoWatcher, WatcherRegistry, _DebouncedEventHandler, _make_watch_job
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


# -- _make_watch_job: the per-file reindex/staleness decision --


def _fresh_conn():
    return sqlite3.connect(":memory:")


def test_watch_job_for_a_new_file_indexes_it_and_records_its_state(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()

    job = _make_watch_job(repo_root, "mod.py")
    job(conn)

    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("mod.py")] == ["", "foo"]
    state = FileStateStore(conn=conn).get("mod.py")
    assert state is not None
    assert state.content_hash != ""


def test_watch_job_is_a_no_op_when_mtime_is_unchanged(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    job = _make_watch_job(repo_root, "mod.py")
    job(conn)
    generation_after_first = IndexMetaStore(conn=conn).current_generation()

    job(conn)  # same file, same on-disk mtime -- must not reindex again

    assert IndexMetaStore(conn=conn).current_generation() == generation_after_first


def test_watch_job_updates_recorded_mtime_but_skips_reindex_when_content_is_unchanged(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "mod.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    job = _make_watch_job(repo_root, "mod.py")
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
    job = _make_watch_job(repo_root, "mod.py")
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
    job = _make_watch_job(repo_root, "mod.py")
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
    # Simulate bootstrap: index directly, bypassing _make_watch_job (and
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

    _make_watch_job(repo_root, "mod.py")(conn)

    assert SymbolStore(conn=conn).list_by_path("mod.py") == []


def test_watch_job_for_a_delete_never_indexed_before_is_a_no_op(tmp_path):
    repo_root = str(tmp_path)
    conn = _fresh_conn()
    generation_before = IndexMetaStore(conn=conn).current_generation()

    job = _make_watch_job(repo_root, "never_existed.py")
    job(conn)  # must not raise

    assert IndexMetaStore(conn=conn).current_generation() == generation_before


def test_rename_decomposes_into_tombstoning_the_old_path_and_indexing_the_new_one(tmp_path):
    repo_root = str(tmp_path)
    _write(repo_root, "old_name.py", "def foo():\n    pass\n")
    conn = _fresh_conn()
    _make_watch_job(repo_root, "old_name.py")(conn)

    os.rename(os.path.join(repo_root, "old_name.py"), os.path.join(repo_root, "new_name.py"))

    _make_watch_job(repo_root, "old_name.py")(conn)
    _make_watch_job(repo_root, "new_name.py")(conn)

    assert SymbolStore(conn=conn).list_by_path("old_name.py") == []
    assert [s.qualname for s in SymbolStore(conn=conn).list_by_path("new_name.py")] == ["", "foo"]


# -- RepoWatcher / WatcherRegistry: real Observer, real write queue --


def test_repo_watcher_end_to_end_indexes_a_newly_created_file(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    db_path = tmp_path / "index.sqlite"
    write_queue = WriteQueue(db_path_for=lambda repo_key: str(db_path))
    watcher = RepoWatcher(str(repo_root), "repo-key", write_queue, debounce_seconds=_SHORT_DEBOUNCE)
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


def test_watcher_registry_creates_exactly_one_watcher_per_repo_key(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    write_queue = WriteQueue(db_path_for=lambda repo_key: ":memory:")
    registry = WatcherRegistry(write_queue)
    try:
        registry.register("repo-key", str(repo_root))
        first_watcher = registry._watchers["repo-key"]
        registry.register("repo-key", str(repo_root))  # idempotent second call

        assert registry._watchers["repo-key"] is first_watcher
    finally:
        registry.close(timeout=2)
