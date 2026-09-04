import json

import pytest

from acie.daemon.lsp_protocol import (
    MalformedLspFrameError,
    content_length_from_headers,
    decode_body,
    encode_frame,
    parse_headers,
)


def test_encode_frame_prepends_a_correct_content_length_header():
    payload = {"text": "héllo"}

    frame = encode_frame(payload)

    header, body = frame.split(b"\r\n\r\n", 1)
    assert header == f"Content-Length: {len(body)}".encode("ascii")
    assert body == json.dumps(payload, separators=(",", ":")).encode("utf-8")


def test_encode_frame_round_trips_through_parse_headers_and_decode_body():
    payload = {"jsonrpc": "2.0", "params": {"value": 1}}

    header, body = encode_frame(payload).split(b"\r\n\r\n", 1)

    assert content_length_from_headers(parse_headers(header)) == len(body)
    assert decode_body(body) == payload


def test_parse_headers_is_case_insensitive_for_content_length():
    headers = parse_headers(b"content-LENGTH: 42\r\nContent-Type: application/vscode-jsonrpc")

    assert headers == {"content-length": "42", "content-type": "application/vscode-jsonrpc"}


def test_parse_headers_raises_on_a_line_with_no_colon_separator():
    with pytest.raises(MalformedLspFrameError):
        parse_headers(b"Content-Length 42")


def test_content_length_from_headers_raises_when_the_header_is_missing():
    with pytest.raises(MalformedLspFrameError):
        content_length_from_headers({})


def test_content_length_from_headers_raises_on_a_non_integer_value():
    with pytest.raises(MalformedLspFrameError):
        content_length_from_headers({"content-length": "many"})


def test_decode_body_raises_on_invalid_utf8():
    with pytest.raises(MalformedLspFrameError):
        decode_body(b"\xff")


def test_decode_body_raises_on_invalid_json():
    with pytest.raises(MalformedLspFrameError):
        decode_body(b"not json")


def test_decode_body_raises_when_the_top_level_value_is_not_an_object():
    with pytest.raises(MalformedLspFrameError):
        decode_body(b"[]")
