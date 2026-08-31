import pytest

from acie.storage.index_meta_store import IndexMetaStore
from acie.tools.errors import InvalidPatternError, StaleIndexGenerationError
from acie.tools.structural_search import structural_search

_OBSERVED_AT = "2026-08-31T00:00:00Z"

_TWO_FUNCTIONS = "def foo():\n    pass\n\n\ndef bar():\n    pass\n"


def _index_meta_store_with_generation(n):
    index_meta_store = IndexMetaStore(":memory:")
    for _ in range(n):
        index_meta_store.bump_generation()
    return index_meta_store


def test_structural_search_returns_one_item_per_match_terse():
    index_meta_store = _index_meta_store_with_generation(1)
    files = {"pkg/mod.py": _TWO_FUNCTIONS}

    envelope = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT,
    )

    assert envelope["index_generation"] == 1
    assert envelope["total_count"] == 2
    assert envelope["truncated"] is False
    result = envelope["results"][0]
    assert result["path"] == "pkg/mod.py"
    assert result["pattern_index"] == 0
    assert result["captures"] == {
        "func.name": [
            {"start_line": 1, "start_col": 4, "end_line": 1, "end_col": 7, "text": "foo"},
        ]
    }
    assert "confidence" not in result
    assert "provenance" not in result


def test_structural_search_full_reveals_confidence_and_provenance():
    index_meta_store = _index_meta_store_with_generation(1)
    files = {"pkg/mod.py": _TWO_FUNCTIONS}

    envelope = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT, full=True,
    )

    result = envelope["results"][0]
    assert result["confidence"] == "EXTRACTED"
    assert result["provenance"] == {
        "provider": "tree-sitter", "version": result["provenance"]["version"], "observed_at": _OBSERVED_AT,
    }
    # pinned separately so a version-string typo can't silently satisfy the
    # assertion above via self-reference
    from importlib.metadata import version
    assert result["provenance"]["version"] == version("tree-sitter-python")


def test_structural_search_filters_by_path_glob():
    index_meta_store = _index_meta_store_with_generation(1)
    files = {
        "pkg/mod.py": _TWO_FUNCTIONS,
        "pkg/other.py": "def baz():\n    pass\n",
    }

    envelope = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT, path_glob="pkg/other.py",
    )

    assert envelope["total_count"] == 1
    assert envelope["results"][0]["captures"]["func.name"][0]["text"] == "baz"


def test_structural_search_raises_invalid_pattern_for_a_malformed_query():
    index_meta_store = _index_meta_store_with_generation(1)

    with pytest.raises(InvalidPatternError):
        structural_search(
            files={"pkg/mod.py": _TWO_FUNCTIONS}, index_meta_store=index_meta_store,
            pattern="(this is not a valid query",
            observed_at=_OBSERVED_AT,
        )


def test_structural_search_returns_empty_when_no_matches():
    index_meta_store = _index_meta_store_with_generation(1)

    envelope = structural_search(
        files={"pkg/mod.py": _TWO_FUNCTIONS}, index_meta_store=index_meta_store,
        pattern="(class_definition name: (identifier) @class.name)",
        observed_at=_OBSERVED_AT,
    )

    assert envelope["results"] == []
    assert envelope["total_count"] == 0
    assert envelope["truncated"] is False


def test_structural_search_orders_by_path_then_start_line_then_start_col():
    index_meta_store = _index_meta_store_with_generation(1)
    # Inserted in reverse path order, and z.py's own match sits before
    # a.py's earlier line -- proves ordering is by the (path, line, col)
    # key, not by dict/file insertion order or match-discovery order.
    files = {
        "pkg/z.py": "def zebra():\n    pass\n",
        "pkg/a.py": "def ant():\n    pass\n",
    }

    envelope = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT,
    )

    names = [r["captures"]["func.name"][0]["text"] for r in envelope["results"]]
    assert names == ["ant", "zebra"]


def test_structural_search_orders_by_pattern_index_as_final_tiebreak_on_a_shared_anchor():
    index_meta_store = _index_meta_store_with_generation(1)
    files = {"pkg/mod.py": "def foo():\n    pass\n"}
    # Both top-level patterns match the exact same function_definition node
    # at the exact same anchor position -- pattern_index must break the tie
    # deterministically, same load-bearing-tiebreak precedent as
    # list_imports' target field.
    pattern = "(function_definition) @b\n(function_definition) @a"

    envelope = structural_search(
        files=files, index_meta_store=index_meta_store, pattern=pattern, observed_at=_OBSERVED_AT,
    )

    assert [r["pattern_index"] for r in envelope["results"]] == [0, 1]
    assert [list(r["captures"].keys())[0] for r in envelope["results"]] == ["b", "a"]


def test_structural_search_paginates_via_next_cursor():
    index_meta_store = _index_meta_store_with_generation(1)
    files = {"pkg/mod.py": _TWO_FUNCTIONS}

    page1 = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT, limit=1,
    )
    assert [r["captures"]["func.name"][0]["text"] for r in page1["results"]] == ["foo"]
    assert page1["total_count"] == 2
    assert page1["truncated"] is True
    assert page1["next_cursor"] is not None

    page2 = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT, limit=1, cursor=page1["next_cursor"],
    )
    assert [r["captures"]["func.name"][0]["text"] for r in page2["results"]] == ["bar"]
    assert page2["total_count"] == 2
    assert page2["truncated"] is False
    assert page2["next_cursor"] is None


def test_structural_search_raises_stale_index_generation_when_generation_changed_since_the_cursor_was_issued():
    index_meta_store = _index_meta_store_with_generation(1)
    files = {"pkg/mod.py": _TWO_FUNCTIONS}

    page1 = structural_search(
        files=files, index_meta_store=index_meta_store,
        pattern="(function_definition name: (identifier) @func.name)",
        observed_at=_OBSERVED_AT, limit=1,
    )
    assert page1["truncated"] is True

    index_meta_store.bump_generation()  # simulates a reindex happening mid-pagination

    with pytest.raises(StaleIndexGenerationError):
        structural_search(
            files=files, index_meta_store=index_meta_store,
            pattern="(function_definition name: (identifier) @func.name)",
            observed_at=_OBSERVED_AT, limit=1, cursor=page1["next_cursor"],
        )


def test_structural_search_skips_matches_with_no_captures():
    # A pattern with zero captures has no node ACIE can derive a location
    # from through QueryCursor.matches()'s API -- skipped rather than
    # crashing on an undefined anchor. Local decision, not asked about
    # explicitly; flagged in the completion memory.
    index_meta_store = _index_meta_store_with_generation(1)

    envelope = structural_search(
        files={"pkg/mod.py": _TWO_FUNCTIONS}, index_meta_store=index_meta_store,
        pattern="(function_definition)",
        observed_at=_OBSERVED_AT,
    )

    assert envelope["results"] == []
    assert envelope["total_count"] == 0
