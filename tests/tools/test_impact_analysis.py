import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, SymbolNotFoundError
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
    # `overrides` joined the followed predicate set in slice A4 (wayfinder
    # ticket 732f8b2d's resolution: {"calls", "imports", "overrides"}) --
    # `inherits` deliberately did NOT, same reasoning graph.py already
    # applies to it (already reachable via find_references/get_definition).
    # This test now exercises all three still-excluded predicates, not just
    # `references`, so the overrides/inherits boundary stays covered.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    referencer = _symbol("pkg/mod.py:referencer#function", "pkg/mod.py", "referencer", "function", line=5)
    inheriting_class = _symbol("pkg/mod.py:Sub#class", "pkg/mod.py", "Sub", "class", line=9)
    defining_module = _symbol("pkg/mod.py:#module", "pkg/mod.py", "", "module", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(referencer)
    symbol_store.upsert(inheriting_class)
    symbol_store.upsert(defining_module)
    relation_store.upsert(_relation(referencer.id, root.id, "references", "pkg/mod.py", 6, 4))
    relation_store.upsert(_relation(inheriting_class.id, root.id, "inherits", "pkg/mod.py", 9, 6))
    relation_store.upsert(_relation(defining_module.id, root.id, "defines", "pkg/mod.py", 1, 0))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_symbols"] == []


def test_impact_analysis_traverses_overrides_predicate_finding_subclass_override_as_affected():
    symbol_store, relation_store, index_meta_store = _stores()
    base_method = _symbol("pkg/base.py:Base.foo#method", "pkg/base.py", "Base.foo", "method", line=2)
    override_method = _symbol("pkg/sub.py:Sub.foo#method", "pkg/sub.py", "Sub.foo", "method", line=3)
    symbol_store.upsert(base_method)
    symbol_store.upsert(override_method)
    relation_store.upsert(_relation(override_method.id, base_method.id, "overrides", "pkg/sub.py", 3, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=base_method.id,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {override_method.id}


def test_impact_analysis_multi_level_override_chain_transitively_discovered_via_bfs():
    # `overrides` points at the immediate base only (per extract_relations.py's
    # own docstring: "multi-level chains fall out for free via BFS hopping
    # through the edges") -- no special-cased chain-walking needed, the
    # existing generic BFS already handles it once overrides is followed.
    symbol_store, relation_store, index_meta_store = _stores()
    grand = _symbol("pkg/a.py:GrandBase.foo#method", "pkg/a.py", "GrandBase.foo", "method", line=1)
    base = _symbol("pkg/b.py:Base.foo#method", "pkg/b.py", "Base.foo", "method", line=1)
    sub = _symbol("pkg/c.py:Sub.foo#method", "pkg/c.py", "Sub.foo", "method", line=1)
    symbol_store.upsert(grand)
    symbol_store.upsert(base)
    symbol_store.upsert(sub)
    relation_store.upsert(_relation(base.id, grand.id, "overrides", "pkg/b.py", 1, 4))
    relation_store.upsert(_relation(sub.id, base.id, "overrides", "pkg/c.py", 1, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=grand.id, depth_clamp=5,
    )

    assert {s["id"] for s in envelope["affected_symbols"]} == {base.id, sub.id}


def test_impact_analysis_affected_symbols_carry_discovery_predicate_field():
    # New per-node field (wayfinder resolution 732f8b2d): impact_analysis
    # has no `edges` list at all, so without this field there is no way to
    # tell from `affected_symbols` alone whether a node was reached via
    # calls/imports/overrides. Unconditional (not full-gated) -- unlike
    # confidence/provenance (the SYMBOL's own metadata), this is traversal
    # metadata, structurally more like `impact_summary`'s always-shown
    # confidence-tier breakdown than like render_symbol's full-only fields.
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

    assert envelope["affected_symbols"][0]["discovery_predicate"] == "calls"


def test_impact_analysis_discovery_predicate_distinguishes_overrides_from_calls():
    symbol_store, relation_store, index_meta_store = _stores()
    base_method = _symbol("pkg/base.py:Base.foo#method", "pkg/base.py", "Base.foo", "method", line=2)
    override_method = _symbol("pkg/sub.py:Sub.foo#method", "pkg/sub.py", "Sub.foo", "method", line=3)
    symbol_store.upsert(base_method)
    symbol_store.upsert(override_method)
    relation_store.upsert(_relation(override_method.id, base_method.id, "overrides", "pkg/sub.py", 3, 4))

    envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=base_method.id, full=True,
    )

    assert envelope["affected_symbols"][0]["discovery_predicate"] == "overrides"


def test_impact_analysis_same_subclass_overriding_two_different_bases_are_independent_edges_not_conflated():
    # Closes the open question in memory b8fed631 (A3's self-critique):
    # A3 deliberately gives independently-EXTRACTED confidence to a
    # same-file-base override and a cross-file-base override on the same
    # subclass method, rather than one true joint-MRO-ambiguous edge.
    # Verified here that this can never cause impact_analysis to double-
    # count or mis-rank a single root's blast radius: `overrides` points at
    # the immediate BASE method, so the two edges have two DIFFERENT
    # targets. Querying either base is a fully independent traversal that
    # only ever sees its own edge -- there is no query shape where both
    # edges collide into one impact_summary tally. Not a correctness bug;
    # no fix needed.
    symbol_store, relation_store, index_meta_store = _stores()
    same_file_base = _symbol("pkg/mod.py:SameFileBase.bar#method", "pkg/mod.py", "SameFileBase.bar", "method", line=2)
    cross_file_base = _symbol("pkg/other.py:CrossFileBase.bar#method", "pkg/other.py", "CrossFileBase.bar", "method", line=2)
    overriding_method = _symbol("pkg/mod.py:Foo.bar#method", "pkg/mod.py", "Foo.bar", "method", line=9)
    symbol_store.upsert(same_file_base)
    symbol_store.upsert(cross_file_base)
    symbol_store.upsert(overriding_method)
    relation_store.upsert(_relation(overriding_method.id, same_file_base.id, "overrides", "pkg/mod.py", 9, 4))
    relation_store.upsert(_relation(overriding_method.id, cross_file_base.id, "overrides", "pkg/mod.py", 9, 4))

    same_file_envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=same_file_base.id,
    )
    cross_file_envelope = impact_analysis(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=cross_file_base.id,
    )

    assert {s["id"] for s in same_file_envelope["affected_symbols"]} == {overriding_method.id}
    assert same_file_envelope["impact_summary"] == {"total": 1, "EXTRACTED": 1, "INFERRED": 0, "AMBIGUOUS": 0}
    assert {s["id"] for s in cross_file_envelope["affected_symbols"]} == {overriding_method.id}
    assert cross_file_envelope["impact_summary"] == {"total": 1, "EXTRACTED": 1, "INFERRED": 0, "AMBIGUOUS": 0}


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
        {"id": "pkg/mod.py:ghost#function", "resolved": False, "discovery_predicate": "calls"}
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


def test_impact_analysis_raises_invalid_argument_for_non_positive_node_cap():
    # Regression for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): node_cap
    # <= 0 used to still return the root node, contradicting the cap.
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        impact_analysis(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", node_cap=0,
        )


def test_impact_analysis_raises_invalid_argument_for_non_positive_depth_clamp():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        impact_analysis(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", depth_clamp=-1,
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
