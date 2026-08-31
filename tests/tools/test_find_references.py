import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import StaleIndexGenerationError, SymbolNotFoundError
from acie.tools.find_references import find_references

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


def _stores_with_generation(n):
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    for _ in range(n):
        index_meta_store.bump_generation()
    return symbol_store, relation_store, index_meta_store


def test_find_references_by_symbol_id_returns_call_sites_terse():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller)
    relation_store.upsert(
        _relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4)
    )

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=callee.id,
    )

    assert envelope["index_generation"] == 1
    assert envelope["total_count"] == 1
    assert envelope["truncated"] is False
    result = envelope["results"][0]
    assert result == {
        "source": caller.id, "target": callee.id, "predicate": "calls",
        "site_file": "pkg/mod.py", "site_line": 2, "site_col": 4,
    }
    assert "confidence" not in result
    assert "provenance" not in result


def test_find_references_full_reveals_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=callee.id, full=True,
    )

    result = envelope["results"][0]
    assert result["confidence"] == "EXTRACTED"
    assert result["provenance"] == {
        "provider": "tree-sitter", "version": "0.25.0", "observed_at": "2026-08-31T00:00:00Z",
    }


def test_find_references_raises_symbol_not_found_for_unknown_symbol_id():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(SymbolNotFoundError):
        find_references(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:nope#function",
        )


def test_find_references_raises_symbol_not_found_for_tombstoned_symbol_id():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.delete("pkg/mod.py:foo#function", observed_at="2026-08-31T01:00:00Z")

    with pytest.raises(SymbolNotFoundError):
        find_references(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function",
        )


def test_find_references_raises_value_error_when_both_symbol_id_and_position_given():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(ValueError):
        find_references(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function",
            position={"file": "pkg/mod.py", "line": 1, "column": 0},
        )


def test_find_references_raises_value_error_when_neither_symbol_id_nor_position_given():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(ValueError):
        find_references(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        )


def test_find_references_by_position_on_the_definition_resolves_via_symbol_start():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5, col=0)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller)
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 5, "column": 0},
    )

    assert [r["source"] for r in envelope["results"]] == [caller.id]


def test_find_references_by_position_on_the_definition_includes_its_own_defines_relation():
    # Combines two behaviors that are each individually tested but never
    # together: resolving *by position* landing on a definition (via the
    # symbol-start fallback in resolve.py), and USAGE_PREDICATES including
    # defines. The declaration's own defines edge (container -> method)
    # should appear in the results alongside its calls.
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    container = _symbol("pkg/mod.py:Foo#class", "pkg/mod.py", "Foo", "class", line=1)
    method = _symbol("pkg/mod.py:Foo.bar#method", "pkg/mod.py", "Foo.bar", "method", line=2, col=4)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=10)
    symbol_store.upsert(container)
    symbol_store.upsert(method)
    symbol_store.upsert(caller)
    relation_store.upsert(_relation(container.id, method.id, "defines", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(caller.id, method.id, "calls", "pkg/mod.py", 11, 0))

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4},
    )

    assert [r["source"] for r in envelope["results"]] == [container.id, caller.id]
    assert envelope["total_count"] == 2


def test_find_references_by_position_on_a_call_site_resolves_to_the_same_symbols_other_references():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    caller_a = _symbol("pkg/mod.py:caller_a#function", "pkg/mod.py", "caller_a", "function", line=1)
    caller_b = _symbol("pkg/mod.py:caller_b#function", "pkg/mod.py", "caller_b", "function", line=8)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller_a)
    symbol_store.upsert(caller_b)
    relation_store.upsert(_relation(caller_a.id, callee.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(caller_b.id, callee.id, "calls", "pkg/mod.py", 9, 4))

    # Cursor sits directly on caller_a's own call-site to callee, not on
    # callee's definition -- find_references should still resolve "the
    # symbol referred to here" and list every reference to it, including
    # the site the cursor started on.
    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4},
    )

    assert {r["source"] for r in envelope["results"]} == {caller_a.id, caller_b.id}
    assert envelope["total_count"] == 2


def test_find_references_unions_references_across_an_ambiguous_positions_candidates():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    candidate_a = _symbol("pkg/a.py:dup#function", "pkg/a.py", "dup", "function", line=1)
    candidate_b = _symbol("pkg/b.py:dup#function", "pkg/b.py", "dup", "function", line=1)
    other_caller_of_a = _symbol("pkg/mod.py:other#function", "pkg/mod.py", "other", "function", line=10)
    symbol_store.upsert(caller)
    symbol_store.upsert(candidate_a)
    symbol_store.upsert(candidate_b)
    symbol_store.upsert(other_caller_of_a)
    # The ambiguous site itself, at (2, 4) -- what the cursor is on.
    relation_store.upsert(
        _relation(caller.id, candidate_a.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )
    relation_store.upsert(
        _relation(caller.id, candidate_b.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )
    # An unambiguous, unrelated call to candidate_a from elsewhere -- should
    # be included in the union since candidate_a is one of the resolved
    # candidates.
    relation_store.upsert(
        _relation(other_caller_of_a.id, candidate_a.id, "calls", "pkg/mod.py", 11, 4)
    )

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4},
    )

    assert envelope["total_count"] == 3
    assert {(r["source"], r["target"]) for r in envelope["results"]} == {
        (caller.id, candidate_a.id),
        (caller.id, candidate_b.id),
        (other_caller_of_a.id, candidate_a.id),
    }


def test_find_references_includes_a_defines_relation_targeting_the_symbol():
    # Locked with the user: find_references is IDE-style "find all usages",
    # which includes the symbol's own declaration site -- unlike
    # get_definition's resolution semantics (resolve.py), which excludes
    # defines because it's redundant there with the symbol-start fallback.
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    container = _symbol("pkg/mod.py:Foo#class", "pkg/mod.py", "Foo", "class", line=1)
    method = _symbol("pkg/mod.py:Foo.bar#method", "pkg/mod.py", "Foo.bar", "method", line=2)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=10)
    symbol_store.upsert(container)
    symbol_store.upsert(method)
    symbol_store.upsert(caller)
    relation_store.upsert(_relation(container.id, method.id, "defines", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(caller.id, method.id, "calls", "pkg/mod.py", 11, 0))

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=method.id,
    )

    assert [r["source"] for r in envelope["results"]] == [container.id, caller.id]
    assert envelope["total_count"] == 2


def test_find_references_by_position_raises_symbol_not_found_when_nothing_resolves():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(SymbolNotFoundError):
        find_references(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            position={"file": "pkg/mod.py", "line": 999, "column": 0},
        )


def test_find_references_returns_empty_results_when_symbol_has_no_references():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:lonely#function", "pkg/mod.py", "lonely", "function"))

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:lonely#function",
    )

    assert envelope["results"] == []
    assert envelope["total_count"] == 0
    assert envelope["truncated"] is False


def test_find_references_orders_by_site_file_line_col_predicate_source():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=1)
    caller_b = _symbol("pkg/b.py:caller#function", "pkg/b.py", "caller", "function", line=1)
    caller_a = _symbol("pkg/a.py:caller#function", "pkg/a.py", "caller", "function", line=1)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller_b)
    symbol_store.upsert(caller_a)
    # Later site (higher line) upserted first, to prove ordering is by the
    # key, not upsert/insertion order.
    relation_store.upsert(_relation(caller_b.id, callee.id, "calls", "pkg/z.py", 9, 0))
    relation_store.upsert(_relation(caller_a.id, callee.id, "calls", "pkg/a.py", 1, 0))

    envelope = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=callee.id,
    )

    assert [r["site_file"] for r in envelope["results"]] == ["pkg/a.py", "pkg/z.py"]


def test_find_references_paginates_via_next_cursor():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=1)
    caller_a = _symbol("pkg/a.py:caller#function", "pkg/a.py", "caller", "function", line=1)
    caller_b = _symbol("pkg/b.py:caller#function", "pkg/b.py", "caller", "function", line=1)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller_a)
    symbol_store.upsert(caller_b)
    relation_store.upsert(_relation(caller_a.id, callee.id, "calls", "pkg/a.py", 1, 0))
    relation_store.upsert(_relation(caller_b.id, callee.id, "calls", "pkg/b.py", 1, 0))

    page1 = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=callee.id, limit=1,
    )
    assert [r["site_file"] for r in page1["results"]] == ["pkg/a.py"]
    assert page1["total_count"] == 2
    assert page1["truncated"] is True
    assert page1["next_cursor"] is not None

    page2 = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=callee.id, limit=1, cursor=page1["next_cursor"],
    )
    assert [r["site_file"] for r in page2["results"]] == ["pkg/b.py"]
    assert page2["total_count"] == 2
    assert page2["truncated"] is False
    assert page2["next_cursor"] is None


def test_find_references_raises_stale_index_generation_when_generation_changed_since_the_cursor_was_issued():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=1)
    caller_a = _symbol("pkg/a.py:caller#function", "pkg/a.py", "caller", "function", line=1)
    caller_b = _symbol("pkg/b.py:caller#function", "pkg/b.py", "caller", "function", line=1)
    symbol_store.upsert(callee)
    symbol_store.upsert(caller_a)
    symbol_store.upsert(caller_b)
    relation_store.upsert(_relation(caller_a.id, callee.id, "calls", "pkg/a.py", 1, 0))
    relation_store.upsert(_relation(caller_b.id, callee.id, "calls", "pkg/b.py", 1, 0))

    page1 = find_references(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=callee.id, limit=1,
    )
    assert page1["truncated"] is True

    index_meta_store.bump_generation()  # simulates a reindex happening mid-pagination

    with pytest.raises(StaleIndexGenerationError):
        find_references(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id=callee.id, limit=1, cursor=page1["next_cursor"],
        )
