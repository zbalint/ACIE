import json
import subprocess
import sys
import time
import threading
from pathlib import Path

import pytest

from acie.daemon.lsp_client import LspClient, LspError
from acie.daemon.pyright_process import PyrightProcess


_FIXTURE = Path(__file__).with_name("_fake_lsp_server.py")


def _client(tmp_path):
    log_path = tmp_path / "server.jsonl"
    popen = subprocess.Popen(
        [sys.executable, str(_FIXTURE), str(log_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    process = PyrightProcess(str(tmp_path), sys.executable, None, popen)
    return LspClient(process), process, log_path


def _events(log_path):
    deadline = time.monotonic() + 1
    while not log_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def _initialized_client(tmp_path):
    client, process, log_path = _client(tmp_path)
    client.initialize(str(tmp_path))
    return client, process, log_path


def test_initialize_sends_the_handshake_and_returns_the_initialize_result(tmp_path):
    client, process, log_path = _client(tmp_path)
    try:
        result = client.initialize(str(tmp_path))

        initialize = next(event for event in _events(log_path) if event.get("method") == "initialize")
        assert result == {"capabilities": {"definitionProvider": True}}
        assert initialize["params"]["processId"] > 0
        assert initialize["params"]["rootUri"] == tmp_path.as_uri()
        assert initialize["params"]["rootPath"] == str(tmp_path)
        assert initialize["params"]["workspaceFolders"] == [{"uri": tmp_path.as_uri(), "name": tmp_path.name}]
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_initialize_sends_initialized_notification_after_the_response(tmp_path):
    client, process, log_path = _client(tmp_path)
    try:
        client.initialize(str(tmp_path))

        deadline = time.monotonic() + 1
        while len(_events(log_path)) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        methods = [event.get("method") for event in _events(log_path)]
        assert methods[:2] == ["initialize", "initialized"]
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_initialize_populates_server_capabilities_from_the_response(tmp_path):
    client, process, _ = _client(tmp_path)
    try:
        client.initialize(str(tmp_path))

        assert client.server_capabilities == {"definitionProvider": True}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_initialize_raises_if_called_twice(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            client.initialize(str(tmp_path))
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_send_request_before_initialize_raises_runtime_error(tmp_path):
    client, process, _ = _client(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            client.send_request("echo", {})
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_send_notification_before_initialize_raises_runtime_error(tmp_path):
    client, process, _ = _client(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            client.send_notification("notice", {})
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_send_request_returns_a_future_resolved_with_the_result(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    try:
        assert client.send_request("echo", {"value": 1}).result(timeout=1) == {"echo": {"value": 1}}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_send_request_future_raises_lsp_error_on_an_error_response(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    try:
        with pytest.raises(LspError, match="reserved error") as caught:
            client.send_request("error", {}).result(timeout=1)
        assert caught.value.code == -32601
        assert caught.value.data == {"method": "error"}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_two_concurrent_send_request_calls_each_resolve_to_their_own_result(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    barrier = threading.Barrier(3)
    futures = []

    def send(params):
        barrier.wait()
        futures.append(client.send_request("echo", params))

    first_thread = threading.Thread(target=send, args=({"request": "first"},))
    second_thread = threading.Thread(target=send, args=({"request": "second"},))
    first_thread.start()
    second_thread.start()
    barrier.wait()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)
    try:
        assert {future.result(timeout=1)["echo"]["request"] for future in futures} == {"first", "second"}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_a_server_initiated_request_receives_a_generic_auto_reply(tmp_path):
    client, process, log_path = _initialized_client(tmp_path)
    try:
        assert client.send_request("server_request", {}).result(timeout=1) == {"echo": {}}

        response = next(event["server_request_response"] for event in _events(log_path) if "server_request_response" in event)
        assert response == {"jsonrpc": "2.0", "id": "server-request", "result": None}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_a_server_initiated_notification_does_not_crash_the_reader_loop(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    try:
        assert client.send_request("notification", {}).result(timeout=1) == {"echo": {}}
        assert client.send_request("echo", {"still": "works"}).result(timeout=1) == {"echo": {"still": "works"}}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_a_malformed_frame_does_not_crash_the_reader_loop(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    try:
        assert client.send_request("malformed", {}).result(timeout=1) == {"echo": {}}
        assert client.send_request("echo", {"still": "works"}).result(timeout=1) == {"echo": {"still": "works"}}
    finally:
        client.close()
        process.popen.terminate()
        process.popen.wait(timeout=1)


def test_pending_requests_are_resolved_with_connection_error_when_the_process_exits(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    try:
        with pytest.raises(ConnectionError, match="stdout closed"):
            client.send_request("exit_without_response", {}).result(timeout=1)
    finally:
        client.close()
        process.popen.wait(timeout=1)


def test_close_sends_shutdown_then_exit_and_joins_the_reader_thread(tmp_path):
    client, process, log_path = _initialized_client(tmp_path)
    client.close()

    assert [event.get("method") for event in _events(log_path)][-2:] == ["shutdown", "exit"]
    assert client._reader_thread.is_alive() is False
    process.popen.wait(timeout=1)


def test_close_resolves_any_still_pending_futures(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    future = client.send_request("exit_without_response", {})
    client.close()

    with pytest.raises(ConnectionError):
        future.result(timeout=1)
    process.popen.wait(timeout=1)


def test_close_is_idempotent(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    client.close()
    client.close()

    process.popen.wait(timeout=1)


def test_send_request_blocked_on_the_write_lock_is_rejected_when_close_starts(tmp_path):
    client, process, _ = _initialized_client(tmp_path)
    sender_started = threading.Event()
    errors = []

    def send():
        sender_started.set()
        try:
            client.send_request("echo", {})
        except RuntimeError as exc:
            errors.append(exc)

    with client._write_lock:
        sender = threading.Thread(target=send)
        sender.start()
        assert sender_started.wait(timeout=1)
        closer = threading.Thread(target=client.close)
        closer.start()
        deadline = time.monotonic() + 1
        while not client._closing and time.monotonic() < deadline:
            time.sleep(0.01)

    sender.join(timeout=1)
    closer.join(timeout=1)
    assert errors and "closed" in str(errors[0])
    assert sender.is_alive() is False
    assert closer.is_alive() is False
    assert client._pending == {}
    process.popen.wait(timeout=1)


def test_close_does_not_touch_the_underlying_popen(tmp_path, monkeypatch):
    client, process, _ = _initialized_client(tmp_path)
    popen = process.popen
    lifecycle_calls = []

    for method_name in ("terminate", "kill", "wait"):
        monkeypatch.setattr(
            popen,
            method_name,
            lambda *args, _method_name=method_name, **kwargs: lifecycle_calls.append(_method_name),
        )

    client.close()

    deadline = time.monotonic() + 1
    while process.is_alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.popen is popen
    assert lifecycle_calls == []
    assert process.is_alive is False
