import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import (
    InvalidArgumentError,
    InvalidCursorError,
    InvalidLimitError,
    StaleIndexGenerationError,
    SymbolNotFoundError,
)
from acie.tools.get_definition import get_definition
from acie.tools.pagination import encode_cursor

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z")


def _symbol(id_, path, qualname, kind, line=1, col=0, confidence=Confidence.EXTRACTED):
    return Symbol(
        id=id_, path=path, qualname=qualname, kind=kind,
        start_line=line, start_col=col, end_line=line + 1, end_col=8,
        confidence=confidence, provenance=_PROVENANCE,
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


def test_get_definition_by_symbol_id_returns_the_symbol_itself_terse():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:foo#function",
    )

    assert envelope["index_generation"] == 1
    assert envelope["total_count"] == 1
    assert envelope["truncated"] is False
    result = envelope["results"][0]
    assert result["id"] == "pkg/mod.py:foo#function"
    assert "confidence" not in result
    assert "provenance" not in result


def test_get_definition_full_reveals_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:foo#function", full=True,
    )

    result = envelope["results"][0]
    assert result["confidence"] == "EXTRACTED"
    assert result["provenance"] == {
        "provider": "tree-sitter", "version": "0.25.0", "observed_at": "2026-08-31T00:00:00Z",
    }


def test_get_definition_raises_symbol_not_found_for_unknown_symbol_id():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(SymbolNotFoundError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:nope#function",
        )


def test_get_definition_raises_symbol_not_found_for_tombstoned_symbol_id():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.delete("pkg/mod.py:foo#function", observed_at="2026-08-31T01:00:00Z")

    with pytest.raises(SymbolNotFoundError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function",
        )


def test_get_definition_raises_invalid_argument_when_both_symbol_id_and_position_given():
    # Regression for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): this used
    # to raise a bare ValueError, which dispatch.py's generic exception
    # handler then demoted to an unhelpful INTERNAL_ERROR.
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(InvalidArgumentError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function",
            position={"file": "pkg/mod.py", "line": 1, "column": 0},
        )


def test_get_definition_raises_invalid_argument_when_neither_symbol_id_nor_position_given():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(InvalidArgumentError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        )


def test_get_definition_raises_invalid_limit_for_a_non_positive_limit():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidLimitError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function", limit=0,
        )


def test_get_definition_raises_invalid_cursor_for_a_semantically_wrong_last_id_type():
    # Code-review regression (2026-09-02): see test_find_symbol's equivalent.
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    bad_cursor = encode_cursor(1, 0)

    with pytest.raises(InvalidCursorError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function", cursor=bad_cursor,
        )


def test_get_definition_by_position_resolves_via_a_call_relation_site():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(callee)
    relation_store.upsert(
        _relation(caller.id, callee.id, "calls", site_file="pkg/mod.py", site_line=2, site_col=4)
    )

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4},
    )

    assert [r["id"] for r in envelope["results"]] == ["pkg/mod.py:callee#function"]
    assert envelope["total_count"] == 1


def test_get_definition_by_position_returns_multiple_candidates_for_an_ambiguous_site():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    candidate_a = _symbol("pkg/a.py:dup#function", "pkg/a.py", "dup", "function", line=1)
    candidate_b = _symbol("pkg/b.py:dup#function", "pkg/b.py", "dup", "function", line=1)
    symbol_store.upsert(caller)
    symbol_store.upsert(candidate_a)
    symbol_store.upsert(candidate_b)
    relation_store.upsert(
        _relation(caller.id, candidate_a.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )
    relation_store.upsert(
        _relation(caller.id, candidate_b.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4},
    )

    assert {r["id"] for r in envelope["results"]} == {"pkg/a.py:dup#function", "pkg/b.py:dup#function"}
    assert envelope["total_count"] == 2


def test_get_definition_min_confidence_excludes_a_less_certain_candidate():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    candidate_a = _symbol("pkg/a.py:dup#function", "pkg/a.py", "dup", "function", line=1)
    candidate_b = _symbol(
        "pkg/b.py:dup#function", "pkg/b.py", "dup", "function", line=1, confidence=Confidence.AMBIGUOUS
    )
    symbol_store.upsert(caller)
    symbol_store.upsert(candidate_a)
    symbol_store.upsert(candidate_b)
    relation_store.upsert(_relation(caller.id, candidate_a.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS))
    relation_store.upsert(_relation(caller.id, candidate_b.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS))

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4}, min_confidence="EXTRACTED",
    )

    assert envelope["total_count"] == 1
    assert envelope["results"][0]["id"] == "pkg/a.py:dup#function"


def test_get_definition_min_confidence_rejects_invalid_value():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:foo#function", min_confidence="NOPE",
        )


def test_get_definition_by_position_falls_back_to_the_symbols_own_start_when_no_relation_site_matches():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1, col=0))

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 1, "column": 0},
    )

    assert [r["id"] for r in envelope["results"]] == ["pkg/mod.py:foo#function"]


def test_get_definition_by_position_ignores_an_imports_relation_site_at_that_point():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    # The import statement sits at line 3 -- deliberately not the same
    # position as any symbol's own start, so the only way this test could
    # pass is if the imports relation is correctly excluded from step 1 AND
    # the step-2 symbol-start fallback correctly finds nothing there either.
    module = _symbol("pkg/mod.py:#module", "pkg/mod.py", "", "module", line=1, col=0)
    symbol_store.upsert(module)
    relation_store.upsert(
        _relation(module.id, "os.path", "imports", "pkg/mod.py", 3, 0)
    )

    with pytest.raises(SymbolNotFoundError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            position={"file": "pkg/mod.py", "line": 3, "column": 0},
        )


def test_get_definition_by_position_excludes_a_defines_relation_at_a_shared_site():
    # Contrived collision (extract_relations never actually produces this --
    # a def site and a call site never share coordinates) to force a real
    # behavioral difference: only "calls" is a reference-predicate, so if
    # "defines" leaked into step 1's candidate set, this would return both
    # container_target and callee instead of just callee.
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    container_target = _symbol("pkg/mod.py:Foo#class", "pkg/mod.py", "Foo", "class", line=2)
    callee = _symbol("pkg/mod.py:callee#function", "pkg/mod.py", "callee", "function", line=5)
    symbol_store.upsert(caller)
    symbol_store.upsert(container_target)
    symbol_store.upsert(callee)
    relation_store.upsert(_relation(caller.id, container_target.id, "defines", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(caller.id, callee.id, "calls", "pkg/mod.py", 2, 4))

    envelope = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4},
    )

    assert [r["id"] for r in envelope["results"]] == ["pkg/mod.py:callee#function"]


def test_get_definition_by_position_raises_symbol_not_found_when_nothing_resolves():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)

    with pytest.raises(SymbolNotFoundError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            position={"file": "pkg/mod.py", "line": 999, "column": 0},
        )


def test_get_definition_paginates_ambiguous_candidates_via_next_cursor():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    candidate_a = _symbol("pkg/a.py:dup#function", "pkg/a.py", "dup", "function", line=1)
    candidate_b = _symbol("pkg/b.py:dup#function", "pkg/b.py", "dup", "function", line=1)
    symbol_store.upsert(caller)
    symbol_store.upsert(candidate_a)
    symbol_store.upsert(candidate_b)
    relation_store.upsert(
        _relation(caller.id, candidate_a.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )
    relation_store.upsert(
        _relation(caller.id, candidate_b.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )

    page1 = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4}, limit=1,
    )
    assert [r["id"] for r in page1["results"]] == ["pkg/a.py:dup#function"]
    assert page1["total_count"] == 2
    assert page1["truncated"] is True
    assert page1["next_cursor"] is not None

    page2 = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4}, limit=1, cursor=page1["next_cursor"],
    )
    assert [r["id"] for r in page2["results"]] == ["pkg/b.py:dup#function"]
    assert page2["total_count"] == 2
    assert page2["truncated"] is False
    assert page2["next_cursor"] is None


def test_get_definition_raises_stale_index_generation_when_generation_changed_since_the_cursor_was_issued():
    symbol_store, relation_store, index_meta_store = _stores_with_generation(1)
    caller = _symbol("pkg/mod.py:caller#function", "pkg/mod.py", "caller", "function", line=1)
    candidate_a = _symbol("pkg/a.py:dup#function", "pkg/a.py", "dup", "function", line=1)
    candidate_b = _symbol("pkg/b.py:dup#function", "pkg/b.py", "dup", "function", line=1)
    symbol_store.upsert(caller)
    symbol_store.upsert(candidate_a)
    symbol_store.upsert(candidate_b)
    relation_store.upsert(
        _relation(caller.id, candidate_a.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )
    relation_store.upsert(
        _relation(caller.id, candidate_b.id, "calls", "pkg/mod.py", 2, 4, confidence=Confidence.AMBIGUOUS)
    )

    page1 = get_definition(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        position={"file": "pkg/mod.py", "line": 2, "column": 4}, limit=1,
    )
    assert page1["truncated"] is True

    index_meta_store.bump_generation()  # simulates a reindex happening mid-pagination

    with pytest.raises(StaleIndexGenerationError):
        get_definition(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            position={"file": "pkg/mod.py", "line": 2, "column": 4}, limit=1,
            cursor=page1["next_cursor"],
        )
