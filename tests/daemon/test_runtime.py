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
