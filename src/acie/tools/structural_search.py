"""Live tree-sitter structural search over caller-supplied source text.

Unlike find_symbol/get_definition/find_references/list_imports, this tool
never touches SymbolStore/RelationStore -- ACIE's IR deliberately stores no
source code text (see ARCHITECTURE.md's Canonical IR section), so there is
nothing in the index a `.scm` query could run against. Instead,
structural_search takes an explicit `files` mapping (path -> source_text)
as its own input, mirroring the same "explicit source_text parameter, disk
I/O deferred to future daemon wiring" pattern already used by
extract_symbols/extract_relations/index_file. Confirmed with the user
(AskUserQuestion, all four recommended options chosen):

1. Source text arrives via an explicit `files` param, not a disk read.
2. Results are grouped one item per whole pattern match (QueryCursor.
   matches()'s own {capture_name: [Node, ...]} shape), not flattened to one
   item per capture -- this preserves which captures co-occurred in the
   same match, which is what a `.scm` query is actually expressing. Each
   capture location also carries the matched source snippet as `text`;
   this is ephemeral query output derived from the caller-supplied text,
   not persisted IR state, so it doesn't conflict with "no source text in
   the IR".
3. Ordering key is (path, anchor_line, anchor_col, pattern_index) -- the
   anchor is the earliest (start_line, start_col) among all of a match's
   captured nodes, and pattern_index is the tiebreak when two matches (from
   different alternative patterns in one compound query string) land on
   the exact same anchor.
4. The function takes an explicit `observed_at` param (no wall-clock
   reads) to stamp every match's provenance deterministically, same as
   extract_symbols/extract_relations/index_file. Every match is inherently
   EXTRACTED confidence -- a structural match is never ambiguous the way a
   name-resolution relation can be.
"""

import fnmatch
from importlib.metadata import version

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Query, QueryCursor, QueryError

from acie.ir.symbol import Confidence, Provenance
from acie.storage.index_meta_store import IndexMetaStore
from acie.tools.errors import InvalidPatternError, StaleIndexGenerationError
from acie.tools.pagination import coerce_tuple_key, decode_cursor, filter_since, paginate

# Same duplication-not-yet-extracted precedent as extract_symbols.py /
# extract_relations.py, which each already define their own copy of this
# exact pair -- a third copy here doesn't meet the "wait for the second
# caller" bar that justified extracting render.py/pagination.py, since
# there's no actual decision/logic being duplicated, just a stdlib lookup.
_LANGUAGE = Language(tspython.language())
_PROVENANCE_VERSION = version("tree-sitter-python")

# Same local v0 default as the other flat-list tools -- not specified in
# ARCHITECTURE.md.
_DEFAULT_LIMIT = 50


def structural_search(
    files: dict[str, str],
    index_meta_store: IndexMetaStore,
    pattern: str,
    observed_at: str,
    path_glob: str | None = None,
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

    try:
        query = Query(_LANGUAGE, pattern)
    except QueryError as exc:
        raise InvalidPatternError(str(exc)) from exc

    provenance = Provenance(provider="tree-sitter", version=_PROVENANCE_VERSION, observed_at=observed_at)
    parser = Parser(_LANGUAGE)
    query_cursor = QueryCursor(query)

    matches = []
    for path, source_text in files.items():
        if path_glob is not None and not fnmatch.fnmatchcase(path, path_glob):
            continue
        tree = parser.parse(source_text.encode("utf-8"))
        for pattern_index, captures in query_cursor.matches(tree.root_node):
            match = _build_match(
                path=path, pattern_index=pattern_index, captures=captures, source_text=source_text
            )
            if match is not None:
                matches.append(match)

    matches.sort(key=_ordering_key)

    remaining = filter_since(matches, after_key, cursor_key=_ordering_key)

    page, truncated, next_cursor = paginate(
        remaining, limit, index_generation, cursor_key=lambda m: list(_ordering_key(m))
    )

    return {
        "index_generation": index_generation,
        "results": [_render(m, full=full, provenance=provenance) for m in page],
        "total_count": len(matches),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def _build_match(*, path: str, pattern_index: int, captures: dict, source_text: str) -> dict | None:
    """None when the match has zero captures -- a pattern with no `@name`
    at all produces a match through QueryCursor.matches(), but there is no
    node ACIE can derive a location from in that case, so it's skipped
    rather than crashing on an undefined anchor. Local decision, not part
    of the confirmed seam questions -- flagged in the completion memory.
    """
    if not captures:
        return None

    source_bytes = source_text.encode("utf-8")
    rendered_captures: dict[str, list[dict]] = {}
    anchor = None
    for name, nodes in captures.items():
        locations = []
        for node in nodes:
            start_line, start_col = node.start_point.row + 1, node.start_point.column
            if anchor is None or (start_line, start_col) < anchor:
                anchor = (start_line, start_col)
            locations.append(
                {
                    "start_line": start_line,
                    "start_col": start_col,
                    "end_line": node.end_point.row + 1,
                    "end_col": node.end_point.column,
                    "text": source_bytes[node.start_byte : node.end_byte].decode("utf-8"),
                }
            )
        rendered_captures[name] = locations

    return {
        "path": path,
        "pattern_index": pattern_index,
        "captures": rendered_captures,
        "anchor": anchor,
    }


def _ordering_key(match: dict) -> tuple:
    anchor_line, anchor_col = match["anchor"]
    return (match["path"], anchor_line, anchor_col, match["pattern_index"])


def _render(match: dict, *, full: bool, provenance: Provenance) -> dict:
    item = {
        "path": match["path"],
        "pattern_index": match["pattern_index"],
        "captures": match["captures"],
    }
    if full:
        item["confidence"] = Confidence.EXTRACTED.value
        item["provenance"] = {
            "provider": provenance.provider,
            "version": provenance.version,
            "observed_at": provenance.observed_at,
        }
    return item
