from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.tools.errors import StaleIndexGenerationError
from acie.tools.pagination import coerce_tuple_key, decode_cursor, filter_since, paginate
from acie.tools.render import render_relation

# Same local v0 default as the other flat-list tools -- not specified in
# ARCHITECTURE.md.
_DEFAULT_LIMIT = 50

# imports is the only predicate list_imports ever returns -- unlike
# find_references/get_definition, there's no symbol_id/position resolution
# here, just a direct scope-by-file query, so no shared resolve.py
# dependency (per ARCHITECTURE.md's "architecturally simpler" framing for
# this tool).
_IMPORT_PREDICATES = {"imports"}


def list_imports(
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    file: str,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
    full: bool = False,
) -> dict:
    index_generation = index_meta_store.current_generation()

    after_key = None
    if cursor is not None:
        cursor_generation, after_key = decode_cursor(cursor)
        if cursor_generation != index_generation:
            raise StaleIndexGenerationError(
                f"index_generation changed from {cursor_generation} to {index_generation} "
                "since this cursor was issued"
            )
        after_key = coerce_tuple_key(after_key)

    matches = relation_store.list_by_site_file(file, predicates=_IMPORT_PREDICATES)
    matches.sort(key=_ordering_key)

    remaining = filter_since(matches, after_key, cursor_key=_ordering_key)

    page, truncated, next_cursor = paginate(
        remaining, limit, index_generation, cursor_key=lambda r: list(_ordering_key(r))
    )

    return {
        "index_generation": index_generation,
        "results": [render_relation(relation, full=full) for relation in page],
        "total_count": len(matches),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def _ordering_key(relation) -> tuple:
    return (relation.site_line, relation.site_col, relation.target)
