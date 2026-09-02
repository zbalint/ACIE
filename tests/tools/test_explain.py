import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.errors import (
    EdgeNotFoundError,
    InvalidArgumentError,
    InvalidCursorError,
    InvalidLimitError,
    SymbolNotFoundError,
)
from acie.tools.explain import explain
from acie.tools.pagination import encode_cursor

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z")


def _symbol(id_, path, qualname, kind, line=1, col=0, observed_at="2026-08-31T00:00:00Z"):
    return Symbol(
        id=id_, path=path, qualname=qualname, kind=kind,
        start_line=line, start_col=col, end_line=line + 1, end_col=8,
        confidence=Confidence.EXTRACTED,
        provenance=Provenance(provider="tree-sitter", version="0.25.0", observed_at=observed_at),
    )


def _relation(source, target, predicate, site_file, site_line, site_col,
              confidence=Confidence.EXTRACTED, observed_at="2026-08-31T00:00:00Z"):
    return Relation(
        source=source, target=target, predicate=predicate,
        site_file=site_file, site_line=site_line, site_col=site_col,
        confidence=confidence,
        provenance=Provenance(provider="tree-sitter", version="0.25.0", observed_at=observed_at),
    )


def _stores():
    return SymbolStore(":memory:"), RelationStore(":memory:"), IndexMetaStore(":memory:")


def test_explain_symbol_with_no_revisions_returns_single_entry_array():
    symbol_store, relation_store, index_meta_store = _stores()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=foo.id,
    )

    assert len(envelope["results"]) == 1
    assert envelope["results"][0]["id"] == foo.id
    assert envelope["total_count"] == 1
    assert envelope["truncated"] is False


def test_explain_raises_symbol_not_found_for_unknown_symbol_id():
    symbol_store, relation_store, index_meta_store = _stores()

    with pytest.raises(SymbolNotFoundError):
        explain(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id="pkg/mod.py:nope#function",
        )


def test_explain_symbol_moved_returns_two_entries_newest_first():
    symbol_store, relation_store, index_meta_store = _stores()
    original = _symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1,
        observed_at="2026-08-31T00:00:00Z",
    )
    symbol_store.upsert(original)
    moved = _symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=12,
        observed_at="2026-08-31T01:00:00Z",
    )
    symbol_store.upsert(moved)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=original.id,
    )

    assert [r["start_line"] for r in envelope["results"]] == [12, 1]
    assert envelope["total_count"] == 2


def test_explain_deleted_symbol_returns_history_tagged_deleted_not_symbol_not_found():
    symbol_store, relation_store, index_meta_store = _stores()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)
    symbol_store.delete(foo.id, observed_at="2026-08-31T02:00:00Z")

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=foo.id,
    )

    assert len(envelope["results"]) == 1
    assert envelope["results"][0]["deleted"] is True
    assert envelope["results"][0]["start_line"] == 1


def test_explain_always_reveals_confidence_and_provenance_even_when_terse():
    # Unlike every other tool, explain's whole point is showing how a
    # fact's confidence/provenance changed across observations -- so unlike
    # the blanket terse/full rule, these fields are never hidden here, even
    # at the default full=False. See ARCHITECTURE.md "MCP Tool Surface".
    symbol_store, relation_store, index_meta_store = _stores()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=foo.id,
    )

    assert envelope["results"][0]["confidence"] == "EXTRACTED"
    assert envelope["results"][0]["provenance"]["provider"] == "tree-sitter"


def test_explain_full_reveals_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=foo.id, full=True,
    )

    assert envelope["results"][0]["confidence"] == "EXTRACTED"
    assert envelope["results"][0]["provenance"]["provider"] == "tree-sitter"


def test_explain_paginates_with_limit_and_cursor():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1,
        observed_at="2026-08-31T00:00:00Z",
    ))
    symbol_store.upsert(_symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=5,
        observed_at="2026-08-31T01:00:00Z",
    ))
    symbol_store.upsert(_symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=9,
        observed_at="2026-08-31T02:00:00Z",
    ))

    page1 = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:foo#function", limit=2,
    )
    assert [r["start_line"] for r in page1["results"]] == [9, 5]
    assert page1["truncated"] is True
    assert page1["total_count"] == 3
    assert page1["next_cursor"] is not None

    page2 = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:foo#function", limit=2, cursor=page1["next_cursor"],
    )
    assert [r["start_line"] for r in page2["results"]] == [1]
    assert page2["truncated"] is False
    assert page2["next_cursor"] is None


def test_explain_reports_index_generation():
    symbol_store, relation_store, index_meta_store = _stores()
    index_meta_store.bump_generation()
    index_meta_store.bump_generation()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id=foo.id,
    )

    assert envelope["index_generation"] == 2


def test_explain_never_errors_on_stale_cursor_generation():
    # The one deliberate deviation from every other flat-list tool: explain
    # never raises StaleIndexGenerationError, since showing history across
    # index generations is its entire purpose (ARCHITECTURE.md).
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1,
        observed_at="2026-08-31T00:00:00Z",
    ))
    symbol_store.upsert(_symbol(
        "pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=5,
        observed_at="2026-08-31T01:00:00Z",
    ))

    page1 = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:foo#function", limit=1,
    )
    index_meta_store.bump_generation()

    page2 = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        symbol_id="pkg/mod.py:foo#function", limit=1, cursor=page1["next_cursor"],
    )
    assert [r["start_line"] for r in page2["results"]] == [1]


def test_explain_edge_confidence_upgrade_returns_two_entries_newest_first():
    symbol_store, relation_store, index_meta_store = _stores()
    ambiguous = _relation(
        "pkg/mod.py:a#function", "pkg/mod.py:b#function", "calls", "pkg/mod.py", 2, 4,
        confidence=Confidence.AMBIGUOUS, observed_at="2026-08-31T00:00:00Z",
    )
    relation_store.upsert(ambiguous)
    extracted = _relation(
        "pkg/mod.py:a#function", "pkg/mod.py:b#function", "calls", "pkg/mod.py", 2, 4,
        confidence=Confidence.EXTRACTED, observed_at="2026-08-31T01:00:00Z",
    )
    relation_store.upsert(extracted)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        edge_ref=_edge_ref(ambiguous), full=True,
    )

    assert [r["confidence"] for r in envelope["results"]] == ["EXTRACTED", "AMBIGUOUS"]
    assert envelope["total_count"] == 2


def test_explain_deleted_edge_returns_history_tagged_deleted_not_edge_not_found():
    symbol_store, relation_store, index_meta_store = _stores()
    relation = _relation("pkg/mod.py:a#function", "pkg/mod.py:b#function", "calls", "pkg/mod.py", 2, 4)
    relation_store.upsert(relation)
    relation_store.delete(
        source=relation.source, target=relation.target, predicate=relation.predicate,
        site_file=relation.site_file, site_line=relation.site_line, site_col=relation.site_col,
        observed_at="2026-08-31T02:00:00Z",
    )

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        edge_ref=_edge_ref(relation),
    )

    assert len(envelope["results"]) == 1
    assert envelope["results"][0]["deleted"] is True
    assert envelope["results"][0]["source"] == relation.source


def test_explain_requires_exactly_one_of_symbol_id_or_edge_ref():
    # Regression for LIVE_MCP_QUALIFICATION_REPORT.md (2026-09-01): this used
    # to raise a bare ValueError, which dispatch.py's generic exception
    # handler then demoted to an unhelpful INTERNAL_ERROR.
    symbol_store, relation_store, index_meta_store = _stores()

    with pytest.raises(InvalidArgumentError):
        explain(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        )


def test_explain_raises_invalid_limit_for_a_non_positive_limit():
    symbol_store, relation_store, index_meta_store = _stores()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)

    with pytest.raises(InvalidLimitError):
        explain(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id=foo.id, limit=0,
        )


def test_explain_raises_invalid_cursor_for_a_non_composite_last_id():
    # Code-review regression (2026-09-02): see test_find_references' equivalent.
    symbol_store, relation_store, index_meta_store = _stores()
    foo = _symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function", line=1)
    symbol_store.upsert(foo)
    bad_cursor = encode_cursor(1, 0)

    with pytest.raises(InvalidCursorError):
        explain(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            symbol_id=foo.id, cursor=bad_cursor,
        )


def _edge_ref(relation):
    return {
        "source_symbol_id": relation.source,
        "target_symbol_id": relation.target,
        "predicate": relation.predicate,
        "site_file": relation.site_file,
        "site_line": relation.site_line,
        "site_col": relation.site_col,
    }


def test_explain_edge_with_no_revisions_returns_single_entry_array():
    symbol_store, relation_store, index_meta_store = _stores()
    relation = _relation("pkg/mod.py:a#function", "pkg/mod.py:b#function", "calls", "pkg/mod.py", 2, 4)
    relation_store.upsert(relation)

    envelope = explain(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        edge_ref=_edge_ref(relation),
    )

    assert len(envelope["results"]) == 1
    assert envelope["results"][0]["source"] == relation.source
    assert envelope["results"][0]["target"] == relation.target
    assert envelope["total_count"] == 1
    assert envelope["truncated"] is False


def test_explain_raises_edge_not_found_for_unknown_edge_ref():
    symbol_store, relation_store, index_meta_store = _stores()

    with pytest.raises(EdgeNotFoundError):
        explain(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            edge_ref={
                "source_symbol_id": "pkg/mod.py:a#function",
                "target_symbol_id": "pkg/mod.py:b#function",
                "predicate": "calls",
                "site_file": "pkg/mod.py",
                "site_line": 2,
                "site_col": 4,
            },
        )
