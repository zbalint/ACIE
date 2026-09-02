"""explain: full multi-provider observation-history retrieval for a symbol
or edge.

See ARCHITECTURE.md "MCP Tool Surface": added specifically to answer
"explain this edge/symbol" -- shows a symbol's or relation's full
observation history. `results` is a flat, newest-first array of
self-contained observation snapshots, entry zero always the current live
fact. Uses real opaque keyset cursor pagination like the other flat-list
tools, but never raises STALE_INDEX_GENERATION -- its whole purpose is
showing history across index generations.

Two seams confirmed with the user before writing tests (AskUserQuestion,
both recommended options chosen):

1. **edge_ref is the full composite key**: {source_symbol_id,
   target_symbol_id, predicate, site_file, site_line, site_col} -- not the
   bare {source, target, predicate} triple ARCHITECTURE.md's signature
   line literally shows. Matches RelationStore's actual PK (multiple call
   sites between the same two symbols are distinct rows) and every other
   tool's already-addressable Relation shape (render_relation).
2. **A deleted (tombstoned) target still returns full history**, not
   SYMBOL_NOT_FOUND/EDGE_NOT_FOUND -- entry zero becomes the most recent
   content snapshot (history()'s last entry), tagged `deleted: true`. Only
   entry zero carries this key; older entries don't.

**Follow-up resolved post-shipment** (AskUserQuestion, recommended option
chosen): unlike every other tool, explain's `full` toggle does NOT gate
confidence/provenance -- those fields are always present, even at the
default `full=False`, because they're the substance of what explain exists
to show (how a fact's certainty/source changed across observations), not a
secondary annotation like they are for the other 7 tools. The `full`
parameter is kept in the signature for interface consistency with the rest
of the tool surface, but is currently inert here (nothing else in explain's
output is gated by it).

**Local decision, not asked about**: `_symbol_entries`/`_edge_entries` are
structurally parallel (live-or-tombstoned-fallback, then reversed history)
but operate on different domain objects (Symbol vs Relation) with
different not-found errors and ordering keys. Kept as two separate
functions rather than one generic callback-parameterized helper -- same
"don't force an abstraction from two superficially-similar branches"
judgment impact_analysis.py already made against graph.py's BFS. If a
real third shape shows up, unify then.
"""

from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import EdgeNotFoundError, InvalidArgumentError, SymbolNotFoundError
from acie.tools.pagination import coerce_tuple_key, decode_cursor, filter_since, paginate
from acie.tools.render import render_relation, render_symbol

_DEFAULT_LIMIT = 50


def explain(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    symbol_id: str | None = None,
    edge_ref: dict | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
    full: bool = False,
) -> dict:
    if (symbol_id is None) == (edge_ref is None):
        raise InvalidArgumentError("explain requires exactly one of symbol_id or edge_ref")

    index_generation = index_meta_store.current_generation()

    # full is accepted for interface consistency with the other 7 tools but
    # is inert here: explain always reveals confidence/provenance, since
    # they're explain's whole point rather than a secondary annotation.
    entries = (
        _symbol_entries(symbol_store, symbol_id)
        if symbol_id is not None
        else _edge_entries(relation_store, edge_ref)
    )

    after_key = None
    if cursor is not None:
        _cursor_generation, after_key = decode_cursor(cursor)
        after_key = coerce_tuple_key(after_key)

    entries.sort(key=lambda pair: pair[0], reverse=True)
    remaining = filter_since(entries, after_key, cursor_key=lambda entry: entry[0], reverse=True)

    page, truncated, next_cursor = paginate(
        remaining, limit, index_generation, cursor_key=lambda entry: list(entry[0])
    )

    return {
        "index_generation": index_generation,
        "results": [item for _key, item in page],
        "total_count": len(entries),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def _symbol_entries(symbol_store: SymbolStore, symbol_id: str) -> list[tuple[tuple, dict]]:
    live = symbol_store.get(symbol_id)
    history = symbol_store.history(symbol_id)

    if live is None and not history and not symbol_store.is_tombstoned(symbol_id):
        raise SymbolNotFoundError(f"no symbol with id {symbol_id!r} has ever been observed")

    entries: list[tuple[tuple, dict]] = []
    if live is not None:
        entries.append((_symbol_key(live), render_symbol(live, full=True)))
        history = history[:-1] if history else history
    else:
        # Tombstoned: no live row, so the most recent history entry stands
        # in for entry zero, tagged deleted.
        deleted_snapshot = history[-1]
        item = render_symbol(deleted_snapshot, full=True)
        item["deleted"] = True
        entries.append((_symbol_key(deleted_snapshot), item))
        history = history[:-1]

    for symbol in history:
        entries.append((_symbol_key(symbol), render_symbol(symbol, full=True)))

    return entries


def _edge_entries(relation_store: RelationStore, edge_ref: dict) -> list[tuple[tuple, dict]]:
    key = {
        "source": edge_ref["source_symbol_id"],
        "target": edge_ref["target_symbol_id"],
        "predicate": edge_ref["predicate"],
        "site_file": edge_ref["site_file"],
        "site_line": edge_ref["site_line"],
        "site_col": edge_ref["site_col"],
    }
    live = relation_store.get(**key)
    history = relation_store.history(**key)

    if live is None and not history and not relation_store.is_tombstoned(**key):
        raise EdgeNotFoundError(f"no edge matches edge_ref {edge_ref!r}")

    entries: list[tuple[tuple, dict]] = []
    if live is not None:
        entries.append((_relation_key(live), render_relation(live, full=True)))
        history = history[:-1] if history else history
    else:
        deleted_snapshot = history[-1]
        item = render_relation(deleted_snapshot, full=True)
        item["deleted"] = True
        entries.append((_relation_key(deleted_snapshot), item))
        history = history[:-1]

    for relation in history:
        entries.append((_relation_key(relation), render_relation(relation, full=True)))

    return entries


def _relation_key(relation) -> tuple:
    # Unlike _symbol_key, the composite lookup key (source/target/predicate/
    # site_*) is already fixed by edge_ref -- the only fields that can vary
    # between a relation's history entries are confidence/provenance (see
    # RelationStore._content_differs), so no site fields are needed here.
    return (
        relation.provenance.observed_at,
        relation.confidence.value,
        relation.provenance.provider,
        relation.provenance.version,
    )


def _symbol_key(symbol) -> tuple:
    # shortcut: composite-of-content-fields tiebreak instead of exposing
    # symbols_history's real history_id row-order column. Only collides if
    # two *distinct* historical snapshots for the same symbol_id are
    # byte-identical in every field (including observed_at) -- upgrade
    # trigger: expose history_id via SymbolStore.history() if that ever
    # produces an actually-observed ordering bug.
    return (
        symbol.provenance.observed_at,
        symbol.path,
        symbol.qualname,
        symbol.kind,
        symbol.start_line,
        symbol.start_col,
        symbol.end_line,
        symbol.end_col,
        symbol.confidence.value,
        symbol.provenance.provider,
        symbol.provenance.version,
    )
