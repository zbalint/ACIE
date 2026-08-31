import os
import signal
import socket
import threading
import time

import pytest

from acie.daemon.discovery import read_discovery_file
from acie.daemon.protocol import (
    build_error_response,
    build_request,
    build_success_response,
    decode_frame_body,
    decode_length_prefix,
    encode_frame,
)
from acie.daemon.server import AnotherDaemonRunningError, DaemonServer, install_signal_handlers


def _free_port() -> int:
    """Reserves an ephemeral port number, then releases it immediately.

    Good enough for tests: a small TOCTOU window exists, but nothing else
    on this machine is racing for it during a test run.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _send_request(port: int, request: dict, *, timeout: float = 2.0) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(encode_frame(request))
        prefix = _recv_exact(sock, 4)
        length = decode_length_prefix(prefix)
        body = _recv_exact(sock, length)
        return decode_frame_body(body)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed before sending the expected number of bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@pytest.fixture
def echo_dispatch():
    calls = []

    def dispatch(request):
        calls.append(request)
        return build_success_response(request["id"], {"echo": request.get("params")})

    dispatch.calls = calls
    return dispatch


def test_start_binds_an_ephemeral_port_when_port_is_zero(echo_dispatch):
    server = DaemonServer(echo_dispatch, port=0)
    server.start()
    try:
        assert server.port != 0
    finally:
        server.shutdown()


def test_dispatches_a_real_request_over_the_socket(echo_dispatch):
    server = DaemonServer(echo_dispatch, port=0)
    server.start()
    try:
        request = build_request("find_symbol", "/repo", {"name": "foo"})
        response = _send_request(server.port, request)
        assert response == {"id": request["id"], "ok": True, "result": {"echo": {"name": "foo"}}}
        assert echo_dispatch.calls == [request]
    finally:
        server.shutdown()


def test_handles_multiple_connect_per_call_requests_in_sequence(echo_dispatch):
    server = DaemonServer(echo_dispatch, port=0)
    server.start()
    try:
        for i in range(5):
            request = build_request("find_symbol", "/repo", {"i": i})
            response = _send_request(server.port, request)
            assert response["result"] == {"echo": {"i": i}}
        assert len(echo_dispatch.calls) == 5
    finally:
        server.shutdown()


def test_handles_concurrent_connections_each_on_its_own_thread():
    # A dispatch that blocks proves connections are served on separate
    # threads, not serialized behind one accept-then-handle loop.
    release = threading.Event()
    entered = threading.Barrier(3)

    def slow_dispatch(request):
        entered.wait(timeout=2)
        release.wait(timeout=2)
        return build_success_response(request["id"], {})

    server = DaemonServer(slow_dispatch, port=0)
    server.start()
    try:
        results = []

        def call():
            results.append(_send_request(server.port, build_request("m", "/r", {})))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        entered.wait(timeout=2)  # both connections + this test thread reached the barrier
        release.set()
        for t in threads:
            t.join(timeout=2)
        assert len(results) == 2
    finally:
        server.shutdown()


def test_malformed_frame_closes_the_connection_without_crashing_the_server(echo_dispatch):
    server = DaemonServer(echo_dispatch, port=0)
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as sock:
            sock.sendall(b"\xff\xff\xff\xff")  # declared length far exceeds MAX_MESSAGE_BYTES
            sock.settimeout(1)
            with pytest.raises((ConnectionError, OSError, TimeoutError)):
                data = sock.recv(4)
                if not data:
                    raise ConnectionError("closed")

        # Server must still be alive and serving other connections.
        request = build_request("find_symbol", "/repo", {})
        response = _send_request(server.port, request)
        assert response["ok"] is True
    finally:
        server.shutdown()


def test_shutdown_method_runs_on_shutdown_callback_and_deletes_discovery_file(tmp_path):
    on_shutdown_calls = []

    def on_shutdown():
        on_shutdown_calls.append(True)

    discovery_path = str(tmp_path / "daemon.json")
    server = DaemonServer(
        lambda req: build_success_response(req["id"], {}),
        port=0,
        discovery_path=discovery_path,
        on_shutdown=on_shutdown,
    )
    server.start()
    assert read_discovery_file(discovery_path) is not None

    response = _send_request(server.port, build_request("shutdown", "/repo", {}))

    assert response["ok"] is True
    assert on_shutdown_calls == [True]
    assert read_discovery_file(discovery_path) is None


def test_requests_arriving_during_an_in_flight_drain_get_daemon_shutting_down_error():
    # DAEMON.md: "stops accepting new RPCs (returning DAEMON_SHUTTING_DOWN
    # to anything that arrives after this point)" describes the window
    # while the drain is in flight -- the listening socket is still open
    # so a concurrent request gets a clean, retryable error rather than a
    # hung connection. (Once shutdown() fully completes and the socket is
    # closed, connection-refused is expected -- that's a different,
    # already-covered scenario.)
    drain_started = threading.Event()
    release_drain = threading.Event()

    def slow_on_shutdown():
        drain_started.set()
        release_drain.wait(timeout=2)

    server = DaemonServer(
        lambda req: build_success_response(req["id"], {}), port=0, on_shutdown=slow_on_shutdown
    )
    server.start()
    port = server.port

    shutdown_thread = threading.Thread(
        target=_send_request, args=(port, build_request("shutdown", "/repo", {}))
    )
    shutdown_thread.start()
    assert drain_started.wait(timeout=2)

    request = build_request("find_symbol", "/repo", {})
    response = _send_request(port, request)

    release_drain.set()
    shutdown_thread.join(timeout=2)

    assert response == build_error_response(
        request["id"], "DAEMON_SHUTTING_DOWN", response["error"]["message"]
    )


def test_shutdown_stops_the_accept_loop_new_connections_are_refused():
    server = DaemonServer(lambda req: build_success_response(req["id"], {}), port=0)
    server.start()
    port = server.port

    server.shutdown()

    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        except OSError:
            return  # refused, as expected
        time.sleep(0.05)
    pytest.fail("server still accepting connections after shutdown()")


def test_shutdown_is_idempotent():
    calls = []
    server = DaemonServer(
        lambda req: build_success_response(req["id"], {}),
        port=0,
        on_shutdown=lambda: calls.append(True),
    )
    server.start()

    server.shutdown()
    server.shutdown()

    assert calls == [True]


def test_election_port_bind_failure_raises_and_leaves_first_server_serving(echo_dispatch):
    election_port = _free_port()
    first = DaemonServer(echo_dispatch, port=0, election_port=election_port)
    first.start()
    try:
        second = DaemonServer(echo_dispatch, port=0, election_port=election_port)
        with pytest.raises(AnotherDaemonRunningError):
            second.start()

        # first daemon is unaffected and still serving.
        response = _send_request(first.port, build_request("m", "/r", {}))
        assert response["ok"] is True
    finally:
        first.shutdown()


def test_election_port_is_released_on_shutdown_so_a_new_daemon_can_start(echo_dispatch):
    election_port = _free_port()
    first = DaemonServer(echo_dispatch, port=0, election_port=election_port)
    first.start()
    first.shutdown()

    second = DaemonServer(echo_dispatch, port=0, election_port=election_port)
    second.start()  # must not raise
    second.shutdown()


def test_install_signal_handlers_wires_sigterm_to_shutdown():
    calls = []
    server = DaemonServer(
        lambda req: build_success_response(req["id"], {}),
        port=0,
        on_shutdown=lambda: calls.append(True),
    )
    server.start()

    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    try:
        install_signal_handlers(server)
        os.kill(os.getpid(), signal.SIGTERM)

        deadline = time.time() + 2
        while time.time() < deadline and not calls:
            time.sleep(0.01)
        assert calls == [True]
    finally:
        signal.signal(signal.SIGTERM, original_term)
        signal.signal(signal.SIGINT, original_int)

