from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.confidence import filter_by_min_confidence
from acie.tools.errors import InvalidArgumentError, StaleIndexGenerationError
from acie.tools.pagination import coerce_tuple_key, decode_cursor, filter_since, paginate
from acie.tools.render import render_relation
from acie.tools.resolve import resolve_symbol_or_position

# Same local v0 default as find_symbol/get_definition -- not specified in
# ARCHITECTURE.md.
_DEFAULT_LIMIT = 50

# Deliberately its own set, not resolve.py's REFERENCE_PREDICATES -- that
# constant governs position->symbol *resolution* (shared with
# get_definition), a different question from "what counts as a usage in
# find_references' own results". Confirmed with the user: find_references
# is IDE-style "find all usages", which includes the symbol's own
# declaration site, so `defines` is included here even though resolve.py
# still excludes it from resolution (where it's redundant with the
# symbol-start fallback). `overrides` joins the set for the same reason as
# `inherits`: a subclass method overriding a base method is a usage of that
# base method (agy/gemini code-review finding, approved by the user,
# 2026-09-02).
USAGE_PREDICATES = {"calls", "references", "inherits", "defines", "overrides"}


def find_references(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    symbol_id: str | None = None,
    position: dict | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
    full: bool = False,
    min_confidence: str | None = None,
) -> dict:
    if (symbol_id is None) == (position is None):
        raise InvalidArgumentError("find_references requires exactly one of symbol_id or position")

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

    # resolve_symbol_or_position may return multiple candidate symbols for
    # an AMBIGUOUS position -- union the reference sites of every
    # candidate, same as get_definition unions its candidate definitions.
    candidates = resolve_symbol_or_position(
        symbol_store, relation_store, symbol_id=symbol_id, position=position
    )
    matches = []
    for candidate in candidates:
        matches.extend(
            relation_store.list_by_target(candidate.id, predicates=USAGE_PREDICATES)
        )
    matches = filter_by_min_confidence(matches, min_confidence)
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
    return (relation.site_file, relation.site_line, relation.site_col, relation.predicate, relation.source)
