import pytest

from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import InvalidArgumentError, InvalidCursorError, InvalidLimitError, StaleIndexGenerationError
from acie.tools.find_symbol import find_symbol
from acie.tools.pagination import encode_cursor

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z")


def _symbol(id_, path, qualname, kind, line=1, confidence=Confidence.EXTRACTED):
    return Symbol(
        id=id_, path=path, qualname=qualname, kind=kind,
        start_line=line, start_col=0, end_line=line + 1, end_col=8,
        confidence=confidence, provenance=_PROVENANCE,
    )


def _stores_with_generation(n):
    symbol_store = SymbolStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    for _ in range(n):
        index_meta_store.bump_generation()
    return symbol_store, index_meta_store


def test_find_symbol_matches_by_qualname_substring_and_terse_mode_hides_provenance():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.upsert(_symbol("pkg/mod.py:bar#function", "pkg/mod.py", "bar", "function"))

    envelope = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="fo")

    assert envelope["index_generation"] == 1
    assert envelope["total_count"] == 1
    assert envelope["truncated"] is False
    assert len(envelope["results"]) == 1
    result = envelope["results"][0]
    assert result["id"] == "pkg/mod.py:foo#function"
    assert result["qualname"] == "foo"
    assert result["kind"] == "function"
    assert "confidence" not in result
    assert "provenance" not in result


def test_find_symbol_full_reveals_confidence_and_provenance():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", full=True)

    result = envelope["results"][0]
    assert result["confidence"] == "EXTRACTED"
    assert result["provenance"] == {
        "provider": "tree-sitter", "version": "0.25.0", "observed_at": "2026-08-31T00:00:00Z",
    }


def test_find_symbol_passes_kind_and_path_glob_filters_through_to_the_store():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.upsert(_symbol("pkg/mod.py:foo#class", "pkg/mod.py", "foo", "class"))
    symbol_store.upsert(_symbol("other/mod.py:foo#function", "other/mod.py", "foo", "function"))

    by_kind = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", kind="class")
    assert [r["id"] for r in by_kind["results"]] == ["pkg/mod.py:foo#class"]

    by_path = find_symbol(
        symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", path_glob="pkg/*",
    )
    assert [r["id"] for r in by_path["results"]] == ["pkg/mod.py:foo#class", "pkg/mod.py:foo#function"]


def test_find_symbol_paginates_via_next_cursor_and_total_count_stays_stable_across_pages():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo_a#function", "pkg/mod.py", "foo_a", "function"))
    symbol_store.upsert(_symbol("pkg/mod.py:foo_b#function", "pkg/mod.py", "foo_b", "function"))
    symbol_store.upsert(_symbol("pkg/mod.py:foo_c#function", "pkg/mod.py", "foo_c", "function"))

    page1 = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", limit=2)

    assert [r["id"] for r in page1["results"]] == ["pkg/mod.py:foo_a#function", "pkg/mod.py:foo_b#function"]
    assert page1["total_count"] == 3
    assert page1["truncated"] is True
    assert page1["next_cursor"] is not None

    page2 = find_symbol(
        symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", limit=2,
        cursor=page1["next_cursor"],
    )

    assert [r["id"] for r in page2["results"]] == ["pkg/mod.py:foo_c#function"]
    assert page2["total_count"] == 3
    assert page2["truncated"] is False
    assert page2["next_cursor"] is None


def test_find_symbol_raises_stale_index_generation_when_generation_changed_since_the_cursor_was_issued():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo_a#function", "pkg/mod.py", "foo_a", "function"))
    symbol_store.upsert(_symbol("pkg/mod.py:foo_b#function", "pkg/mod.py", "foo_b", "function"))

    page1 = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", limit=1)
    assert page1["truncated"] is True

    index_meta_store.bump_generation()  # simulates a reindex happening mid-pagination

    with pytest.raises(StaleIndexGenerationError):
        find_symbol(
            symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", limit=1,
            cursor=page1["next_cursor"],
        )


def test_find_symbol_returns_empty_results_when_nothing_matches():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="zzz")

    assert envelope["results"] == []
    assert envelope["total_count"] == 0
    assert envelope["truncated"] is False


def test_find_symbol_raises_invalid_limit_for_a_non_positive_limit():
    # Regression for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): limit=0
    # used to crash with IndexError instead of a typed error.
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidLimitError):
        find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", limit=0)


def test_find_symbol_raises_invalid_cursor_for_a_semantically_wrong_last_id_type():
    # Code-review regression (2026-09-02): a syntactically valid cursor
    # encoding an int last_id (symbol ids are strings) used to crash with a
    # bare "'>' not supported between instances of 'str' and 'int'" instead
    # of a typed error.
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    bad_cursor = encode_cursor(1, 0)

    with pytest.raises(InvalidCursorError):
        find_symbol(symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", cursor=bad_cursor)


def test_find_symbol_min_confidence_excludes_less_certain_matches():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.upsert(
        _symbol("pkg/mod.py:foo#function@2", "pkg/mod.py", "foo", "function", confidence=Confidence.AMBIGUOUS)
    )

    envelope = find_symbol(
        symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", min_confidence="EXTRACTED",
    )

    assert envelope["total_count"] == 1
    assert envelope["results"][0]["id"] == "pkg/mod.py:foo#function"


def test_find_symbol_min_confidence_rejects_invalid_value():
    symbol_store, index_meta_store = _stores_with_generation(1)
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        find_symbol(
            symbol_store=symbol_store, index_meta_store=index_meta_store, name="foo", min_confidence="NOPE",
        )
