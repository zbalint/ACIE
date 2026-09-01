import subprocess
import time

from acie.daemon.protocol import build_request
from acie.daemon.runtime import create_daemon
from tests.daemon.rpc import send_request


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
