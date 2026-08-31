"""Shared symbol_id/position resolution for MCP tools that anchor on a
symbol -- get_definition and find_references.

Extracted from get_definition once find_references needed byte-identical
resolution semantics -- see ARCHITECTURE.md "MCP Tool Surface":
find_references is documented as having "the same mutually-exclusive
shape as get_definition". Confirmed with the user (AskUserQuestion,
recommended option chosen) that this is exact reuse via a shared helper,
not independently-designed-but-similar logic, mirroring the precedent
already set for pagination.py's cursor mechanics.
"""

from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import SymbolNotFoundError

# Relation predicates whose site is a genuine "reference to a symbol"
# resolvable to a target. imports is excluded: its target is a raw
# dotted-name string, not a symbol_id (an external import has no
# ACIE-tracked definition). defines is excluded: it's a containment
# relation, not a reference site, and its site already equals the target
# symbol's own start position -- covered by the symbol-start fallback
# below, not worth a redundant relation-site match.
REFERENCE_PREDICATES = {"calls", "references", "inherits"}


def resolve_symbol_or_position(
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    *,
    symbol_id: str | None,
    position: dict | None,
) -> list:
    """Resolves symbol_id or position to a list of live Symbols.

    symbol_id resolves to itself (a single-element list), or raises
    SymbolNotFoundError if it doesn't name a live symbol.

    position resolves via exact-point matching, in two ordered steps:
      1. a relations_live row whose (site_file, site_line, site_col)
         exactly equals position, restricted to REFERENCE_PREDICATES --
         every matching candidate target is returned (an AMBIGUOUS
         multi-target site yields multiple results).
      2. fallback: a symbol whose own (path, start_line, start_col)
         exactly equals position.
      Raises SymbolNotFoundError if neither step matches.
    """
    if symbol_id is not None:
        symbol = symbol_store.get(symbol_id)
        if symbol is None:
            raise SymbolNotFoundError(f"no live symbol with id {symbol_id!r}")
        return [symbol]

    path, line, column = position["file"], position["line"], position["column"]

    reference_sites = relation_store.list_by_site(
        site_file=path, site_line=line, site_col=column, predicates=REFERENCE_PREDICATES
    )
    if reference_sites:
        targets = []
        for relation in reference_sites:
            target = symbol_store.get(relation.target)
            if target is not None:
                targets.append(target)
        if targets:
            return targets

    at_start = symbol_store.at_start(path=path, line=line, col=column)
    if at_start is not None:
        return [at_start]

    raise SymbolNotFoundError(f"no definition resolves at {path}:{line}:{column}")
