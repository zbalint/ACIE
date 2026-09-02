import io
import json
import threading
import time

import acie.cli
import acie.daemon.runtime
from acie.cli import main
from acie.daemon.client import daemon_is_running
from acie.daemon.discovery import write_discovery_file
from acie.daemon.protocol import build_error_response, build_success_response
from acie.daemon.server import DaemonServer


def test_daemon_status_json_reports_stopped_when_no_discovery_file_exists(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = main(["daemon", "status", "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"running": False, "status": "stopped"}


def test_daemon_status_json_reports_stopped_for_a_stale_discovery_file(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_discovery_file(
        str(tmp_path / ".acie" / "daemon.json"),
        service_port=1,
        auth_token=None,
        daemon_pid=9999,
    )

    exit_code = main(["daemon", "status", "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"running": False, "status": "stopped"}


def test_daemon_status_json_probes_a_live_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = DaemonServer(
        lambda request: build_error_response(request["id"], "UNKNOWN_METHOD", "unknown"),
        port=0,
        discovery_path=str(tmp_path / ".acie" / "daemon.json"),
    )
    server.start()
    try:
        exit_code = main(["daemon", "status", "--json"])
    finally:
        server.shutdown()

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"running": True, "status": "running"}


def test_daemon_status_reports_shutting_down_for_a_daemon_mid_drain(monkeypatch, tmp_path, capsys):
    # Regression: status collapsed "shutdown has begun, drain still in
    # progress" and "process has fully exited" into the same "stopped"
    # result the instant shutdown() set _shutting_down, because ping just
    # got DAEMON_SHUTTING_DOWN back and daemon_is_running() treated any
    # non-ok ping as "not running". A live daemon mid-drain still holds
    # its port/PID for up to _SHUTDOWN_DRAIN_TIMEOUT_SECONDS -- status
    # must say so instead of claiming it's already stopped.
    monkeypatch.setenv("HOME", str(tmp_path))
    drain_started = threading.Event()
    release_drain = threading.Event()

    def slow_on_shutdown():
        drain_started.set()
        release_drain.wait(timeout=2)

    server = DaemonServer(
        lambda request: build_success_response(request["id"], {}),
        port=0,
        discovery_path=str(tmp_path / ".acie" / "daemon.json"),
        on_shutdown=slow_on_shutdown,
    )
    server.start()
    shutdown_thread = threading.Thread(target=server.shutdown)
    shutdown_thread.start()
    try:
        assert drain_started.wait(timeout=2)

        exit_code = main(["daemon", "status", "--json"])
    finally:
        release_drain.set()
        shutdown_thread.join(timeout=2)

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"running": False, "status": "shutting_down"}


def test_spawn_daemon_reaps_its_subprocess_so_it_never_zombies(monkeypatch, tmp_path):
    # Regression: subprocess.Popen()'s return value was discarded and
    # never waited on. If the spawned daemon exits quickly (e.g. it lost
    # the election-port race), nothing ever reaped it, so it stayed a
    # zombie for the entire lifetime of the long-running parent process
    # (acie serve-mcp).
    monkeypatch.setenv("HOME", str(tmp_path))
    waited = threading.Event()

    class FakeProc:
        def wait(self):
            waited.set()

    monkeypatch.setattr(acie.cli.subprocess, "Popen", lambda *args, **kwargs: FakeProc())

    acie.cli._spawn_daemon()

    assert waited.wait(timeout=2), "spawned subprocess was never reaped"


def test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    discovery_path = str(tmp_path / ".acie" / "daemon.json")

    try:
        assert main(["daemon", "start"]) == 0
        assert daemon_is_running(discovery_path)
        assert main(["daemon", "stop"]) == 0
        assert not daemon_is_running(discovery_path)
    finally:
        main(["daemon", "stop"])


def test_daemon_stop_uses_a_client_timeout_that_covers_the_servers_real_drain_budget(
    monkeypatch, tmp_path
):
    # Regression: _daemon_stop() sent the `shutdown` RPC with
    # request_daemon's 2.0s library-default client timeout, far shorter
    # than the server's real drain budget (runtime.py's
    # _SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 10.0) -- so `acie daemon stop`'s
    # own exit code was close to meaningless whenever a real drain took
    # longer than 2s (it read back as a client-side failure even though
    # the server-side shutdown was proceeding correctly). Patch the
    # runtime constant down so the test stays fast while still proving
    # _daemon_stop() reads the live value rather than a hardcoded default.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(acie.daemon.runtime, "_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", 3.0)
    release_drain = threading.Event()

    def slow_on_shutdown():
        release_drain.wait(timeout=3.0)

    server = DaemonServer(
        lambda request: build_success_response(request["id"], {}),
        port=0,
        discovery_path=str(tmp_path / ".acie" / "daemon.json"),
        on_shutdown=slow_on_shutdown,
    )
    server.start()

    def _release_after_delay():
        time.sleep(2.2)  # longer than the old hardcoded 2.0s client timeout
        release_drain.set()

    threading.Thread(target=_release_after_delay).start()

    exit_code = main(["daemon", "stop"])

    assert exit_code == 0


def test_notify_hook_returns_0_when_no_daemon_is_running(monkeypatch, tmp_path):
    # ARCHITECTURE.md "Agent Hook Integration": must never break or delay
    # the calling agent's tool-use flow -- an unreachable daemon is a
    # silent no-op, never a nonzero exit.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    exit_code = main(["notify-hook", "--agent", "claude-code"])

    assert exit_code == 0



def test_notify_hook_accepts_omp_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    exit_code = main(["notify-hook", "--agent", "omp"])

    assert exit_code == 0

def test_notify_hook_returns_0_even_when_the_daemon_answers_with_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    server = DaemonServer(
        lambda request: build_error_response(request["id"], "INTERNAL_ERROR", "boom"),
        port=0,
        discovery_path=str(tmp_path / ".acie" / "daemon.json"),
    )
    server.start()
    try:
        exit_code = main(["notify-hook", "--agent", "git"])
    finally:
        server.shutdown()

    assert exit_code == 0


def test_notify_hook_sends_the_agent_and_stdin_payload_to_the_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_input": {"file_path": "x.py"}}'))
    received = []

    def dispatch(request):
        received.append(request)
        return build_success_response(request["id"], {"status": "accepted"})

    server = DaemonServer(dispatch, port=0, discovery_path=str(tmp_path / ".acie" / "daemon.json"))
    server.start()
    try:
        exit_code = main(["notify-hook", "--agent", "claude-code"])
    finally:
        server.shutdown()

    assert exit_code == 0
    assert len(received) == 1
    assert received[0]["method"] == "notify_hook"
    assert received[0]["params"]["agent"] == "claude-code"
    assert received[0]["params"]["payload"] == '{"tool_input": {"file_path": "x.py"}}'
    assert received[0]["repo_path"] == str(tmp_path)
