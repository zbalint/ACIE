"""Tests for acie.tools.architecture -- v1 slice C1 (wayfinder ticket
47d8cd0d): the one-time dotted-name -> file_path(s) index the eventual
`architecture` MCP tool (C2, not yet built) will use to classify each
`imports` edge's raw dotted-name target as internal (resolves to a real
file in this repo) or external (doesn't resolve). See
acie.tools.architecture's module docstring and acie.module_paths for the
suffix-tolerant derivation this index is built from.
"""

from acie.indexer import index_file
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.architecture import build_dotted_name_index

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-09-03T00:00:00Z")


def _module_symbol(path):
    return Symbol(
        id=f"{path}:#module", path=path, qualname="", kind="module",
        start_line=1, start_col=0, end_line=1, end_col=0,
        confidence=Confidence.EXTRACTED, provenance=_PROVENANCE,
    )


def test_empty_repo_produces_an_empty_index():
    symbol_store = SymbolStore(":memory:")
    assert build_dotted_name_index(symbol_store) == {}


def test_single_file_registers_its_full_dotted_path():
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("pkg/mod.py"))

    index = build_dotted_name_index(symbol_store)

    assert index["pkg.mod"] == ["pkg/mod.py"]


def test_single_file_registers_every_dotted_suffix_not_just_the_full_path():
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("pkg/sub/mod.py"))

    index = build_dotted_name_index(symbol_store)

    assert index["pkg.sub.mod"] == ["pkg/sub/mod.py"]
    assert index["sub.mod"] == ["pkg/sub/mod.py"]
    assert index["mod"] == ["pkg/sub/mod.py"]


def test_init_py_registers_under_its_package_name_not_a_dunder_init_suffix():
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("pkg/__init__.py"))

    index = build_dotted_name_index(symbol_store)

    assert index["pkg"] == ["pkg/__init__.py"]
    assert "__init__" not in index


def test_src_layout_prefix_does_not_prevent_resolution_by_the_real_import_name():
    # The design-risk memory's (271dc881) feared failure mode: a src-layout
    # repo's internal imports silently misclassifying as external. Confirms
    # this does NOT happen -- src/mypackage/foo.py is found by the dotted
    # name real code actually imports it as, `mypackage.foo`.
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("src/mypackage/foo.py"))

    index = build_dotted_name_index(symbol_store)

    assert index["mypackage.foo"] == ["src/mypackage/foo.py"]
    assert "src.mypackage.foo" not in index or index["src.mypackage.foo"] == ["src/mypackage/foo.py"]


def test_two_files_sharing_a_dotted_suffix_both_appear_as_candidates():
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("vendor_a/pkg/other.py"))
    symbol_store.upsert(_module_symbol("vendor_b/pkg/other.py"))

    index = build_dotted_name_index(symbol_store)

    assert set(index["pkg.other"]) == {"vendor_a/pkg/other.py", "vendor_b/pkg/other.py"}


def test_non_module_symbols_are_excluded_from_the_index():
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("pkg/mod.py"))
    symbol_store.upsert(
        Symbol(
            id="pkg/mod.py:helper#function", path="pkg/mod.py", qualname="helper", kind="function",
            start_line=2, start_col=0, end_line=3, end_col=8,
            confidence=Confidence.EXTRACTED, provenance=_PROVENANCE,
        )
    )

    index = build_dotted_name_index(symbol_store)

    # Only the module's own dotted suffixes appear -- a same-named function
    # symbol must never leak in as a spurious "module" candidate.
    assert index["mod"] == ["pkg/mod.py"]
    assert "helper" not in index


def test_a_dotted_name_with_no_matching_file_is_absent_from_the_index():
    symbol_store = SymbolStore(":memory:")
    symbol_store.upsert(_module_symbol("pkg/mod.py"))

    index = build_dotted_name_index(symbol_store)

    assert "os.path" not in index


def test_index_built_from_a_real_indexed_src_layout_repo():
    # End-to-end through the real index_file pipeline (not hand-built
    # Symbol objects), same "don't let integration coverage lag a new
    # extractor" lesson from B1's review (memory d56588f1).
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")

    index_file(
        path="src/mypackage/foo.py",
        source_text="def helper():\n    pass\n",
        observed_at="2026-09-03T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="src/mypackage/bar.py",
        source_text="from mypackage.foo import helper\n\n\nhelper()\n",
        observed_at="2026-09-03T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    index = build_dotted_name_index(symbol_store)

    assert index["mypackage.foo"] == ["src/mypackage/foo.py"]
    assert index["mypackage.bar"] == ["src/mypackage/bar.py"]
