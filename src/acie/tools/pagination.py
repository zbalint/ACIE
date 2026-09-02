"""Shared opaque keyset-cursor mechanics for the flat-list MCP tools.

Extracted from find_symbol once get_definition needed the identical
[index_generation, last_id] cursor encoding -- see ARCHITECTURE.md's
"Pagination" cross-cutting rule (8111bce9): the envelope/cursor scheme is
the real v0 design, not per-tool throwaway code, so a second caller reuses
this rather than growing its own copy.

`last_id` is deliberately typed as a bare JSON-serializable value, not
`str`: find_symbol/get_definition order by a symbol's own id (a string),
but find_references orders by a composite site key with no single string
identity -- (site_file, site_line, site_col, predicate, source). Passing
that key as a JSON list round-trips correctly through the same encode/
decode pair (see tests/tools/test_pagination.py), so callers with a
composite ordering key don't need a separate delimited-string encoding.
"""

import base64
import json
from typing import Any, Callable

from acie.tools.errors import InvalidCursorError, InvalidLimitError


def encode_cursor(generation: int, last_id: Any) -> str:
    payload = json.dumps([generation, last_id]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str) -> tuple[int, Any]:
    # LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): a malformed cursor used
    # to let raw base64/JSON/unpack exceptions escape here, which
    # dispatch.py's generic `except Exception` then turned into an
    # unhelpful INTERNAL_ERROR leaking decoder internals. (ValueError,
    # TypeError) covers binascii.Error, UnicodeDecodeError,
    # json.JSONDecodeError, and a wrong-shape unpack alike.
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii"))
        generation, last_id = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise InvalidCursorError(f"cursor is malformed: {exc}") from exc
    return generation, last_id


def validate_limit(limit: int) -> None:
    # LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): limit<=0 crashed every
    # cursor-bearing tool -- see paginate()'s docstring for why.
    if limit <= 0:
        raise InvalidLimitError(f"limit must be a positive integer, got {limit!r}")


def coerce_tuple_key(value: Any) -> tuple:
    """Coerces a decoded cursor's composite last_id into a tuple.

    Code-review finding (2026-09-02) on the limit/cursor hardening pass: a
    syntactically valid cursor (correct base64/JSON, right [generation,
    last_id] shape) can still carry a semantically wrong last_id -- e.g. a
    bare int/float where find_references/list_imports/structural_search/
    explain expect the composite-key list decode_cursor's own docstring
    describes. `tuple(value)` on a non-iterable value (an int, for example)
    raises a bare TypeError that used to escape as INTERNAL_ERROR -- caught
    here and re-raised as the same InvalidCursorError decode_cursor uses,
    since this is the same untrustworthy-client-input problem, just
    surfacing one step later.
    """
    try:
        return tuple(value)
    except TypeError as exc:
        raise InvalidCursorError(f"cursor is malformed: {exc}") from exc


def filter_since(
    items: list, after: Any, cursor_key: Callable[[Any], Any], *, reverse: bool = False
) -> list:
    """Keyset-filters `items` to those strictly past `after` in the caller's
    own ordering (or strictly before, for explain's newest-first order).

    Same code-review finding as coerce_tuple_key: comparing `cursor_key(item)`
    against a semantically wrong `after` (e.g. a string id against an int
    cursor value, the exact reviewer repro) raises a bare TypeError, caught
    here and re-raised as InvalidCursorError rather than escaping to
    dispatch.py's generic INTERNAL_ERROR handler.
    """
    if after is None:
        return items
    try:
        if reverse:
            return [item for item in items if cursor_key(item) < after]
        return [item for item in items if cursor_key(item) > after]
    except TypeError as exc:
        raise InvalidCursorError(f"cursor is malformed: {exc}") from exc


def paginate(
    remaining: list, limit: int, generation: int, cursor_key: Callable[[Any], Any]
) -> tuple[list, bool, str | None]:
    """Shared page/truncated/next_cursor construction for the six flat-list
    tools (find_symbol, get_definition, find_references, list_imports,
    structural_search, explain).

    Extracted because all six duplicated the exact same three-line block,
    which shared the exact same live-observed bug: at limit=0 a nonempty
    `remaining` is truncated but has an empty page, so `page[-1]` (used to
    build next_cursor) raised IndexError. Fixing this once here, rather
    than in six copies, follows this project's own "wait for the second
    caller" precedent that already justified extracting this module.

    `remaining` must already be filtered (past any cursor's `after` key)
    and sorted in the caller's own ordering -- callers differ on both of
    those (composite keys, reverse order for explain), which is why this
    helper only owns the truncation/cursor half, not the filtering half.
    """
    validate_limit(limit)
    page = remaining[:limit]
    truncated = len(remaining) > limit
    next_cursor = encode_cursor(generation, cursor_key(page[-1])) if truncated else None
    return page, truncated, next_cursor
