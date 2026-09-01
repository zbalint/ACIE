"""Connect-per-call client helpers for an already-discovered ACIE daemon."""

import socket

from acie.daemon.discovery import read_discovery_file
from acie.daemon.protocol import (
    LENGTH_PREFIX_SIZE,
    MalformedFrameError,
    build_request,
    decode_frame_body,
    decode_length_prefix,
    encode_frame,
)
from acie.daemon.sockets import recv_exact


def request_daemon(
    discovery_path: str,
    *,
    method: str,
    repo_path: str,
    params: dict,
    timeout: float = 2.0,
) -> dict | None:
    """Send one request using discovery state, or return None when unavailable."""
    discovery = read_discovery_file(discovery_path)
    if not isinstance(discovery, dict):
        return None
    port = discovery.get("service_port")
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    request = build_request(method, repo_path, params, token=discovery.get("auth_token"))
    try:
        return _request(port, request, timeout=timeout)
    except (OSError, ValueError, MalformedFrameError):
        return None


def daemon_is_running(discovery_path: str, *, timeout: float = 0.2) -> bool:
    """Return whether discovery points at a daemon that responds to ping."""
    response = request_daemon(
        discovery_path, method="ping", repo_path="", params={}, timeout=timeout
    )
    return response is not None and response.get("ok") is True


def _request(port: int, request: dict, *, timeout: float) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encode_frame(request))
        prefix = recv_exact(sock, LENGTH_PREFIX_SIZE)
        if prefix is None:
            raise OSError("daemon closed its connection before sending a full response")
        body = recv_exact(sock, decode_length_prefix(prefix))
        if body is None:
            raise OSError("daemon closed its connection before sending a full response")
    return decode_frame_body(body)
