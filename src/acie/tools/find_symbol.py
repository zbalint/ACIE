from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.confidence import filter_by_min_confidence
from acie.tools.errors import StaleIndexGenerationError
from acie.tools.pagination import decode_cursor, filter_since, paginate
from acie.tools.render import render_symbol

# Not specified in ARCHITECTURE.md's MCP Tool Surface section -- a local v0
# implementation decision, same status as symbol_id's now-removed local
# kind validation was before slice 2 settled where that seam belongs.
_DEFAULT_LIMIT = 50


def find_symbol(
    symbol_store: SymbolStore,
    index_meta_store: IndexMetaStore,
    name: str,
    kind: str | None = None,
    path_glob: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
    full: bool = False,
    min_confidence: str | None = None,
) -> dict:
    index_generation = index_meta_store.current_generation()

    after_id = None
    if cursor is not None:
        cursor_generation, after_id = decode_cursor(cursor)
        if cursor_generation != index_generation:
            raise StaleIndexGenerationError(
                f"index_generation changed from {cursor_generation} to {index_generation} "
                "since this cursor was issued"
            )

    matches = symbol_store.search(qualname_substring=name, kind=kind, path_glob=path_glob)
    matches = filter_by_min_confidence(matches, min_confidence)
    remaining = filter_since(matches, after_id, cursor_key=lambda s: s.id)

    page, truncated, next_cursor = paginate(remaining, limit, index_generation, cursor_key=lambda s: s.id)

    return {
        "index_generation": index_generation,
        "results": [render_symbol(symbol, full=full) for symbol in page],
        "total_count": len(matches),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }
