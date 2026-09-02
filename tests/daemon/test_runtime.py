import os
import subprocess
import time

from acie.daemon.protocol import build_request
from acie.daemon.runtime import create_daemon
from tests.daemon.rpc import send_request

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
