"""Shared low-level socket I/O for the daemon's connect-per-call transport.

Both client.py (an already-discovered daemon's connect-per-call client) and
server.py (the daemon's own connection-handling loop) need the exact same
byte-accumulation loop to read a request/response off a socket. Kept here,
not in protocol.py -- that module's own docstring is explicit ("no socket
I/O here"), and this keeps that invariant true -- and not shared via a
client->server or server->client import, which would invert the layering
either way.
"""

import socket


def recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Reads exactly n bytes, or None if the peer closes before n bytes arrive."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
