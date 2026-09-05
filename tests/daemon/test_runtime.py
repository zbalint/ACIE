import os
import shutil
import subprocess
import threading
import time

import pytest

from acie.daemon import runtime
from acie.daemon.protocol import build_request
from acie.daemon import repo_fingerprint
from acie.daemon.repo_fingerprint import compute_repo_fingerprint
from acie.daemon.runtime import create_daemon, ensure_fresh
from acie.daemon.write_queue import WriteQueue
from acie.storage.index_meta_store import IndexMetaStore
from tests.daemon.rpc import send_request


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()

_DEBOUNCE_WAIT = 1.0


def test_runtime_bootstraps_a_real_repo_then_dispatches_its_indexed_tool_request(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        request = build_request("find_symbol", str(repo), {"name": "target"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, request)
            if response["ok"]:
                break
            assert response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        assert [item["qualname"] for item in response["result"]["results"]] == ["target"]
    finally:
        server.shutdown()


def test_runtime_bootstrap_skips_files_the_repos_own_gitignore_excludes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def kept():\n    pass\n", encoding="utf-8")
    (repo / ".gitignore").write_text("generated.py\n", encoding="utf-8")
    (repo / "generated.py").write_text("def excluded():\n    pass\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        # Poll on the file we DO expect to become ready, then assert the
        # ignored one never shows up -- there's no separate signal for
        # "bootstrap decided not to index this file".
        request = build_request("find_symbol", str(repo), {"name": "kept"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, request)
            if response["ok"]:
                break
            assert response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        excluded_response = send_request(
            server.port, build_request("find_symbol", str(repo), {"name": "excluded"})
        )
        assert excluded_response["ok"] is True
        assert excluded_response["result"]["results"] == []
    finally:
        server.shutdown()


def test_runtime_watcher_picks_up_a_file_added_after_bootstrap_completes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        # Wait for bootstrap to finish (repo becomes ready) before adding a
        # second file -- this test is specifically about the watcher's
        # post-bootstrap trigger, not bootstrap's own initial walk.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, build_request("find_symbol", str(repo), {"name": "target"}))
            if response["ok"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        (repo / "added_later.py").write_text("def new_symbol():\n    pass\n", encoding="utf-8")

        deadline = time.monotonic() + 2 + _DEBOUNCE_WAIT
        found = False
        while time.monotonic() < deadline:
            response = send_request(
                server.port, build_request("find_symbol", str(repo), {"name": "new_symbol"})
            )
            if response["ok"] and response["result"]["results"]:
                found = True
                break
            time.sleep(0.05)

        assert found, "watcher never reindexed added_later.py within the debounce+poll window"
    finally:
        server.shutdown()


def test_runtime_routes_notify_hook_method_to_the_git_agent_handler(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first"], check=True)

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        # First call: nothing recorded yet -- just records the starting HEAD.
        first = send_request(
            server.port, build_request("notify_hook", str(repo), {"agent": "git", "payload": ""})
        )
        assert first["ok"] is True

        (repo / "module.py").write_text("def renamed_target():\n    pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "commit", "-aq", "-m", "second"], check=True)

        second = send_request(
            server.port, build_request("notify_hook", str(repo), {"agent": "git", "payload": ""})
        )
        assert second["ok"] is True

        deadline = time.monotonic() + 2
        found = False
        while time.monotonic() < deadline:
            response = send_request(
                server.port, build_request("find_symbol", str(repo), {"name": "renamed_target"})
            )
            if response["ok"] and response["result"]["results"]:
                found = True
                break
            time.sleep(0.02)
        assert found, "git-hook-triggered reindex never surfaced the renamed function"
    finally:
        server.shutdown()


def test_runtime_dedupes_write_queue_and_bootstrap_state_across_repo_path_spellings(tmp_path):
    # decision 10's fix (SALTMDB f4bdfc9d, grilled 2026-09-02): a symlink
    # and its realpath'd twin are two different repo_path strings for the
    # exact same underlying repo -- the currently-reachable trigger found
    # by comparing cli.py's notify-hook (os.getcwd(), not realpath'd)
    # against mcp_server.py's (os.path.realpath(os.getcwd())). Before the
    # fix, WriteQueue/BootstrapCoordinator/WatcherRegistry keyed on the
    # raw string each treated these as two unrelated repos, sharing one
    # on-disk index.sqlite but never each other's in-memory state.
    real_repo = tmp_path / "real_repo"
    real_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(real_repo)], check=True)
    (real_repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")
    real_repo_path = os.path.realpath(str(real_repo))
    symlinked_repo = tmp_path / "symlinked_repo"
    symlinked_repo.symlink_to(real_repo_path, target_is_directory=True)

    state_dir = tmp_path / "state"
    server = create_daemon(state_dir=str(state_dir), port=0)
    server.start()
    try:
        # Bootstrap via the symlinked spelling and wait for it to finish.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(
                server.port, build_request("find_symbol", str(symlinked_repo), {"name": "target"})
            )
            if response["ok"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing via the symlinked path")

        # The realpath'd spelling of the identical repo must already be
        # ready -- it shares the symlinked spelling's repo_id-keyed
        # BootstrapCoordinator state, not a fresh one of its own.
        response = send_request(server.port, build_request("find_symbol", real_repo_path, {"name": "target"}))
        assert response["ok"] is True
        assert [item["qualname"] for item in response["result"]["results"]] == ["target"]

        # Only one repos/<repo-id>/ directory was ever created -- confirms
        # the write-queue/bootstrap state was genuinely shared, not just
        # coincidentally both-ready from two independent walks.
        repo_dirs = list((state_dir / "repos").iterdir())
        assert len(repo_dirs) == 1
    finally:
        server.shutdown()


def test_runtime_worktree_smoke_shared_bootstrap_state_does_not_crash_or_deadlock(tmp_path):
    # decision 10's fix: two live worktrees of one repo now share one
    # repo_id-keyed write queue and bootstrap-readiness flag. Full
    # multi-worktree walk-merging semantics are explicitly out of v0 scope
    # (ARCHITECTURE.md "Not Yet Specified") -- this only smoke-tests that
    # the shared-state path doesn't crash or deadlock and matches the
    # agreed first-registrant-wins behavior, not that both worktrees'
    # possibly-diverged content ends up indexed.
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(["git", "-C", str(main_repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(main_repo), "config", "user.name", "T"], check=True)
    (main_repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main_repo), "commit", "-q", "-m", "first"], check=True)

    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-b", "other-branch", str(worktree)], check=True
    )

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, build_request("find_symbol", str(main_repo), {"name": "target"}))
            if response["ok"]:
                break
            assert response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("main worktree did not finish bootstrap indexing")

        # Registering the second worktree (shared repo_id, distinct
        # repo_root) must not crash or deadlock, and per the agreed
        # first-registrant-wins behavior it reads as ready immediately too.
        response = send_request(server.port, build_request("find_symbol", str(worktree), {"name": "target"}))
        assert response["ok"] is True
    finally:
        server.shutdown()


def test_runtime_list_imports_sees_a_fresh_edit_with_no_wait_for_the_watchers_debounce(tmp_path):
    # Tier 4 (DAEMON.md "Incremental Indexing Wiring"): proves the *very
    # next* query reflects an on-disk edit with a single immediate call --
    # unlike the watcher tests above, this deliberately never polls/sleeps
    # for the ~500ms debounce window, since the point is that tier 4 makes
    # that wait unnecessary for the 3 tools it covers.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("import os\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, build_request("list_imports", str(repo), {"file": "module.py"}))
            if response["ok"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")
        assert len(response["result"]["results"]) == 1

        (repo / "module.py").write_text("import os\nimport sys\n", encoding="utf-8")

        response = send_request(server.port, build_request("list_imports", str(repo), {"file": "module.py"}))
        assert response["ok"] is True
        assert len(response["result"]["results"]) == 2
    finally:
        server.shutdown()


def test_runtime_get_definition_by_position_sees_a_fresh_edit_with_no_wait_for_the_watchers_debounce(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, build_request("find_symbol", str(repo), {"name": "target"}))
            if response["ok"] and response["result"]["results"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        (repo / "module.py").write_text(
            "def target():\n    pass\n\n\ndef caller():\n    target()\n", encoding="utf-8"
        )

        response = send_request(
            server.port,
            build_request("get_definition", str(repo), {"position": {"file": "module.py", "line": 6, "column": 4}}),
        )
        assert response["ok"] is True
        assert [r["id"] for r in response["result"]["results"]] == ["module.py:target#function"]
    finally:
        server.shutdown()


def test_ensure_fresh_never_blocks_the_caller_past_its_own_timeout_when_the_writer_thread_is_backlogged(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "module.py").write_text("import os\n", encoding="utf-8")
    db_path = str(tmp_path / "index.sqlite")
    write_queue = WriteQueue(db_path_for=lambda repo_id: db_path)

    release = threading.Event()

    def slow_job(conn):
        release.wait(timeout=5)

    write_queue.submit("repo-1", slow_job)  # occupies repo-1's only writer thread

    start = time.monotonic()
    ensure_fresh(write_queue, "repo-1", str(repo_root), "list_imports", {"file": "module.py"}, timeout=0.1)
    elapsed = time.monotonic() - start

    release.set()
    write_queue.close(timeout=2)

    assert elapsed < 1.0, "ensure_fresh blocked well past its own bounded timeout"


def test_runtime_list_imports_with_a_traversal_file_param_never_reads_outside_the_repo(tmp_path):
    # P1 regression (codex review, 2026-09-02): a "../outside.py" file
    # param used to reach make_reindex_job with no containment check,
    # letting tier 4 read/index a file outside the repo. This proves the
    # fix end-to-end: the request completes normally (no crash, no
    # INTERNAL_ERROR) and the file living just outside the repo is never
    # touched.
    outside = tmp_path / "outside.py"
    outside.write_text("import shutil\n", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("import os\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = send_request(server.port, build_request("list_imports", str(repo), {"file": "module.py"}))
            if response["ok"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        response = send_request(
            server.port, build_request("list_imports", str(repo), {"file": "../outside.py"})
        )
        assert response["ok"] is True
        assert response["result"]["results"] == []

        # The traversal attempt must not have caused outside.py to be read
        # and indexed under some collapsed/mangled path either.
        untouched = send_request(server.port, build_request("find_symbol", str(repo), {"name": "shutil"}))
        assert untouched["ok"] is True
        assert untouched["result"]["results"] == []
    finally:
        server.shutdown()


def test_trigger_enrichment_offloads_reentrant_write_queue_follow_up(tmp_path, monkeypatch):
    repo_id = "repo-a"
    db_path = str(tmp_path / "index.sqlite")
    write_queue = WriteQueue(db_path_for=lambda _repo_id: db_path)
    follow_up_ran = threading.Event()
    worker_finished = threading.Event()

    def fake_run_enrichment_pass(**kwargs):
        try:
            follow_up = kwargs["write_queue"].submit(repo_id, lambda conn: follow_up_ran.set())
            follow_up.result(timeout=5.0)
        finally:
            worker_finished.set()

    monkeypatch.setattr(runtime, "run_enrichment_pass", fake_run_enrichment_pass, raising=False)
    try:
        outer = write_queue.submit(
            repo_id,
            lambda conn: runtime.trigger_enrichment(
                object(),
                write_queue,
                lambda _repo_id: db_path,
                repo_id,
                str(tmp_path),
            ),
        )
        outer.result(timeout=5.0)
        assert follow_up_ran.wait(timeout=5.0), "triggered enrichment follow-up never ran"
        assert worker_finished.wait(timeout=5.0), "enrichment worker did not finish"
    finally:
        worker_finished.wait(timeout=5.0)
        write_queue.close(timeout=5.0)


def test_trigger_enrichment_returns_before_a_slow_process_registry_finishes(tmp_path):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowProcessRegistry:
        def ensure_process(self, repo_root):
            started.set()
            release.wait(timeout=2.0)
            finished.set()
            return None

    start = time.monotonic()
    try:
        runtime.trigger_enrichment(
            SlowProcessRegistry(),
            object(),
            lambda repo_id: str(tmp_path / "index.sqlite"),
            "repo-a",
            str(tmp_path),
        )
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "trigger_enrichment waited for the enrichment pass"
        assert started.wait(timeout=1.0), "enrichment thread never started"
    finally:
        release.set()

    assert finished.wait(timeout=2.0), "enrichment thread did not finish after release"


def test_trigger_enrichment_logs_and_swallows_background_setup_errors(tmp_path, monkeypatch):
    warning_called = threading.Event()
    messages = []

    def warning(message, *args, **kwargs):
        messages.append(message)
        warning_called.set()

    monkeypatch.setattr(runtime._logger, "warning", warning)

    def db_path_for(repo_id):
        raise RuntimeError("index path unavailable")

    runtime.trigger_enrichment(object(), object(), db_path_for, "repo-a", str(tmp_path))

    assert warning_called.wait(timeout=2.0), "background setup error was not logged"
    assert "could not open index" in messages[0]


def test_create_daemon_routes_indexed_and_watcher_triggers_through_one_scheduler_and_closes_registry(
    tmp_path, monkeypatch
):
    registries = []
    schedulers = []
    close_timeouts = []
    indexed_callbacks = []
    watcher_callbacks = []
    indexed_triggers = []
    watcher_triggers = []

    class FakeProcessRegistry:
        def __init__(self):
            registries.append(self)

        def close(self, timeout=None):
            close_timeouts.append(timeout)

    class FakeScheduler:
        def __init__(self, process_registry, *args, **kwargs):
            self.process_registry = process_registry
            schedulers.append(self)

        def trigger_now(self, repo_id, repo_root):
            indexed_triggers.append((repo_id, repo_root))

        def on_watcher_edit(self, repo_id, repo_root):
            watcher_triggers.append((repo_id, repo_root))

    class FakeBootstrapCoordinator:
        def __init__(self, **kwargs):
            indexed_callbacks.append(kwargs["on_indexed"])

        def repo_ready(self, repo_id):
            return False

        def register(self, repo_id, repo_root):
            pass

    class FakeWatcherRegistry:
        def __init__(self, write_queue, *, on_paths_changed):
            watcher_callbacks.append(on_paths_changed)

        def register(self, repo_id, repo_root):
            pass

        def close(self, timeout=None):
            pass

    monkeypatch.setattr(runtime, "PyrightProcessRegistry", FakeProcessRegistry)
    monkeypatch.setattr(runtime, "EnrichmentScheduler", FakeScheduler)
    monkeypatch.setattr(runtime, "BootstrapCoordinator", FakeBootstrapCoordinator)
    monkeypatch.setattr(runtime, "WatcherRegistry", FakeWatcherRegistry)

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    try:
        indexed_callbacks[0]("repo-a", "/repo-a")
        watcher_callbacks[0]("repo-b", "/repo-b")
    finally:
        server.shutdown()

    assert len(registries) == 1
    assert len(schedulers) == 1
    assert schedulers[0].process_registry is registries[0]
    assert indexed_triggers == [("repo-a", "/repo-a")]
    assert watcher_triggers == [("repo-b", "/repo-b")]
    assert len(close_timeouts) == 1
    assert 0.0 <= close_timeouts[0] <= runtime._SHUTDOWN_DRAIN_TIMEOUT_SECONDS


def test_triggered_enrichment_persists_the_current_fingerprint_after_a_successful_pass(tmp_path, monkeypatch):
    db_path = str(tmp_path / "index.sqlite")
    IndexMetaStore(db_path)
    calls = []

    def fake_run_enrichment_pass(**kwargs):
        calls.append(kwargs["repo_id"])
        return []

    monkeypatch.setattr(runtime, "run_enrichment_pass", fake_run_enrichment_pass)
    monkeypatch.setattr(runtime, "compute_repo_fingerprint", lambda repo_root: "fingerprint-1")

    runtime._run_triggered_enrichment(
        object(),
        object(),
        lambda repo_id: db_path,
        "repo-a",
        str(tmp_path),
        lambda repo_root: [],
    )

    assert calls == ["repo-a"]
    assert IndexMetaStore(db_path).get_last_enrichment_fingerprint() == "fingerprint-1"


def test_triggered_enrichment_keeps_the_previous_fingerprint_when_git_is_unavailable(tmp_path, monkeypatch):
    db_path = str(tmp_path / "index.sqlite")
    IndexMetaStore(db_path).set_last_enrichment_fingerprint("previous")

    monkeypatch.setattr(runtime, "run_enrichment_pass", lambda **kwargs: [])
    monkeypatch.setattr(runtime, "compute_repo_fingerprint", lambda repo_root: None)

    runtime._run_triggered_enrichment(
        object(),
        object(),
        lambda repo_id: db_path,
        "repo-a",
        str(tmp_path),
        lambda repo_root: [],
    )

    assert IndexMetaStore(db_path).get_last_enrichment_fingerprint() == "previous"


def test_runtime_watcher_edit_triggers_a_second_enrichment_pass_without_restart(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")

    calls = []
    calls_lock = threading.Lock()
    first_pass = threading.Event()

    def fake_run_enrichment_pass(**kwargs):
        with calls_lock:
            calls.append((kwargs["repo_id"], kwargs["repo_root"]))
        first_pass.set()
        return []

    real_scheduler = runtime.EnrichmentScheduler

    def fast_scheduler(process_registry, write_queue, db_path_for, *, walk_repo):
        return real_scheduler(
            process_registry,
            write_queue,
            db_path_for,
            walk_repo=walk_repo,
            quiet_seconds=0.05,
            max_wait_seconds=0.2,
        )

    monkeypatch.setattr(runtime, "run_enrichment_pass", fake_run_enrichment_pass)
    monkeypatch.setattr(runtime, "EnrichmentScheduler", fast_scheduler)
    state_dir = tmp_path / "state"
    server = create_daemon(state_dir=str(state_dir), port=0)
    server.start()
    try:
        request = build_request("find_symbol", str(repo), {"name": "target"})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            response = send_request(server.port, request)
            if response["ok"]:
                break
            assert response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        assert first_pass.wait(timeout=2.0)
        time.sleep(0.2)  # settle the startup reconciliation's one-shot pass
        with calls_lock:
            initial_count = len(calls)

        (repo / "added.py").write_text("def new_symbol():\n    pass\n", encoding="utf-8")

        assert _wait_until(
            lambda: len(calls) > initial_count,
            timeout=3.0,
        ), "watcher edit never triggered another enrichment pass"
    finally:
        server.shutdown()


def test_runtime_reconciles_a_tracked_edit_after_a_daemon_restart(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    tracked = repo / "module.py"
    tracked.write_text("def target():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    calls = []
    calls_lock = threading.Lock()

    def fake_run_enrichment_pass(**kwargs):
        with calls_lock:
            calls.append((kwargs["repo_id"], kwargs["repo_root"]))
        return []

    monkeypatch.setattr(runtime, "run_enrichment_pass", fake_run_enrichment_pass)
    state_dir = tmp_path / "state"
    server_one = create_daemon(state_dir=str(state_dir), port=0)
    server_one.start()
    try:
        request = build_request("find_symbol", str(repo), {"name": "target"})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            response = send_request(server_one.port, request)
            if response["ok"]:
                break
            assert response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")
        assert _wait_until(lambda: len(calls) >= 1)
        time.sleep(0.2)
    finally:
        server_one.shutdown()

    tracked.write_text("def renamed_target():\n    pass\n", encoding="utf-8")
    server_two = create_daemon(state_dir=str(state_dir), port=0)
    server_two.start()
    try:
        response = send_request(server_two.port, request)
        assert response["ok"] is True
        assert _wait_until(lambda: len(calls) >= 2), "restart reconciliation never re-enriched the edited repo"
    finally:
        server_two.shutdown()


def test_runtime_bootstrap_triggers_enrichment_for_an_ambiguous_cross_file_call(tmp_path):
    if shutil.which("basedpyright-langserver") is None:
        pytest.skip("basedpyright-langserver is not installed")

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "vendor" / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "pkg" / "mod.py").write_text(
        "from pkg.other import helper\n\n\ndef caller():\n    helper()\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "other.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    (repo / "vendor" / "pkg" / "other.py").write_text("def helper():\n    pass\n", encoding="utf-8")

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        ready_request = build_request("find_symbol", str(repo), {"name": "caller"})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ready_response = send_request(server.port, ready_request)
            if ready_response["ok"]:
                break
            assert ready_response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        graph_request = build_request(
            "graph",
            str(repo),
            {
                "root": "pkg/mod.py:caller#function",
                "graph_type": "call",
                "direction": "downstream",
                "full": True,
            },
        )
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            graph_response = send_request(server.port, graph_request)
            if graph_response["ok"]:
                inferred = [
                    edge
                    for edge in graph_response["result"]["edges"]
                    if edge.get("confidence") == "INFERRED"
                ]
                if inferred:
                    break
            else:
                assert graph_response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.1)
        else:
            raise AssertionError("daemon enrichment never inferred the ambiguous call")

        assert inferred[0]["target"] == "pkg/other.py:helper#function"
    finally:
        server.shutdown()


def test_runtime_bootstrap_triggers_enrichment_without_a_pyright_binary(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (repo / "other.py").write_text("def other():\n    pass\n", encoding="utf-8")

    triggered = threading.Event()
    calls = []

    def fake_run_enrichment_pass(**kwargs):
        calls.append((kwargs["repo_id"], kwargs["repo_root"]))
        triggered.set()
        return []

    monkeypatch.setattr(runtime, "run_enrichment_pass", fake_run_enrichment_pass)

    server = create_daemon(state_dir=str(tmp_path / "state"), port=0)
    server.start()
    try:
        request = build_request("find_symbol", str(repo), {"name": "target"})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            response = send_request(server.port, request)
            if response["ok"]:
                break
            assert response["error"]["code"] == "INDEX_NOT_READY"
            time.sleep(0.01)
        else:
            raise AssertionError("repo did not finish bootstrap indexing")

        assert triggered.wait(timeout=2.0), "bootstrap never triggered the enrichment pass"
        time.sleep(0.1)
        assert 1 <= len(calls) <= 2
        assert all(call[1] == str(repo) for call in calls)
    finally:
        server.shutdown()


def test_repo_fingerprint_changes_for_tracked_and_untracked_worktree_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    tracked = repo / "module.py"
    tracked.write_text("def target():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    clean = compute_repo_fingerprint(str(repo))
    tracked.write_text("def renamed_target():\n    pass\n", encoding="utf-8")
    tracked_changed = compute_repo_fingerprint(str(repo))
    (repo / "untracked.py").write_text("def new_symbol():\n    pass\n", encoding="utf-8")
    untracked_changed = compute_repo_fingerprint(str(repo))

    assert clean is not None
    assert tracked_changed is not None
    assert untracked_changed is not None
    assert tracked_changed != clean
    assert untracked_changed != tracked_changed


def test_repo_fingerprint_returns_none_when_git_cannot_describe_the_directory(tmp_path):
    assert compute_repo_fingerprint(str(tmp_path)) is None


def test_repo_fingerprint_returns_none_when_git_subprocess_raises(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(repo_fingerprint.subprocess, "run", fail)

    assert compute_repo_fingerprint(str(tmp_path)) is None
