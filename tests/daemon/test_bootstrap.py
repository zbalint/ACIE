import os
import sqlite3
import threading
import time
from concurrent.futures import Future

from acie.daemon.bootstrap import BootstrapCoordinator
from acie.daemon.write_queue import WriteQueue
from acie.indexer import index_file
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore

_OBSERVED_AT = "2026-09-01T00:00:00Z"

# decision 10's fix (SALTMDB f4bdfc9d, grilled 2026-09-02): register()/
# repo_ready() are keyed on repo_id, walk_repo on repo_root -- these two
# tests never need to differ (no real git repo here, no worktree scenario),
# so every fake below just reuses the same opaque string for both.


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _make_coordinator(tmp_path, files_by_repo, db_paths=None):
    db_paths = db_paths if db_paths is not None else {}

    def db_path_for(repo_id):
        return db_paths.setdefault(repo_id, str(tmp_path / f"{repo_id}.sqlite"))

    write_queue = WriteQueue(db_path_for=db_path_for)
    coordinator = BootstrapCoordinator(
        write_queue=write_queue,
        db_path_for=db_path_for,
        walk_repo=lambda repo_root: files_by_repo.get(repo_root, []),
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
    coordinator._walk_repo = lambda repo_root: walk_calls.append(repo_root) or []

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
        def submit(self, repo_id, fn):
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
        db_path_for=lambda repo_id: db_path,
        walk_repo=lambda repo_root: [("pkg/mod.py", "def foo():\n    pass\n")],
    )

    coordinator.register("repo-a", "repo-a")

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
    coordinator.register("repo-a", "repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a")), "repo never became ready"

    store = SymbolStore(db_path_for("repo-a"))
    assert {s.qualname for s in store.search(qualname_substring="")} == {"", "foo", "bar"}
    write_queue.close()


def test_register_on_a_repo_with_no_files_becomes_ready_without_any_write_queue_submission(tmp_path):
    submitted = []
    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={"repo-a": []})
    real_submit = write_queue.submit
    write_queue.submit = lambda repo_id, fn: submitted.append(repo_id) or real_submit(repo_id, fn)

    coordinator.register("repo-a", "repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))
    assert submitted == []
    write_queue.close()


def test_register_is_idempotent_and_never_double_walks_a_repo_already_in_progress(tmp_path):
    walk_calls = []

    def walk_repo(repo_root):
        walk_calls.append(repo_root)
        return [("pkg/mod.py", "def foo():\n    pass\n")]

    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={})
    coordinator._walk_repo = walk_repo

    coordinator.register("repo-a", "repo-a")
    coordinator.register("repo-a", "repo-a")
    coordinator.register("repo-a", "repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))
    assert walk_calls == ["repo-a"]
    write_queue.close()


def test_register_is_a_no_op_once_the_repo_is_already_ready(tmp_path):
    walk_calls = []
    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={"repo-a": []})
    coordinator.register("repo-a", "repo-a")
    assert _wait_until(lambda: coordinator.repo_ready("repo-a"))

    coordinator._walk_repo = lambda repo_root: walk_calls.append(repo_root) or []
    coordinator.register("repo-a", "repo-a")

    assert walk_calls == []
    write_queue.close()


def test_concurrent_registers_from_multiple_threads_only_walk_once(tmp_path):
    walk_calls = []
    lock = threading.Lock()

    def walk_repo(repo_root):
        with lock:
            walk_calls.append(repo_root)
        return [(f"pkg/mod{i}.py", f"def f{i}():\n    pass\n") for i in range(5)]

    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={})
    coordinator._walk_repo = walk_repo

    threads = [
        threading.Thread(target=coordinator.register, args=("repo-a", "repo-a")) for _ in range(10)
    ]
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
        def submit(self, repo_id, fn):
            future = Future()
            future.set_exception(RuntimeError("writer startup failed"))
            return future

    coordinator = BootstrapCoordinator(
        write_queue=AlwaysFailingWriteQueue(),
        db_path_for=lambda repo_id: str(tmp_path / f"{repo_id}.sqlite"),
        walk_repo=lambda repo_root: [
            ("pkg/mod.py", "def foo():\n    pass\n"),
            ("pkg/other.py", "def bar():\n    pass\n"),
        ],
    )

    coordinator.register("repo-a", "repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a")), "repo stayed wedged in INDEX_NOT_READY"


def test_different_repos_bootstrap_independently(tmp_path):
    files = {
        "repo-a": [("pkg/mod.py", "def foo():\n    pass\n")],
        "repo-b": [("pkg/mod.py", "def baz():\n    pass\n")],
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)

    coordinator.register("repo-a", "repo-a")
    coordinator.register("repo-b", "repo-b")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a") and coordinator.repo_ready("repo-b"))

    store_a = SymbolStore(db_path_for("repo-a"))
    store_b = SymbolStore(db_path_for("repo-b"))
    assert {s.qualname for s in store_a.search(qualname_substring="")} == {"", "foo"}
    assert {s.qualname for s in store_b.search(qualname_substring="")} == {"", "baz"}
    write_queue.close()


def test_register_resolves_a_cross_file_imported_call_regardless_of_walk_order(tmp_path):
    """The bootstrap-plan's central claim: register()'s second pass closes
    the ordering gap indexer.py's own tests pin as a known limitation of a
    single index_file call -- the caller (pkg/mod.py) is walked *before*
    its callee (pkg/other.py) here, and the cross-file `calls` edge must
    still exist once repo_ready() flips true, with no second register()
    call from the test.
    """
    files = {
        "repo-a": [
            ("pkg/mod.py", "from pkg.other import helper\n\n\nhelper()\n"),
            ("pkg/other.py", "def helper():\n    pass\n"),
        ]
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)

    coordinator.register("repo-a", "repo-a")

    assert _wait_until(lambda: coordinator.repo_ready("repo-a")), "repo never became ready"

    relation_store = RelationStore(db_path_for("repo-a"))
    calls = relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(calls) == 1
    assert calls[0].target == "pkg/other.py:helper#function"
    write_queue.close()


def _index_files_once_directly(db_path, files):
    """Simulates a pre-upgrade install: each file indexed exactly once,
    via a single index_file call per file (no bootstrap two-pass), matching
    the shape of an already-existing index.sqlite from before cross-file
    call resolution existed.
    """
    conn = sqlite3.connect(db_path)
    for path, source_text in files:
        index_file(
            path=path, source_text=source_text, observed_at=_OBSERVED_AT,
            symbol_store=SymbolStore(conn=conn), relation_store=RelationStore(conn=conn),
            index_meta_store=IndexMetaStore(conn=conn),
        )
    conn.close()


def test_register_retroactively_resolves_cross_file_calls_for_an_already_indexed_repo(tmp_path):
    """Codex review finding (P1): repo_ready() trusts a pre-existing
    index.sqlite immediately (this class's own docstring), so register()'s
    normal walk-then-two-pass path is never reached for an already-indexed
    repo -- an upgraded installation would otherwise never gain cross-file
    call resolution unless individual files happened to be edited later. A
    dedicated, flag-gated catch-up pass (IndexMetaStore.cross_file_pass_done,
    same lazy-migration idiom as _migrate_add_head_sha_column_if_missing)
    must retroactively resolve it the first time such a repo is registered
    again.
    """
    files = {
        "repo-a": [
            ("pkg/mod.py", "from pkg.other import helper\n\n\nhelper()\n"),
            ("pkg/other.py", "def helper():\n    pass\n"),
        ]
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)
    _index_files_once_directly(db_path_for("repo-a"), files["repo-a"])
    assert RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"calls"}) == []
    coordinator._walk_repo = lambda repo_root: files.get(repo_root, [])

    coordinator.register("repo-a", "repo-a")

    assert _wait_until(
        lambda: RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"calls"}) != []
    ), "cross-file call was never retroactively resolved"
    calls = RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(calls) == 1
    assert calls[0].target == "pkg/other.py:helper#function"
    write_queue.close()


def test_register_does_not_repeat_the_cross_file_migration_once_done(tmp_path):
    files = {
        "repo-a": [
            ("pkg/mod.py", "from pkg.other import helper\n\n\nhelper()\n"),
            ("pkg/other.py", "def helper():\n    pass\n"),
        ]
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)
    _index_files_once_directly(db_path_for("repo-a"), files["repo-a"])
    walk_calls = []
    coordinator._walk_repo = lambda repo_root: walk_calls.append(repo_root) or files.get(repo_root, [])

    coordinator.register("repo-a", "repo-a")
    assert _wait_until(
        lambda: RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"calls"}) != []
    )
    assert walk_calls == ["repo-a"]

    coordinator.register("repo-a", "repo-a")
    coordinator.register("repo-a", "repo-a")

    assert walk_calls == ["repo-a"], "migration re-walked the repo after already completing once"
    write_queue.close()


def test_register_retroactively_resolves_cross_file_inherits_for_a_repo_stuck_at_the_old_calls_only_migration_version(
    tmp_path,
):
    """Codex review finding (P1), slice A2: a repo that already completed
    the pre-A2 calls-only catch-up pass (persisted at cross-file-pass
    version 1) must NOT be skipped by BootstrapCoordinator's flag-gated
    catch-up check just because that old flag reads "done" -- only a repo
    at or above CURRENT_CROSS_FILE_PASS_VERSION (2, calls+inherits) may be
    skipped. Otherwise an install upgraded straight from the calls-only
    migration would never gain cross-file inherits resolution for its
    pre-existing files at all, exactly like the original calls-only gap
    this same catch-up mechanism was built to close.
    """
    files = {
        "repo-a": [
            ("pkg/mod.py", "from pkg.other import Base\n\n\nclass Foo(Base):\n    pass\n"),
            ("pkg/other.py", "class Base:\n    pass\n"),
        ]
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)
    _index_files_once_directly(db_path_for("repo-a"), files["repo-a"])
    # Simulate an install that already fully completed the pre-A2
    # calls-only migration (persisted version 1) -- not a fresh/never-
    # migrated repo (version 0), which the older test above already covers.
    conn = sqlite3.connect(db_path_for("repo-a"))
    conn.execute("UPDATE index_meta SET cross_file_pass_version = 1 WHERE id = 0")
    conn.commit()
    conn.close()
    assert RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"inherits"}) == []
    coordinator._walk_repo = lambda repo_root: files.get(repo_root, [])

    coordinator.register("repo-a", "repo-a")

    assert _wait_until(
        lambda: RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"inherits"}) != []
    ), "cross-file inherits was never retroactively resolved for a repo stuck at the old calls-only version"
    inherits = RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"inherits"})
    assert len(inherits) == 1
    assert inherits[0].target == "pkg/other.py:Base#class"
    write_queue.close()


def test_register_retroactively_resolves_cross_file_overrides_for_a_repo_stuck_at_the_old_calls_and_inherits_migration_version(
    tmp_path,
):
    """Slice A3: mirrors test_register_retroactively_resolves_cross_file_inherits_for_a_repo_stuck_at_the_old_calls_only_migration_version
    one version up -- a repo that already completed the pre-A3 calls+inherits
    catch-up pass (persisted at cross-file-pass version 2) must NOT be
    skipped just because that old version already reads "done"; only a repo
    at or above CURRENT_CROSS_FILE_PASS_VERSION (3, calls+inherits+overrides)
    may be skipped.
    """
    files = {
        "repo-a": [
            ("pkg/mod.py", "from pkg.other import Base\n\n\nclass Foo(Base):\n    def bar(self):\n        pass\n"),
            ("pkg/other.py", "class Base:\n    def bar(self):\n        pass\n"),
        ]
    }
    coordinator, write_queue, db_path_for = _make_coordinator(tmp_path, files_by_repo=files)
    _index_files_once_directly(db_path_for("repo-a"), files["repo-a"])
    # Simulate an install that already fully completed the pre-A3
    # calls+inherits migration (persisted version 2) -- not a fresh/never-
    # migrated repo (version 0/1), which the older tests above already cover.
    conn = sqlite3.connect(db_path_for("repo-a"))
    conn.execute("UPDATE index_meta SET cross_file_pass_version = 2 WHERE id = 0")
    conn.commit()
    conn.close()
    assert RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"overrides"}) == []
    coordinator._walk_repo = lambda repo_root: files.get(repo_root, [])

    coordinator.register("repo-a", "repo-a")

    assert _wait_until(
        lambda: RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"overrides"}) != []
    ), "cross-file overrides was never retroactively resolved for a repo stuck at the old calls+inherits version"
    overrides = RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"overrides"})
    assert len(overrides) == 1
    assert overrides[0].target == "pkg/other.py:Base.bar#method"
    write_queue.close()


def test_cross_file_migration_flag_persists_across_a_simulated_daemon_restart(tmp_path):
    """The in-memory _in_progress/_ready guards alone don't prove the flag
    is actually persisted to disk -- a brand-new BootstrapCoordinator
    instance (simulating a daemon restart) sharing the same on-disk
    index.sqlite must also skip a redundant re-walk, since only the
    persisted flag (not any in-process state) survives a restart.
    """
    files = {
        "repo-a": [
            ("pkg/mod.py", "from pkg.other import helper\n\n\nhelper()\n"),
            ("pkg/other.py", "def helper():\n    pass\n"),
        ]
    }
    db_paths: dict[str, str] = {}
    coordinator1, write_queue1, db_path_for = _make_coordinator(tmp_path, files_by_repo=files, db_paths=db_paths)
    _index_files_once_directly(db_path_for("repo-a"), files["repo-a"])
    coordinator1._walk_repo = lambda repo_root: files.get(repo_root, [])
    coordinator1.register("repo-a", "repo-a")
    assert _wait_until(
        lambda: RelationStore(db_path_for("repo-a")).list_by_site_file("pkg/mod.py", predicates={"calls"}) != []
    )
    write_queue1.close()

    walk_calls = []
    coordinator2, write_queue2, _ = _make_coordinator(tmp_path, files_by_repo=files, db_paths=db_paths)
    coordinator2._walk_repo = lambda repo_root: walk_calls.append(repo_root) or files.get(repo_root, [])

    coordinator2.register("repo-a", "repo-a")
    assert coordinator2.repo_ready("repo-a") is True
    time.sleep(0.1)  # give an incorrect redundant walk a chance to happen

    assert walk_calls == [], "migration re-walked after a simulated daemon restart despite the persisted flag"
    write_queue2.close()


def test_register_keys_readiness_on_repo_id_but_walks_the_given_repo_root(tmp_path):
    # decision 10's fix: two different repo_ids sharing the same repo_root
    # never comes up in practice (repo_id.py's resolve_repo_id and
    # resolve_repo_root are both derived from the same underlying repo),
    # but the reverse -- one repo_id, walked via its own distinct
    # repo_root -- is exactly the worktree scenario the fix must support:
    # register()'s first argument (repo_id) drives readiness bookkeeping,
    # its second (repo_root) is what actually gets passed to walk_repo.
    seen_roots = []

    def walk_repo(repo_root):
        seen_roots.append(repo_root)
        return [("pkg/mod.py", "def foo():\n    pass\n")]

    coordinator, write_queue, _ = _make_coordinator(tmp_path, files_by_repo={})
    coordinator._walk_repo = walk_repo

    coordinator.register("shared-repo-id", "/worktrees/a")

    assert _wait_until(lambda: coordinator.repo_ready("shared-repo-id"))
    assert seen_roots == ["/worktrees/a"]
    write_queue.close()
