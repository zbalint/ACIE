import base64

import pytest

from acie.tools.errors import InvalidCursorError, InvalidLimitError
from acie.tools.pagination import coerce_tuple_key, decode_cursor, encode_cursor, filter_since, paginate, validate_limit


def test_round_trips_a_string_last_key():
    cursor = encode_cursor(3, "pkg/mod.py:foo#function")

    assert decode_cursor(cursor) == (3, "pkg/mod.py:foo#function")


def test_round_trips_a_composite_list_last_key():
    # find_references' ordering key has no single string identity -- it's
    # the (site_file, site_line, site_col, predicate, source) tuple. JSON
    # already round-trips a list faithfully; this pins that as a supported,
    # tested contract rather than an incidental side effect of json.dumps.
    last_key = ["pkg/mod.py", 2, 4, "calls", "pkg/mod.py:caller#function"]

    cursor = encode_cursor(5, last_key)

    assert decode_cursor(cursor) == (5, last_key)


# Regression coverage for the live-observed gap (LIVE_MCP_QUALIFICATION_
# REPORT.md, 2026-09-01): decode_cursor used to let raw base64/JSON/shape
# exceptions escape, which dispatch.py's generic `except Exception` then
# turned into an unhelpful INTERNAL_ERROR leaking decoder internals. It must
# raise ACIE's own typed error instead.
def test_decode_cursor_rejects_invalid_base64():
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-valid-base64!!!")


def test_decode_cursor_rejects_base64_that_is_not_json():
    garbage_json = base64_of("not json")

    with pytest.raises(InvalidCursorError):
        decode_cursor(garbage_json)


def test_decode_cursor_rejects_wrong_shape_payload():
    # Valid base64 and valid JSON, but not the [generation, last_id] pair
    # decode_cursor's unpack assumes.
    wrong_shape = base64_of("[1, 2, 3]")

    with pytest.raises(InvalidCursorError):
        decode_cursor(wrong_shape)


def base64_of(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


# Regression coverage for the live-observed gap: limit=0 crashed every
# cursor-bearing tool with `IndexError: list index out of range` because a
# nonempty result set was still "truncated" but page[-1] didn't exist.
# Negative limits sliced inconsistently. Both must now be rejected up front.
def test_validate_limit_rejects_zero():
    with pytest.raises(InvalidLimitError):
        validate_limit(0)


def test_validate_limit_rejects_negative():
    with pytest.raises(InvalidLimitError):
        validate_limit(-1)


def test_validate_limit_accepts_positive():
    validate_limit(1)  # must not raise


def test_paginate_rejects_non_positive_limit_before_touching_remaining():
    with pytest.raises(InvalidLimitError):
        paginate(["a", "b"], 0, generation=1, cursor_key=lambda x: x)


def test_paginate_returns_full_page_untruncated_when_limit_covers_everything():
    page, truncated, next_cursor = paginate(["a", "b"], 5, generation=1, cursor_key=lambda x: x)

    assert page == ["a", "b"]
    assert truncated is False
    assert next_cursor is None


def test_paginate_truncates_and_builds_a_cursor_from_the_last_page_item():
    page, truncated, next_cursor = paginate(["a", "b", "c"], 2, generation=7, cursor_key=lambda x: x)

    assert page == ["a", "b"]
    assert truncated is True
    assert decode_cursor(next_cursor) == (7, "b")


# Regression coverage for a codex code-review finding (2026-09-02) on the
# limit/cursor hardening pass: decode_cursor only catches a *structurally*
# malformed cursor (bad base64/JSON/shape). A syntactically valid cursor can
# still carry a *semantically* wrong last_id -- e.g. an integer where a
# string symbol id (find_symbol/get_definition) or a composite list
# (find_references/list_imports/structural_search/explain) is expected.
# Reproduced end-to-end: encode_cursor(1, 0) fed to find_symbol raised a bare
# "TypeError: '>' not supported between instances of 'str' and 'int'" that
# escaped as INTERNAL_ERROR. filter_since/coerce_tuple_key close this by
# catching the TypeError a wrong-typed cursor value provokes, wherever it's
# provoked, and re-raising ACIE's own InvalidCursorError -- same seam,
# same code, just caught one step later than decode_cursor's own checks.
def test_filter_since_passes_through_unfiltered_when_after_is_none():
    assert filter_since(["a", "b"], None, cursor_key=lambda x: x) == ["a", "b"]


def test_filter_since_keeps_items_strictly_after_the_cursor_key():
    assert filter_since(["a", "b", "c"], "a", cursor_key=lambda x: x) == ["b", "c"]


def test_filter_since_reverse_keeps_items_strictly_before_the_cursor_key():
    assert filter_since(["c", "b", "a"], "b", cursor_key=lambda x: x, reverse=True) == ["a"]


def test_filter_since_raises_invalid_cursor_when_comparison_types_mismatch():
    # The exact reviewer repro: a string symbol id compared against an int
    # cursor value.
    with pytest.raises(InvalidCursorError):
        filter_since(["pkg/mod.py:foo#function"], 0, cursor_key=lambda x: x)


def test_coerce_tuple_key_passes_through_a_list_as_a_tuple():
    assert coerce_tuple_key(["pkg/mod.py", 2, 4]) == ("pkg/mod.py", 2, 4)


def test_coerce_tuple_key_raises_invalid_cursor_for_a_non_iterable_value():
    with pytest.raises(InvalidCursorError):
        coerce_tuple_key(0)
