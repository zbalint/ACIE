import pytest

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.affected_tests import affected_tests
from acie.tools.errors import InvalidArgumentError, SymbolNotFoundError

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


def test_affected_tests_direct_call_from_a_test_file_is_found():
    symbol_store, relation_store, index_meta_store = _stores()
    target = _symbol("pkg/mod.py:target#function", "pkg/mod.py", "target", "function", line=1)
    test_fn = _symbol(
        "tests/test_mod.py:test_target#function", "tests/test_mod.py", "test_target", "function", line=3,
    )
    symbol_store.upsert(target)
    symbol_store.upsert(test_fn)
    relation_store.upsert(_relation(test_fn.id, target.id, "calls", "tests/test_mod.py", 4, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=target.id,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_fn.id}
    assert envelope["root"] == target.id
    assert envelope["truncated"] is False


def test_affected_tests_root_never_appears_in_affected_tests_even_if_root_is_itself_a_test():
    symbol_store, relation_store, index_meta_store = _stores()
    root_test = _symbol(
        "tests/test_mod.py:test_root#function", "tests/test_mod.py", "test_root", "function", line=1,
    )
    caller_test = _symbol(
        "tests/test_mod.py:test_caller#function", "tests/test_mod.py", "test_caller", "function", line=5,
    )
    symbol_store.upsert(root_test)
    symbol_store.upsert(caller_test)
    relation_store.upsert(_relation(caller_test.id, root_test.id, "calls", "tests/test_mod.py", 6, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root_test.id,
    )

    assert root_test.id not in {t["id"] for t in envelope["affected_tests"]}
    assert {t["id"] for t in envelope["affected_tests"]} == {caller_test.id}


def test_affected_tests_traverses_overrides_predicate_finding_test_subclass_as_affected():
    symbol_store, relation_store, index_meta_store = _stores()
    base_method = _symbol("pkg/base.py:Base.test_foo#method", "pkg/base.py", "Base.test_foo", "method", line=2)
    test_override = _symbol(
        "tests/test_base.py:TestFoo.test_foo#method", "tests/test_base.py", "TestFoo.test_foo", "method", line=3,
    )
    symbol_store.upsert(base_method)
    symbol_store.upsert(test_override)
    relation_store.upsert(_relation(test_override.id, base_method.id, "overrides", "tests/test_base.py", 3, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=base_method.id,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_override.id}


def test_affected_tests_excludes_imports_predicate():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    importer_test = _symbol(
        "tests/test_mod.py:#module", "tests/test_mod.py", "", "module", line=1,
    )
    symbol_store.upsert(root)
    symbol_store.upsert(importer_test)
    relation_store.upsert(_relation(importer_test.id, root.id, "imports", "tests/test_mod.py", 1, 0))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_tests"] == []


def test_affected_tests_excludes_references_inherits_defines_predicates():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    referencer = _symbol(
        "tests/test_mod.py:test_referencer#function", "tests/test_mod.py", "test_referencer", "function", line=5,
    )
    inheriting = _symbol(
        "tests/test_mod.py:TestSub#class", "tests/test_mod.py", "TestSub", "class", line=9,
    )
    defining_module = _symbol("tests/test_mod.py:#module", "tests/test_mod.py", "", "module", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(referencer)
    symbol_store.upsert(inheriting)
    symbol_store.upsert(defining_module)
    relation_store.upsert(_relation(referencer.id, root.id, "references", "tests/test_mod.py", 6, 4))
    relation_store.upsert(_relation(inheriting.id, root.id, "inherits", "tests/test_mod.py", 9, 6))
    relation_store.upsert(_relation(defining_module.id, root.id, "defines", "tests/test_mod.py", 1, 0))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_tests"] == []


def test_affected_tests_transitively_found_through_a_non_test_intermediate_caller():
    # root <- helper (non-test, not emitted) <- test_it (emitted). node_cap/
    # depth_clamp must be generous enough to traverse THROUGH the non-test
    # hop, per the module's documented local decision that node_cap bounds
    # the whole BFS, not just the test-identified subset.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    helper = _symbol("pkg/mod.py:helper#function", "pkg/mod.py", "helper", "function", line=5)
    test_it = _symbol("tests/test_mod.py:test_it#function", "tests/test_mod.py", "test_it", "function", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(helper)
    symbol_store.upsert(test_it)
    relation_store.upsert(_relation(helper.id, root.id, "calls", "pkg/mod.py", 6, 4))
    relation_store.upsert(_relation(test_it.id, helper.id, "calls", "tests/test_mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id, depth_clamp=5,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_it.id}
    assert helper.id not in {t["id"] for t in envelope["affected_tests"]}


def test_affected_tests_requires_qualname_test_prefix_not_just_file_path():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    non_test_helper_in_test_file = _symbol(
        "tests/test_mod.py:helper_fixture#function", "tests/test_mod.py", "helper_fixture", "function", line=3,
    )
    symbol_store.upsert(root)
    symbol_store.upsert(non_test_helper_in_test_file)
    relation_store.upsert(
        _relation(non_test_helper_in_test_file.id, root.id, "calls", "tests/test_mod.py", 4, 4)
    )

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_tests"] == []


def test_affected_tests_requires_test_file_path_not_just_qualname_prefix():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_named_but_not_in_test_file = _symbol(
        "pkg/mod.py:test_helper#function", "pkg/mod.py", "test_helper", "function", line=3,
    )
    symbol_store.upsert(root)
    symbol_store.upsert(test_named_but_not_in_test_file)
    relation_store.upsert(
        _relation(test_named_but_not_in_test_file.id, root.id, "calls", "pkg/mod.py", 4, 4)
    )

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_tests"] == []


def test_affected_tests_accepts_star_test_dot_py_file_convention_too():
    # pytest's second file convention: *_test.py, not just test_*.py.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_fn = _symbol(
        "tests/mod_test.py:test_root#function", "tests/mod_test.py", "test_root", "function", line=1,
    )
    symbol_store.upsert(root)
    symbol_store.upsert(test_fn)
    relation_store.upsert(_relation(test_fn.id, root.id, "calls", "tests/mod_test.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_fn.id}


def test_affected_tests_recognizes_unittest_style_method_qualname():
    # kind="method", qualname="TestFoo.test_bar" -- the leaf after the last
    # dot must be checked, not the whole qualname.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_method = _symbol(
        "tests/test_mod.py:TestFoo.test_bar#method", "tests/test_mod.py", "TestFoo.test_bar", "method", line=3,
    )
    symbol_store.upsert(root)
    symbol_store.upsert(test_method)
    relation_store.upsert(_relation(test_method.id, root.id, "calls", "tests/test_mod.py", 4, 8))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_method.id}


def test_affected_tests_carries_discovery_predicate_field():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_fn = _symbol("tests/test_mod.py:test_root#function", "tests/test_mod.py", "test_root", "function", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(test_fn)
    relation_store.upsert(_relation(test_fn.id, root.id, "calls", "tests/test_mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["affected_tests"][0]["discovery_predicate"] == "calls"


def test_affected_tests_summary_breaks_out_counts_by_confidence_tier():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    extracted_test = _symbol(
        "tests/test_mod.py:test_extracted#function", "tests/test_mod.py", "test_extracted", "function", line=5,
    )
    ambiguous_test = _symbol(
        "tests/test_mod.py:test_ambiguous#function", "tests/test_mod.py", "test_ambiguous", "function", line=9,
    )
    symbol_store.upsert(root)
    symbol_store.upsert(extracted_test)
    symbol_store.upsert(ambiguous_test)
    relation_store.upsert(
        _relation(extracted_test.id, root.id, "calls", "tests/test_mod.py", 6, 4, confidence=Confidence.EXTRACTED)
    )
    relation_store.upsert(
        _relation(ambiguous_test.id, root.id, "calls", "tests/test_mod.py", 10, 4, confidence=Confidence.AMBIGUOUS)
    )

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    assert envelope["test_summary"] == {"total": 2, "EXTRACTED": 1, "INFERRED": 0, "AMBIGUOUS": 1}


def test_affected_tests_summary_is_zeroed_when_root_has_no_affected_tests():
    symbol_store, relation_store, index_meta_store = _stores()
    lonely = _symbol("pkg/mod.py:lonely#function", "pkg/mod.py", "lonely", "function")
    symbol_store.upsert(lonely)

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=lonely.id,
    )

    assert envelope["affected_tests"] == []
    assert envelope["test_summary"] == {"total": 0, "EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
    assert envelope["truncated"] is False


def test_affected_tests_depth_clamp_stops_traversal_and_reports_truncated():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    helper = _symbol("pkg/mod.py:helper#function", "pkg/mod.py", "helper", "function", line=5)
    test_it = _symbol("tests/test_mod.py:test_it#function", "tests/test_mod.py", "test_it", "function", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(helper)
    symbol_store.upsert(test_it)
    relation_store.upsert(_relation(helper.id, root.id, "calls", "pkg/mod.py", 6, 4))
    relation_store.upsert(_relation(test_it.id, helper.id, "calls", "tests/test_mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id, depth_clamp=1,
    )

    assert envelope["affected_tests"] == []
    assert envelope["truncated"] is True


def test_affected_tests_node_cap_truncates_and_reports_truncated_true():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_a = _symbol("tests/test_mod.py:test_a#function", "tests/test_mod.py", "test_a", "function", line=1)
    test_b = _symbol("tests/test_mod.py:test_b#function", "tests/test_mod.py", "test_b", "function", line=5)
    symbol_store.upsert(root)
    symbol_store.upsert(test_a)
    symbol_store.upsert(test_b)
    relation_store.upsert(_relation(test_a.id, root.id, "calls", "tests/test_mod.py", 2, 4))
    relation_store.upsert(_relation(test_b.id, root.id, "calls", "tests/test_mod.py", 6, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id, node_cap=2,
    )

    assert len(envelope["affected_tests"]) == 1
    assert envelope["truncated"] is True
    assert envelope["node_cap"] == 2


def test_affected_tests_handles_a_two_node_call_cycle_without_infinite_loop():
    symbol_store, relation_store, index_meta_store = _stores()
    a = _symbol("pkg/mod.py:a#function", "pkg/mod.py", "a", "function", line=1)
    test_b = _symbol("tests/test_mod.py:test_b#function", "tests/test_mod.py", "test_b", "function", line=5)
    symbol_store.upsert(a)
    symbol_store.upsert(test_b)
    relation_store.upsert(_relation(test_b.id, a.id, "calls", "tests/test_mod.py", 6, 4))
    relation_store.upsert(_relation(a.id, test_b.id, "calls", "pkg/mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=a.id, depth_clamp=5,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_b.id}
    assert envelope["truncated"] is False


def test_affected_tests_unresolved_leaf_is_never_emitted_but_still_traversed_through():
    # An orphaned relation source (not in symbol_store) has no kind/path/
    # qualname to classify -- can never itself be a test -- but the BFS
    # still walks onward through it to a real test further upstream.
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_it = _symbol("tests/test_mod.py:test_it#function", "tests/test_mod.py", "test_it", "function", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(test_it)
    ghost_id = "pkg/mod.py:ghost#function"
    relation_store.upsert(_relation(ghost_id, root.id, "calls", "pkg/mod.py", 2, 4))
    relation_store.upsert(_relation(test_it.id, ghost_id, "calls", "tests/test_mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id, depth_clamp=5,
    )

    assert {t["id"] for t in envelope["affected_tests"]} == {test_it.id}


def test_affected_tests_full_reveals_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_fn = _symbol("tests/test_mod.py:test_root#function", "tests/test_mod.py", "test_root", "function", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(test_fn)
    relation_store.upsert(_relation(test_fn.id, root.id, "calls", "tests/test_mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id, full=True,
    )

    affected = envelope["affected_tests"][0]
    assert affected["confidence"] == "EXTRACTED"
    assert affected["provenance"]["provider"] == "tree-sitter"


def test_affected_tests_terse_by_default_hides_confidence_and_provenance():
    symbol_store, relation_store, index_meta_store = _stores()
    root = _symbol("pkg/mod.py:root#function", "pkg/mod.py", "root", "function", line=1)
    test_fn = _symbol("tests/test_mod.py:test_root#function", "tests/test_mod.py", "test_root", "function", line=1)
    symbol_store.upsert(root)
    symbol_store.upsert(test_fn)
    relation_store.upsert(_relation(test_fn.id, root.id, "calls", "tests/test_mod.py", 2, 4))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root=root.id,
    )

    affected = envelope["affected_tests"][0]
    assert "confidence" not in affected
    assert "provenance" not in affected


def test_affected_tests_raises_symbol_not_found_for_unknown_root():
    symbol_store, relation_store, index_meta_store = _stores()

    with pytest.raises(SymbolNotFoundError):
        affected_tests(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:nope#function",
        )


def test_affected_tests_raises_symbol_not_found_for_tombstoned_root():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))
    symbol_store.delete("pkg/mod.py:foo#function", observed_at="2026-08-31T01:00:00Z")

    with pytest.raises(SymbolNotFoundError):
        affected_tests(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function",
        )


def test_affected_tests_raises_invalid_argument_for_non_positive_node_cap():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        affected_tests(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", node_cap=0,
        )


def test_affected_tests_raises_invalid_argument_for_non_positive_depth_clamp():
    symbol_store, relation_store, index_meta_store = _stores()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    with pytest.raises(InvalidArgumentError):
        affected_tests(
            symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
            root="pkg/mod.py:foo#function", depth_clamp=-1,
        )


def test_affected_tests_reports_index_generation():
    symbol_store, relation_store, index_meta_store = _stores()
    index_meta_store.bump_generation()
    index_meta_store.bump_generation()
    symbol_store.upsert(_symbol("pkg/mod.py:foo#function", "pkg/mod.py", "foo", "function"))

    envelope = affected_tests(
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
        root="pkg/mod.py:foo#function",
    )

    assert envelope["index_generation"] == 2
