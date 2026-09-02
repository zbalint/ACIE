import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, SymbolNotFoundError
from acie.tools.graph import graph

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


def test_graph_downstream_call_traverses_outbound_calls_one_hop():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=caller.id, graph_type="call", direction="downstream",
    )

    assert {n["id"] for n in envelope["nodes"]} == {caller.id, callee.id}
    assert envelope["edges"] == [
        {
            "source": caller.id, "target": callee.id, "predicate": "calls",
            "site_file": "pkg/mod.py", "site_line": 2, "site_col": 4,
        }
    ]
    assert envelope["truncated"] is False


def test_graph_upstream_call_traverses_inbound_calls_one_hop():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=callee.id, graph_type="call", direction="upstream",
    )

    assert {n["id"] for n in envelope["nodes"]} == {caller.id, callee.id}
    assert [e["source"] for e in envelope["edges"]] == [caller.id]


def test_graph_call_multi_hop_traverses_transitively_when_depth_clamp_allows():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    c = _symbol("pkg/mod.py:c#function", "pkg/mod.py", "c", "function", line=9)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    symbol_store.upsert(c)
    relation_store.upsert(_relation(a.id, b.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(b.id, c.id, "calls", "pkg/mod.py", 6, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, graph_type="call", direction="downstream", depth_clamp=5,
    )

    assert {n["id"] for n in envelope["nodes"]} == {a.id, b.id, c.id}
    assert envelope["truncated"] is False


def test_graph_depth_clamp_stops_traversal_and_reports_truncated_when_more_exists():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    c = _symbol("pkg/mod.py:c#function", "pkg/mod.py", "c", "function", line=9)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    symbol_store.upsert(c)
    relation_store.upsert(_relation(a.id, b.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(b.id, c.id, "calls", "pkg/mod.py", 6, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, graph_type="call", direction="downstream", depth_clamp=1,
    )

    assert {n["id"] for n in envelope["nodes"]} == {a.id, b.id}
    assert envelope["truncated"] is True


def test_graph_depth_clamp_reached_exactly_at_the_true_frontier_is_not_falsely_truncated():
    # depth_clamp=1 stops right where the graph actually ends (b has no
    # further outbound calls) -- must not report truncated=True just
    # because depth_clamp happened to be the number of hops taken.
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    relation_store.upsert(_relation(a.id, b.id, "calls", "pkg/mod.py", 2, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, graph_type="call", direction="downstream", depth_clamp=1,
    )

    assert {n["id"] for n in envelope["nodes"]} == {a.id, b.id}
    assert envelope["truncated"] is False


def test_graph_node_cap_truncates_and_reports_truncated_true():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    c = _symbol("pkg/mod.py:c#function", "pkg/mod.py", "c", "function", line=9)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    symbol_store.upsert(c)
    relation_store.upsert(_relation(a.id, b.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(a.id, c.id, "calls", "pkg/mod.py", 3, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, graph_type="call", direction="downstream", node_cap=2,
    )

    assert len(envelope["nodes"]) == 2
    assert envelope["truncated"] is True
    assert envelope["node_cap"] == 2


def test_graph_node_cap_selection_is_deterministic_by_edge_ordering_key():
    # relations_live's PRIMARY KEY is (source, target, predicate, site_*),
    # so an unordered SQLite scan for a fixed source naturally comes back
    # in ascending *target* order -- a weaker test that just picks two
    # arbitrary target ids can pass by accident even with the explicit
    # `sorted(..., key=_edge_ordering_key)` call removed, if target order
    # happens to already agree with site_line order. This test deliberately
    # makes them disagree: the earlier call site (site_line=2, which must
    # win under _edge_ordering_key) is given the lexically-LATER target id
    # ("zz"), and the later call site (site_line=9) the lexically-EARLIER
    # target id ("aa") -- so a version relying on unordered target-order
    # iteration would keep the wrong (aa, site_line=9) node instead.
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    early_site_target = _symbol("pkg/mod.py:zz#function", "pkg/mod.py", "zz", "function", line=20)
    late_site_target = _symbol("pkg/mod.py:aa#function", "pkg/mod.py", "aa", "function", line=2)
    symbol_store.upsert(a)
    symbol_store.upsert(early_site_target)
    symbol_store.upsert(late_site_target)
    relation_store.upsert(_relation(a.id, early_site_target.id, "calls", "pkg/mod.py", 2, 0))
    relation_store.upsert(_relation(a.id, late_site_target.id, "calls", "pkg/mod.py", 9, 0))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, graph_type="call", direction="downstream", node_cap=2,
    )

    assert {n["id"] for n in envelope["nodes"]} == {a.id, early_site_target.id}
    assert envelope["truncated"] is True


def test_graph_handles_a_two_node_call_cycle_without_infinite_loop():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    b = _symbol("pkg/mod.py:b#function", "pkg/mod.py", "b", "function", line=5)
    symbol_store.upsert(a)
    symbol_store.upsert(b)
    relation_store.upsert(_relation(a.id, b.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(b.id, a.id, "calls", "pkg/mod.py", 6, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, graph_type="call", direction="downstream", depth_clamp=5,
    )

    assert {n["id"] for n in envelope["nodes"]} == {a.id, b.id}
    assert len(envelope["edges"]) == 2
    assert envelope["truncated"] is False


def test_graph_dependency_downstream_renders_unresolved_import_target_as_leaf_node():
    symbol_store, relation_store, index_meta_store = _stores()
    module = _symbol("pkg/mod.py:#module", "pkg/mod.py", "", "module", line=1)
    symbol_store.upsert(module)
    relation_store.upsert(_relation(module.id, "os.path", "imports", "pkg/mod.py", 1, 0))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=module.id, graph_type="dependency", direction="downstream",
    )

    nodes_by_id = {n["id"]: n for n in envelope["nodes"]}
    assert nodes_by_id["os.path"] == {"id": "os.path", "resolved": False}
    assert nodes_by_id[module.id]["resolved"] is True


def test_graph_call_graph_type_ignores_imports_edges():
    symbol_store, relation_store, index_meta_store = _stores()
    module = _symbol("pkg/mod.py:#module", "pkg/mod.py", "", "module", line=1)
    symbol_store.upsert(module)
    relation_store.upsert(_relation(module.id, "os.path", "imports", "pkg/mod.py", 1, 0))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=module.id, graph_type="call", direction="downstream",
    )

    assert envelope["nodes"] == [{
        "id": module.id, "path": "pkg/mod.py", "qualname": "", "kind": "module",
        "start_line": 1, "start_col": 0, "end_line": 2, "end_col": 8, "resolved": True,
    }]
    assert envelope["edges"] == []


def test_graph_full_reveals_confidence_and_provenance_on_edges_and_resolved_nodes():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=caller.id, graph_type="call", direction="downstream", full=True,
    )

    edge = envelope["edges"][0]
    assert edge["confidence"] == "EXTRACTED"
    assert edge["provenance"]["provider"] == "tree-sitter"
    callee_node = next(n for n in envelope["nodes"] if n["id"] == callee.id)
    assert callee_node["confidence"] == "EXTRACTED"


def test_graph_terse_by_default_hides_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=caller.id, graph_type="call", direction="downstream",
    )

    edge = envelope["edges"][0]
    assert "confidence" not in edge
    assert "provenance" not in edge


def test_graph_raises_symbol_not_found_for_unknown_root():
    symbol_store, relation_store, index_meta_store = _stores()

    with pytest.raises(SymbolNotFoundError):
        graph(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:nope#function", graph_type="call", direction="downstream",
        )


def test_graph_raises_symbol_not_found_for_tombstoned_root():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.delete("pkg/mod.py:foo#function", observed_at="2026-08-31T01:00:00Z")

    with pytest.raises(SymbolNotFoundError):
        graph(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", graph_type="call", direction="downstream",
        )


def test_graph_raises_invalid_argument_for_invalid_graph_type():
    # Regression for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): this used
    # to raise a bare ValueError, which dispatch.py's generic exception
    # handler then demoted to an unhelpful INTERNAL_ERROR.
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        graph(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", graph_type="bogus", direction="downstream",
        )


def test_graph_raises_invalid_argument_for_invalid_direction():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        graph(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", graph_type="call", direction="sideways",
        )


def test_graph_raises_invalid_argument_for_non_positive_node_cap():
    # Regression for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): node_cap
    # <= 0 used to still return the root node, contradicting the cap.
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        graph(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", graph_type="call", direction="downstream", node_cap=0,
        )


def test_graph_raises_invalid_argument_for_non_positive_depth_clamp():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        graph(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", graph_type="call", direction="downstream", depth_clamp=-1,
        )


def test_graph_root_with_no_edges_returns_a_single_node_graph():
    symbol_store, relation_store, index_meta_store = _stores()
    lonely = _symbol("pkg/mod.py:lonely#function", "pkg/mod.py", "lonely", "function")
    symbol_store.upsert(lonely)

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=lonely.id, graph_type="call", direction="downstream",
    )

    assert envelope["nodes"] == [{
        "id": lonely.id, "path": "pkg/mod.py", "qualname": "lonely", "kind": "function",
        "start_line": 1, "start_col": 0, "end_line": 2, "end_col": 8, "resolved": True,
    }]
    assert envelope["edges"] == []
    assert envelope["truncated"] is False


def test_graph_dedups_multiple_call_sites_between_the_same_two_symbols_into_one_node_but_multiple_edges():
    symbol_store, relation_store, index_meta_store = _stores()
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 3, 4))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=caller.id, graph_type="call", direction="downstream",
    )

    assert {n["id"] for n in envelope["nodes"]} == {caller.id, callee.id}
    assert len(envelope["edges"]) == 2


def test_graph_reports_index_generation():
    symbol_store, relation_store, index_meta_store = _stores()
    index_meta_store.bump_generation()
    index_meta_store.bump_generation()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = graph(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root="pkg/mod.py:foo#function", graph_type="call", direction="downstream",
    )

    assert envelope["index_generation"] == 2
