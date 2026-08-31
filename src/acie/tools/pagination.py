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
from typing import Any


def encode_cursor(generation: int, last_id: Any) -> str:
    payload = json.dumps([generation, last_id]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str) -> tuple[int, Any]:
    payload = base64.urlsafe_b64decode(cursor.encode("ascii"))
    generation, last_id = json.loads(payload)
    return generation, last_id
