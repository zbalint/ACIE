import json
import struct

import pytest

from acie.daemon.protocol import (
    LENGTH_PREFIX_SIZE,
    MAX_MESSAGE_BYTES,
    MalformedFrameError,
    build_error_response,
    build_request,
    build_success_response,
    decode_frame_body,
    decode_length_prefix,
    encode_frame,
)


def test_encode_frame_roundtrips_through_decode_length_prefix_and_decode_frame_body():
    payload = {"id": "abc-123", "token": None, "method": "find_symbol", "repo_path": "/repo", "params": {"name": "foo"}}

    frame = encode_frame(payload)

    prefix, body = frame[:LENGTH_PREFIX_SIZE], frame[LENGTH_PREFIX_SIZE:]
    length = decode_length_prefix(prefix)
    assert length == len(body)
    assert decode_frame_body(body) == payload


def test_encode_frame_prefix_is_four_byte_big_endian_length():
    payload = {"a": 1}
    body = json.dumps(payload).encode("utf-8")

    frame = encode_frame(payload)

    assert frame[:LENGTH_PREFIX_SIZE] == struct.pack(">I", len(body))
    assert frame[LENGTH_PREFIX_SIZE:] == body


def test_encode_frame_rejects_payload_exceeding_max_message_bytes():
    oversized = {"data": "x" * (MAX_MESSAGE_BYTES + 1)}

    with pytest.raises(MalformedFrameError):
        encode_frame(oversized)


def test_decode_length_prefix_rejects_wrong_size_prefix():
    with pytest.raises(MalformedFrameError):
        decode_length_prefix(b"\x00\x00\x01")


def test_decode_length_prefix_rejects_declared_length_exceeding_cap():
    oversized_prefix = struct.pack(">I", MAX_MESSAGE_BYTES + 1)

    with pytest.raises(MalformedFrameError):
        decode_length_prefix(oversized_prefix)


def test_decode_length_prefix_accepts_declared_length_at_exactly_the_cap():
    boundary_prefix = struct.pack(">I", MAX_MESSAGE_BYTES)

    assert decode_length_prefix(boundary_prefix) == MAX_MESSAGE_BYTES


def test_decode_frame_body_rejects_invalid_json():
    with pytest.raises(MalformedFrameError):
        decode_frame_body(b"{not json")


def test_decode_frame_body_rejects_non_dict_json():
    with pytest.raises(MalformedFrameError):
        decode_frame_body(b"[1, 2, 3]")


def test_decode_frame_body_rejects_invalid_utf8():
    with pytest.raises(MalformedFrameError):
        decode_frame_body(b"\xff\xfe\x00")


def test_build_request_includes_uuid4_id_method_repo_path_params_and_null_token_by_default():
    request = build_request(method="find_symbol", repo_path="/repo", params={"name": "foo"})

    assert request["method"] == "find_symbol"
    assert request["repo_path"] == "/repo"
    assert request["params"] == {"name": "foo"}
    assert request["token"] is None
    # id is a real uuid4, not a placeholder -- two calls never collide.
    other = build_request(method="find_symbol", repo_path="/repo", params={})
    assert request["id"] != other["id"]
    assert len(request["id"]) == 36


def test_build_request_accepts_explicit_token():
    request = build_request(method="graph", repo_path="/repo", params={}, token="secret")

    assert request["token"] == "secret"


def test_build_success_response_wraps_result_under_matching_id():
    response = build_success_response("abc-123", {"results": [1, 2, 3]})

    assert response == {"id": "abc-123", "ok": True, "result": {"results": [1, 2, 3]}}


def test_build_error_response_carries_code_and_message_under_matching_id():
    response = build_error_response("abc-123", "UNKNOWN_METHOD", "no such method: bogus")

    assert response == {
        "id": "abc-123",
        "ok": False,
        "error": {"code": "UNKNOWN_METHOD", "message": "no such method: bogus"},
    }
