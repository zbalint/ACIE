from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, StaleIndexGenerationError
from acie.tools.pagination import decode_cursor, filter_since, paginate
from acie.tools.render import render_symbol
from acie.tools.resolve import resolve_symbol_or_position

# Same local v0 default as find_symbol -- not specified in ARCHITECTURE.md.
_DEFAULT_LIMIT = 50


def get_definition(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    symbol_id: str | None = None,
    position: dict | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
    full: bool = False,
) -> dict:
    if (symbol_id is None) == (position is None):
        raise InvalidArgumentError("get_definition requires exactly one of symbol_id or position")

    index_generation = index_meta_store.current_generation()

    after_id = None
    if cursor is not None:
        cursor_generation, after_id = decode_cursor(cursor)
        if cursor_generation != index_generation:
            raise StaleIndexGenerationError(
                f"index_generation changed from {cursor_generation} to {index_generation} "
                "since this cursor was issued"
            )

    matches = resolve_symbol_or_position(
        symbol_store, relation_store, symbol_id=symbol_id, position=position
    )
    matches.sort(key=lambda s: s.id)
    remaining = filter_since(matches, after_id, cursor_key=lambda s: s.id)

    page, truncated, next_cursor = paginate(remaining, limit, index_generation, cursor_key=lambda s: s.id)

    return {
        "index_generation": index_generation,
        "results": [render_symbol(symbol, full=full) for symbol in page],
        "total_count": len(matches),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }
