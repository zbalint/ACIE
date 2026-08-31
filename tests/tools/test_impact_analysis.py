import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import SymbolNotFoundError
from acie.tools.impact_analysis import impact_analysis

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z")


def _symbol(id_, path, qualname, kind, line=1, col=0):
    return Symbol(
        id=id_, path=path, qualname=qualname, kind=kind,
        start_line=line, start_col=col, end_line=line + 1, end_col=8,
        confidence=Confidence.EXTRACTED, provenance=_PROVENANCE,
    )


def _relation(source, target, predicate, site_file, site_line, site_col, confidence=Confidence.EXTRACTED):
    return Relation(
        source=source, target=target, predicate=predicate,
        site_file=site_file, site_line=site_line, site_col=site_col,
        confidence=confidence, provenance=_PROVENANCE,
    )


def _stores():
    return SymbolStore(":memory:"), RelationStore(":memory:"), IndexMetaStore(":memory:")


def test_impact_analysis_upstream_call_one_hop_lists_caller_as_affected():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=callee.id,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {caller.id}
    assert envelope["root"] == callee.id
    assert envelope["truncated"] is False


def test_impact_analysis_root_excluded_from_affected_symbols():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=callee.id,
    )

    assert callee.id not in {s["id"] for s in envelope["affected_symbols"]}


def test_impact_analysis_traverses_both_calls_and_imports_predicates():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    caller = _symbol("pkg/caller.py:caller#function", "pkg/caller.py", "caller", "function", line=1)
    importer_module = _symbol("pkg/importer.py:#module", "pkg/importer.py", "", "module", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(caller)
    symbol_store.upsert(importer_module)
    relation_store.upsert(_relation(caller.id, root.id, "calls", "pkg/caller.py", 2, 4))
    # Manually constructed 'imports' edge whose target happens to resolve to
    # a real symbol -- extract_relations never emits this shape today (see
    # graph.py's dependency-graph limitation), but impact_analysis's own
    # traversal must still generically honor the predicate set regardless.
    relation_store.upsert(_relation(importer_module.id, root.id, "imports", "pkg/importer.py", 1, 0))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {caller.id, importer_module.id}


def test_impact_analysis_excludes_references_inherits_defines_predicates():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    referencer = _symbol("pkg/mod.py:referencer#function", "pkg/mod.py", "referencer", "function", line=5)
    symbol_store.upsert(root)
    symbol_store.upsert(referencer)
    relation_store.upsert(_relation(referencer.id, root.id, "references", "pkg/mod.py", 6, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_symbols"] == []


def test_impact_analysis_multi_hop_transitive_when_depth_clamp_allows():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    c = _symbol("pkg/mod.py:c#function", "pkg/mod.py", "c", "function", line=9)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    symbol_store.upsert(c)
    relation_store.upsert(_relation(b.id, a.id, "calls", "pkg/mod.py", 6, 4))
    relation_store.upsert(_relation(c.id, b.id, "calls", "pkg/mod.py", 10, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, depth_clamp=5,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {b.id, c.id}
    assert envelope["truncated"] is False


def test_impact_analysis_depth_clamp_stops_traversal_and_reports_truncated():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    c = _symbol("pkg/mod.py:c#function", "pkg/mod.py", "c", "function", line=9)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    symbol_store.upsert(c)
    relation_store.upsert(_relation(b.id, a.id, "calls", "pkg/mod.py", 6, 4))
    relation_store.upsert(_relation(c.id, b.id, "calls", "pkg/mod.py", 10, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, depth_clamp=1,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {b.id}
    assert envelope["truncated"] is True


def test_impact_analysis_depth_clamp_reached_exactly_at_true_frontier_is_not_falsely_truncated():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    relation_store.upsert(_relation(b.id, a.id, "calls", "pkg/mod.py", 6, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, depth_clamp=1,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {b.id}
    assert envelope["truncated"] is False


def test_impact_analysis_node_cap_truncates_and_reports_truncated_true():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    c = _symbol("pkg/mod.py:c#function", "pkg/mod.py", "c", "function", line=9)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    symbol_store.upsert(c)
    relation_store.upsert(_relation(b.id, a.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(c.id, a.id, "calls", "pkg/mod.py", 3, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, node_cap=2,
    )

    # node_cap=2 counts root as 1 of the cap, leaving room for exactly 1
    # affected symbol.
    assert len(envelope["affected_symbols"]) == 1
    assert envelope["truncated"] is True
    assert envelope["node_cap"] == 2


def test_impact_analysis_node_cap_selection_is_deterministic_by_edge_ordering_key():
    # Same adversarial-ordering precedent as graph.py's analogous test
    # (423baebd): relations_live has no rowid ordering guarantee tied to
    # site_line, so without an explicit sort, an unordered scan for a fixed
    # target naturally returns rows in *insertion* order -- a weak version
    # of this test that happens to insert rows in already-site_line-order
    # would pass even with the sort removed. This test deliberately
    # inserts the LATER call site (site_line=9) first and the EARLIER one
    # (site_line=2, which must win under node_cap=2) second, so insertion
    # order and the intended site_line order disagree.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    early_site_caller = _symbol("pkg/mod.py:zz#function", "pkg/mod.py", "zz", "function", line=20)
    late_site_caller = _symbol("pkg/mod.py:aa#function", "pkg/mod.py", "aa", "function", line=2)
    symbol_store.upsert(root)
    symbol_store.upsert(early_site_caller)
    symbol_store.upsert(late_site_caller)
    relation_store.upsert(_relation(late_site_caller.id, root.id, "calls", "pkg/mod.py", 9, 0))
    relation_store.upsert(_relation(early_site_caller.id, root.id, "calls", "pkg/mod.py", 2, 0))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id, node_cap=2,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {early_site_caller.id}
    assert envelope["truncated"] is True


def test_impact_analysis_handles_a_two_node_call_cycle_without_infinite_loop():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    relation_store.upsert(_relation(b.id, a.id, "calls", "pkg/mod.py", 6, 4))
    relation_store.upsert(_relation(a.id, b.id, "calls", "pkg/mod.py", 2, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, depth_clamp=5,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {b.id}
    assert envelope["truncated"] is False


def test_impact_analysis_renders_orphaned_relation_source_as_unresolved_leaf():
    # A relation whose source symbol isn't (or is no longer) in symbol_store
    # -- defensive path, mirrors graph.py's unresolved-leaf handling even
    # though today's extractors don't produce orphaned call sources.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    symbol_store.upsert(root)
    relation_store.upsert(_relation("pkg/mod.py:ghost#function", root.id, "calls", "pkg/mod.py", 2, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_symbols"] == [
        {"id": "pkg/mod.py:ghost#function", "resolved": False}
    ]


def test_impact_analysis_full_reveals_confidence_and_provenance_on_affected_symbols():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=callee.id, full=True,
    )

    affected = envelope["affected_symbols"][0]
    assert affected["confidence"] == "EXTRACTED"
    assert affected["provenance"]["provider"] == "tree-sitter"


def test_impact_analysis_terse_by_default_hides_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=callee.id,
    )

    affected = envelope["affected_symbols"][0]
    assert "confidence" not in affected
    assert "provenance" not in affected


def test_impact_analysis_summary_breaks_out_counts_by_confidence_tier():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    extracted_caller = _symbol("pkg/mod.py:extracted_caller#function", "pkg/mod.py", "extracted_caller", "function", line=5)
    ambiguous_caller = _symbol("pkg/mod.py:ambiguous_caller#function", "pkg/mod.py", "ambiguous_caller", "function", line=9)
    symbol_store.upsert(root)
    symbol_store.upsert(extracted_caller)
    symbol_store.upsert(ambiguous_caller)
    relation_store.upsert(_relation(extracted_caller.id, root.id, "calls", "pkg/mod.py", 6, 4, confidence=Confidence.EXTRACTED))
    relation_store.upsert(_relation(ambiguous_caller.id, root.id, "calls", "pkg/mod.py", 10, 4, confidence=Confidence.AMBIGUOUS))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["impact_summary"] == {
        "total": 2, "EXTRACTED": 1, "INFERRED": 0, "AMBIGUOUS": 1,
    }


def test_impact_analysis_summary_is_zeroed_when_root_has_no_affected_symbols():
    symbol_store, relation_store, index_meta_store = _stores()
    lonely = _symbol("pkg/mod.py:lonely#function", "pkg/mod.py", "lonely", "function")
    symbol_store.upsert(lonely)

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=lonely.id,
    )

    assert envelope["affected_symbols"] == []
    assert envelope["impact_summary"] == {"total": 0, "EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
    assert envelope["truncated"] is False


def test_impact_analysis_raises_symbol_not_found_for_unknown_root():
    symbol_store, relation_store, index_meta_store = _stores()

    with pytest.raises(SymbolNotFoundError):
        impact_analysis(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:nope#function",
        )


def test_impact_analysis_raises_symbol_not_found_for_tombstoned_root():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.delete("pkg/mod.py:foo#function", observed_at="2026-08-31T01:00:00Z")

    with pytest.raises(SymbolNotFoundError):
        impact_analysis(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function",
        )


def test_impact_analysis_reports_index_generation():
    symbol_store, relation_store, index_meta_store = _stores()
    index_meta_store.bump_generation()
    index_meta_store.bump_generation()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root="pkg/mod.py:foo#function",
    )

    assert envelope["index_generation"] == 2
