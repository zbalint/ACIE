"""Shared Symbol/Relation item rendering for the MCP tools.

render_relation was extracted from find_references once list_imports
needed byte-identical item rendering -- "wait for the second caller"
precedent already followed twice before (resolve.py, pagination.py).
Confirmed with the user (AskUserQuestion, recommended option chosen):
list_imports uses the full Relation shape (source/target/predicate/
site_file/site_line/site_col, plus confidence/provenance under full=true),
not a trimmed import-specific shape, so this helper is exact reuse, not
independently-designed-but-similar logic.

render_symbol was extracted later, from find_symbol.py's and
get_definition.py's identical private `_render` -- graph.py is the 3rd
caller of this exact logic (the "wait for 2nd caller" rule was not
followed strictly here; the 2 pre-existing copies were only unified once
a 3rd real caller made the duplication unambiguous). Both call sites were
refactored to import from here instead of keeping their own copy;
confirmed behavior-preserving by running the full suite unchanged before
adding any graph.py code.
"""

from acie.ir.relation import Relation
from acie.ir.symbol import Symbol


def render_relation(relation: Relation, *, full: bool) -> dict:
    item = {
        "source": relation.source,
        "target": relation.target,
        "predicate": relation.predicate,
        "site_file": relation.site_file,
        "site_line": relation.site_line,
        "site_col": relation.site_col,
    }
    if full:
        item["confidence"] = relation.confidence.value
        item["provenance"] = {
            "provider": relation.provenance.provider,
            "version": relation.provenance.version,
            "observed_at": relation.provenance.observed_at,
        }
    return item


def render_symbol(symbol: Symbol, *, full: bool) -> dict:
    item = {
        "id": symbol.id,
        "path": symbol.path,
        "qualname": symbol.qualname,
        "kind": symbol.kind,
        "start_line": symbol.start_line,
        "start_col": symbol.start_col,
        "end_line": symbol.end_line,
        "end_col": symbol.end_col,
    }
    if full:
        item["confidence"] = symbol.confidence.value
        item["provenance"] = {
            "provider": symbol.provenance.provider,
            "version": symbol.provenance.version,
            "observed_at": symbol.provenance.observed_at,
        }
    return item
