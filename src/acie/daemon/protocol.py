"""Wire framing and RPC envelope construction for the daemon transport.

See DAEMON.md "IPC Transport" and "Request/Response Envelope". Pure
byte<->dict functions only -- no socket I/O here. The daemon server and
`acie serve-mcp` client wire these into real loopback-TCP sockets in a
later slice; this module owns only the framing and envelope shapes both
sides agree on.
"""

import json
import struct
import uuid

# 4-byte big-endian length prefix, per DAEMON.md "IPC Transport".
LENGTH_PREFIX_SIZE = 4

# DAEMON.md names a "DAEMON_RPC_MAX_MESSAGE_BYTES-style cap" but defers the
# exact value to implementation time (see "Deferred to Implementation").
# 16 MiB comfortably covers the largest plausible single-tool response
# (e.g. an untruncated structural_search over a large repo) while still
# rejecting a wildly malformed/runaway length prefix outright.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class MalformedFrameError(Exception):
    """A length prefix or frame body couldn't be decoded, or exceeded MAX_MESSAGE_BYTES."""


def encode_frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise MalformedFrameError(
            f"frame body {len(body)} bytes exceeds MAX_MESSAGE_BYTES ({MAX_MESSAGE_BYTES})"
        )
    return struct.pack(">I", len(body)) + body


def decode_length_prefix(prefix: bytes) -> int:
    if len(prefix) != LENGTH_PREFIX_SIZE:
        raise MalformedFrameError(
            f"length prefix must be {LENGTH_PREFIX_SIZE} bytes, got {len(prefix)}"
        )
    (length,) = struct.unpack(">I", prefix)
    if length > MAX_MESSAGE_BYTES:
        raise MalformedFrameError(
            f"declared frame length {length} exceeds MAX_MESSAGE_BYTES ({MAX_MESSAGE_BYTES})"
        )
    return length


def decode_frame_body(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedFrameError(f"frame body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MalformedFrameError(
            f"frame body must decode to a JSON object, got {type(payload).__name__}"
        )
    return payload


def build_request(method: str, repo_path: str, params: dict, token: str | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "token": token,
        "method": method,
        "repo_path": repo_path,
        "params": params,
    }


def build_success_response(request_id: str, result) -> dict:
    return {"id": request_id, "ok": True, "result": result}


def build_error_response(
    request_id: str, code: str, message: str, *, details: dict | None = None
) -> dict:
    error = {"code": code, "message": message}
    if details:
        error.update(details)
    return {"id": request_id, "ok": False, "error": error}
