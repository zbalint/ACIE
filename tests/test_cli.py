import json

from acie.cli import main
from acie.daemon.client import daemon_is_running
from acie.daemon.discovery import write_discovery_file
from acie.daemon.protocol import build_error_response
from acie.daemon.server import DaemonServer


def test_daemon_status_json_reports_stopped_when_no_discovery_file_exists(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = main(["daemon", "status", "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {"running": False}


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
    assert json.loads(capsys.readouterr().out) == {"running": False}


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
    assert json.loads(capsys.readouterr().out) == {"running": True}


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
