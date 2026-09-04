"""Pure Content-Length framing for the external Language Server Protocol.

This intentionally stays separate from ``protocol.py``: ACIE's daemon framing
and the LSP's independently-versioned HTTP-derived framing share JSON bodies
but not a protocol contract.
"""

import json


class MalformedLspFrameError(Exception):
    """An LSP header or JSON body could not be decoded."""


def encode_frame(payload: dict) -> bytes:
    """Encode one JSON-RPC payload in LSP Content-Length framing."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def parse_headers(raw_header_bytes: bytes) -> dict[str, str]:
    """Parse CRLF-joined header lines, normalizing names to lowercase."""
    try:
        lines = raw_header_bytes.decode("ascii").split("\r\n")
    except UnicodeDecodeError as exc:
        raise MalformedLspFrameError(f"LSP headers are not ASCII: {exc}") from exc

    headers = {}
    for line in lines:
        if ": " not in line:
            raise MalformedLspFrameError(f"LSP header has no ': ' separator: {line[:256]!r}")
        name, value = line.split(": ", 1)
        headers[name.lower()] = value
    return headers


def content_length_from_headers(headers: dict[str, str]) -> int:
    """Return the required non-negative Content-Length header."""
    value = headers.get("content-length")
    if value is None or not value.isdigit():
        raise MalformedLspFrameError("LSP Content-Length must be a non-negative integer")
    return int(value)


def decode_body(body: bytes) -> dict:
    """Decode one UTF-8 JSON object body."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedLspFrameError(f"LSP frame body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MalformedLspFrameError(
            f"LSP frame body must decode to a JSON object, got {type(payload).__name__}"
        )
    return payload
