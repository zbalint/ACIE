"""Shared loopback-RPC test support for daemon integration tests."""

import socket

from acie.daemon.protocol import decode_frame_body, decode_length_prefix, encode_frame


def send_request(port: int, request: dict, *, timeout: float = 2.0) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(encode_frame(request))
        prefix = recv_exact(sock, 4)
        body = recv_exact(sock, decode_length_prefix(prefix))
    return decode_frame_body(body)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise ConnectionError("peer closed before sending the expected number of bytes")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)
