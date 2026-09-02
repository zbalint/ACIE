import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.tools.errors import InvalidCursorError, InvalidLimitError, StaleIndexGenerationError
from acie.tools.list_imports import list_imports
from acie.tools.pagination import encode_cursor

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z")


def _import_relation(module_symbol_id, target, site_line, site_col=0):
    return Relation(
        source=module_symbol_id, target=target, predicate="imports",
        site_file="pkg/mod.py", site_line=site_line, site_col=site_col,
        confidence=Confidence.EXTRACTED, provenance=_PROVENANCE,
    )


def _stores_with_generation(n):
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    for _ in range(n):
        index_meta_store.bump_generation()
    return relation_store, index_meta_store


def test_list_imports_returns_import_relations_for_the_file_terse():
    relation_store, index_meta_store = _stores_with_generation(1)
    relation_store.upsert(_import_relation("pkg/mod.py:<module>#module", "os", 1))

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
    )

    assert envelope["index_generation"] == 1
    assert envelope["total_count"] == 1
    assert envelope["truncated"] is False
    result = envelope["results"][0]
    assert result == {
        "source": "pkg/mod.py:<module>#module", "target": "os", "predicate": "imports",
        "site_file": "pkg/mod.py", "site_line": 1, "site_col": 0,
    }
    assert "confidence" not in result
    assert "provenance" not in result


def test_list_imports_full_reveals_confidence_and_provenance():
    relation_store, index_meta_store = _stores_with_generation(1)
    relation_store.upsert(_import_relation("pkg/mod.py:<module>#module", "os", 1))

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py", full=True,
    )

    result = envelope["results"][0]
    assert result["confidence"] == "EXTRACTED"
    assert result["provenance"] == {
        "provider": "tree-sitter", "version": "0.25.0", "observed_at": "2026-08-31T00:00:00Z",
    }


def test_list_imports_excludes_relations_of_other_predicates_in_the_same_file():
    relation_store, index_meta_store = _stores_with_generation(1)
    module_id = "pkg/mod.py:<module>#module"
    relation_store.upsert(_import_relation(module_id, "os", 1))
    relation_store.upsert(
        Relation(
            source=module_id, target="pkg/mod.py:foo#function", predicate="defines",
            site_file="pkg/mod.py", site_line=3, site_col=0,
            confidence=Confidence.EXTRACTED, provenance=_PROVENANCE,
        )
    )

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
    )

    assert envelope["total_count"] == 1
    assert envelope["results"][0]["target"] == "os"


def test_list_imports_excludes_import_relations_from_other_files():
    relation_store, index_meta_store = _stores_with_generation(1)
    relation_store.upsert(_import_relation("pkg/mod.py:<module>#module", "os", 1))
    relation_store.upsert(
        Relation(
            source="pkg/other.py:<module>#module", target="sys", predicate="imports",
            site_file="pkg/other.py", site_line=1, site_col=0,
            confidence=Confidence.EXTRACTED, provenance=_PROVENANCE,
        )
    )

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
    )

    assert envelope["total_count"] == 1
    assert envelope["results"][0]["target"] == "os"


def test_list_imports_returns_empty_when_file_has_no_imports():
    relation_store, index_meta_store = _stores_with_generation(1)

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/empty.py",
    )

    assert envelope["results"] == []
    assert envelope["total_count"] == 0
    assert envelope["truncated"] is False


def test_list_imports_orders_by_site_line_site_col_target():
    relation_store, index_meta_store = _stores_with_generation(1)
    module_id = "pkg/mod.py:<module>#module"
    # Later import upserted first, to prove ordering is by the key, not
    # insertion order.
    relation_store.upsert(_import_relation(module_id, "sys", 5))
    relation_store.upsert(_import_relation(module_id, "os", 1))

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
    )

    assert [r["target"] for r in envelope["results"]] == ["os", "sys"]


def test_list_imports_orders_by_target_as_final_tiebreak_on_a_shared_site():
    relation_store, index_meta_store = _stores_with_generation(1)
    module_id = "pkg/mod.py:<module>#module"
    # Same (site_line, site_col) -- e.g. a combined `import os, sys` this
    # extractor doesn't (yet) split, or simply two synthetic same-site
    # relations -- target must still break the tie deterministically.
    relation_store.upsert(_import_relation(module_id, "sys", 1))
    relation_store.upsert(_import_relation(module_id, "os", 1))

    envelope = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
    )

    assert [r["target"] for r in envelope["results"]] == ["os", "sys"]


def test_list_imports_paginates_via_next_cursor():
    relation_store, index_meta_store = _stores_with_generation(1)
    module_id = "pkg/mod.py:<module>#module"
    relation_store.upsert(_import_relation(module_id, "os", 1))
    relation_store.upsert(_import_relation(module_id, "sys", 2))

    page1 = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py", limit=1,
    )
    assert [r["target"] for r in page1["results"]] == ["os"]
    assert page1["total_count"] == 2
    assert page1["truncated"] is True
    assert page1["next_cursor"] is not None

    page2 = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
        limit=1, cursor=page1["next_cursor"],
    )
    assert [r["target"] for r in page2["results"]] == ["sys"]
    assert page2["total_count"] == 2
    assert page2["truncated"] is False
    assert page2["next_cursor"] is None


def test_list_imports_raises_stale_index_generation_when_generation_changed_since_the_cursor_was_issued():
    relation_store, index_meta_store = _stores_with_generation(1)
    module_id = "pkg/mod.py:<module>#module"
    relation_store.upsert(_import_relation(module_id, "os", 1))
    relation_store.upsert(_import_relation(module_id, "sys", 2))

    page1 = list_imports(
        relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py", limit=1,
    )
    assert page1["truncated"] is True

    index_meta_store.bump_generation()  # simulates a reindex happening mid-pagination

    with pytest.raises(StaleIndexGenerationError):
        list_imports(
            relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
            limit=1, cursor=page1["next_cursor"],
        )


def test_list_imports_raises_invalid_limit_for_a_non_positive_limit():
    relation_store, index_meta_store = _stores_with_generation(1)
    relation_store.upsert(_import_relation("pkg/mod.py:<module>#module", "os", 1))

    with pytest.raises(InvalidLimitError):
        list_imports(
            relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py", limit=0,
        )


def test_list_imports_raises_invalid_cursor_for_a_non_composite_last_id():
    # Code-review regression (2026-09-02): see test_find_references' equivalent.
    relation_store, index_meta_store = _stores_with_generation(1)
    relation_store.upsert(_import_relation("pkg/mod.py:<module>#module", "os", 1))
    bad_cursor = encode_cursor(1, 0)

    with pytest.raises(InvalidCursorError):
        list_imports(
            relation_store=relation_store, index_meta_store=index_meta_store, file="pkg/mod.py",
            cursor=bad_cursor,
        )
