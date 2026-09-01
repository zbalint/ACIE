import os
import socket
import threading

from acie.daemon.client import request_daemon
from acie.daemon.discovery import write_discovery_file


def test_request_daemon_returns_none_instead_of_raising_on_a_malformed_response_frame(tmp_path):
    # Regression: request_daemon's `except (OSError, ValueError)` missed
    # protocol.MalformedFrameError, so a corrupt/oversized response frame
    # crashed every caller relying on its documented "None when
    # unavailable" contract (daemon_is_running, _daemon_status,
    # _ensure_daemon, the MCP adapter's daemon calls).
    discovery_path = str(tmp_path / "daemon.json")

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    def fake_daemon():
        conn, _addr = server_sock.accept()
        conn.recv(65536)  # drain the request
        conn.sendall(b"\xff\xff\xff\xff")  # declared length far exceeds MAX_MESSAGE_BYTES
        conn.close()

    write_discovery_file(discovery_path, service_port=port, auth_token=None, daemon_pid=os.getpid())
    thread = threading.Thread(target=fake_daemon, daemon=True)
    thread.start()

    response = request_daemon(discovery_path, method="ping", repo_path="/r", params={})

    assert response is None
    thread.join(timeout=2)
    server_sock.close()
