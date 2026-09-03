"""Tests for acie.tools.architecture.

v1 slice C1 (wayfinder ticket 47d8cd0d) built `build_dotted_name_index`,
the one-time dotted-name -> file_path(s) index the `architecture` MCP tool
uses to classify each `imports` edge's raw dotted-name target as internal
(resolves to a real file in this repo) or external (doesn't resolve). See
acie.tools.architecture's module docstring and acie.module_paths for the
suffix-tolerant derivation this index is built from.

v1 slice C2 adds the `architecture(root, granularity, node_cap, full)` MCP
tool itself, file granularity. v1 slice C3 adds `granularity="package"`,
a directory-based rollup one level up from C2's file nodes. See
architecture()'s own docstring for the seam decisions (C2's root
path-prefix-scope semantics confirmed via AskUserQuestion; C3's package
node/edge shape and the rest decided locally following
graph.py/impact_analysis.py/affected_tests.py precedent).
"""

import json

from acie.indexer import index_file
from acie.ir.symbol import Confidence, Provenance, Symbol
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore
from acie.tools.architecture import architecture, build_dotted_name_index
from acie.tools.errors import InvalidArgumentError, InvalidConfigError

_PROVENANCE = Provenance(provider="tree-sitter", version="0.25.0", observed_at="2026-09-03T00:00:00Z")
_OBSERVED_AT = "2026-09-03T00:00:00Z"


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


# ---------------------------------------------------------------------------
# architecture() -- v1 slice C2, file granularity only.
# ---------------------------------------------------------------------------


def _stores():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    return symbol_store, relation_store, index_meta_store


def _index(symbol_store, relation_store, index_meta_store, path, source_text):
    index_file(
        path=path, source_text=source_text, observed_at=_OBSERVED_AT,
        symbol_store=symbol_store, relation_store=relation_store, index_meta_store=index_meta_store,
    )


def test_empty_repo_produces_no_nodes_or_edges():
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["truncated"] is False


def test_a_single_file_with_no_imports_is_one_node_with_zero_external_dependencies():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/mod.py", "def helper():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["path"] == "pkg/mod.py"
    assert result["nodes"][0]["kind"] == "module"
    assert result["nodes"][0]["external_dependency_count"] == 0
    assert result["edges"] == []


def test_a_resolvable_import_between_two_files_produces_one_edge():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/foo.py", "def helper():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/bar.py",
        "from pkg.foo import helper\n\n\nhelper()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store)

    paths = {node["path"] for node in result["nodes"]}
    assert paths == {"pkg/foo.py", "pkg/bar.py"}
    assert result["edges"] == [{"source": "pkg/bar.py", "target": "pkg/foo.py"}]
    bar_node = next(node for node in result["nodes"] if node["path"] == "pkg/bar.py")
    assert bar_node["external_dependency_count"] == 0


def test_multiple_imports_of_the_same_target_file_collapse_to_one_edge():
    # pkg/bar.py imports two distinct names from pkg/foo.py -- two `imports`
    # relations at the symbol level, but only one file-level edge once
    # rolled up (the whole point of an "aggregation view").
    symbol_store, relation_store, index_meta_store = _stores()
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/foo.py",
        "def helper():\n    pass\n\n\ndef other():\n    pass\n",
    )
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/bar.py",
        "from pkg.foo import helper, other\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert result["edges"] == [{"source": "pkg/bar.py", "target": "pkg/foo.py"}]


def test_a_file_importing_from_itself_produces_a_self_loop_edge():
    # Review finding (P1, commit 260caec): an earlier `candidate_path !=
    # module.path` guard silently dropped this edge instead of rendering
    # it. A module's own `imports` relation resolving back to its own file
    # is real data -- keep it as a genuine source == target self-loop.
    symbol_store, relation_store, index_meta_store = _stores()
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/foo.py",
        "from pkg.foo import helper\n\n\ndef helper():\n    pass\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert result["edges"] == [{"source": "pkg/foo.py", "target": "pkg/foo.py"}]


def test_an_unresolvable_import_increments_external_dependency_count_and_adds_no_node():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/mod.py", "import os\n")

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["path"] == "pkg/mod.py"
    assert result["nodes"][0]["external_dependency_count"] == 1
    assert result["edges"] == []
    assert all(node["path"] != "os" for node in result["nodes"])


def test_root_scopes_nodes_to_files_under_that_path_prefix():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/sub/a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg/subx/b.py", "def b():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg/other/c.py", "def c():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, root="pkg/sub")

    # "pkg/subx/b.py" must NOT match root="pkg/sub" (prefix boundary, not a
    # bare string.startswith on the unnormalized root).
    assert {node["path"] for node in result["nodes"]} == {"pkg/sub/a.py"}


def test_root_none_includes_every_file_in_the_repo():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "b/c.py", "def c():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, root=None)

    assert {node["path"] for node in result["nodes"]} == {"a.py", "b/c.py"}


def test_root_matching_no_files_returns_an_empty_view_not_an_error():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "a.py", "def a():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, root="nowhere")

    assert result["nodes"] == []
    assert result["edges"] == []


def test_an_import_resolving_outside_the_scoped_root_produces_no_edge_and_is_not_external():
    # pkg/sub/a.py imports pkg/other/c.py -- resolves internally, but
    # pkg/other/c.py is out of root="pkg/sub" scope, so no edge is rendered
    # for it and it is NOT counted as an external dependency either (it did
    # resolve within the repo -- it's just outside this view).
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/other/c.py", "def c():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/sub/a.py",
        "from pkg.other.c import c\n\n\nc()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, root="pkg/sub")

    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["path"] == "pkg/sub/a.py"
    assert result["nodes"][0]["external_dependency_count"] == 0
    assert result["edges"] == []


def test_an_ambiguously_resolving_import_produces_an_edge_to_each_candidate():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "vendor_a/pkg/other.py", "def x():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "vendor_b/pkg/other.py", "def x():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "main.py",
        "from pkg.other import x\n\n\nx()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert result["edges"] == [
        {"source": "main.py", "target": "vendor_a/pkg/other.py"},
        {"source": "main.py", "target": "vendor_b/pkg/other.py"},
    ]


def test_node_cap_truncates_the_node_list_and_sets_truncated_true():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "b.py", "def b():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, node_cap=1)

    assert len(result["nodes"]) == 1
    assert result["truncated"] is True


def test_node_cap_not_exceeded_reports_truncated_false():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "a.py", "def a():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, node_cap=100)

    assert result["truncated"] is False


def test_non_positive_node_cap_raises_invalid_argument_error():
    symbol_store, relation_store, index_meta_store = _stores()

    try:
        architecture(symbol_store, relation_store, index_meta_store, node_cap=0)
        assert False, "expected InvalidArgumentError"
    except InvalidArgumentError:
        pass


def test_unrecognized_granularity_raises_invalid_argument_error():
    symbol_store, relation_store, index_meta_store = _stores()

    try:
        architecture(symbol_store, relation_store, index_meta_store, granularity="symbol")
        assert False, "expected InvalidArgumentError"
    except InvalidArgumentError:
        pass


def test_unrecognized_granularity_still_names_both_valid_values_in_the_message():
    symbol_store, relation_store, index_meta_store = _stores()

    try:
        architecture(symbol_store, relation_store, index_meta_store, granularity="symbol")
        assert False, "expected InvalidArgumentError"
    except InvalidArgumentError as exc:
        assert "'file'" in str(exc) and "'package'" in str(exc)


def test_full_false_omits_confidence_and_provenance_on_nodes():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "a.py", "def a():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, full=False)

    assert "confidence" not in result["nodes"][0]
    assert "provenance" not in result["nodes"][0]


def test_full_true_reveals_confidence_and_provenance_on_nodes():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "a.py", "def a():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, full=True)

    assert result["nodes"][0]["confidence"] == "EXTRACTED"
    assert result["nodes"][0]["provenance"]["provider"] == "tree-sitter"


def test_index_generation_is_included_in_the_envelope():
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert result["index_generation"] == index_meta_store.current_generation()


def test_root_and_node_cap_are_echoed_in_the_envelope():
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store, root="pkg", node_cap=42)

    assert result["root"] == "pkg"
    assert result["node_cap"] == 42


# ---------------------------------------------------------------------------
# architecture() -- v1 slice C3, package (directory-based rollup) granularity.
# ---------------------------------------------------------------------------


def test_package_empty_repo_produces_no_nodes_or_edges():
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["truncated"] is False


def test_package_node_path_is_the_files_immediate_containing_directory():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/sub/mod.py", "def helper():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["path"] == "pkg/sub"
    assert result["nodes"][0]["kind"] == "package"
    assert result["nodes"][0]["external_dependency_count"] == 0


def test_package_a_repo_root_file_with_no_directory_rolls_up_to_the_empty_string_package():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "mod.py", "def helper():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["nodes"] == [{"path": "", "kind": "package", "external_dependency_count": 0}]


def test_package_two_files_in_the_same_directory_collapse_to_one_node():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg/b.py", "def b():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["nodes"] == [{"path": "pkg", "kind": "package", "external_dependency_count": 0}]


def test_package_an_import_between_files_in_the_same_directory_produces_no_self_edge():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/foo.py", "def helper():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/bar.py",
        "from pkg.foo import helper\n\n\nhelper()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["nodes"] == [{"path": "pkg", "kind": "package", "external_dependency_count": 0}]
    assert result["edges"] == []


def test_package_a_resolvable_cross_directory_import_produces_one_edge_between_package_dirs():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg_a/foo.py", "def helper():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg_b/bar.py",
        "from pkg_a.foo import helper\n\n\nhelper()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["edges"] == [{"source": "pkg_b", "target": "pkg_a"}]


def test_package_multiple_cross_directory_imports_between_the_same_two_packages_collapse_to_one_edge():
    # pkg_b/bar.py imports from two different files in pkg_a/ -- several
    # file-level imports relations, still one edge once rolled up to
    # package granularity.
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg_a/foo.py", "def helper():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg_a/other.py", "def other():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg_b/bar.py",
        "from pkg_a.foo import helper\nfrom pkg_a.other import other\n\n\nhelper()\nother()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["edges"] == [{"source": "pkg_b", "target": "pkg_a"}]


def test_package_an_unresolvable_import_increments_the_package_level_external_dependency_count():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/a.py", "import os\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg/b.py", "import sys\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    # Both files' external imports roll up onto the single "pkg" node.
    assert result["nodes"] == [{"path": "pkg", "kind": "package", "external_dependency_count": 2}]


def test_package_root_scopes_nodes_to_directories_under_that_path_prefix():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/sub/a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg/subx/b.py", "def b():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, root="pkg/sub", granularity="package")

    assert {node["path"] for node in result["nodes"]} == {"pkg/sub"}


def test_package_import_resolving_outside_the_scoped_root_produces_no_edge_and_is_not_external():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/other/c.py", "def c():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/sub/a.py",
        "from pkg.other.c import c\n\n\nc()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, root="pkg/sub", granularity="package")

    assert result["nodes"] == [{"path": "pkg/sub", "kind": "package", "external_dependency_count": 0}]
    assert result["edges"] == []


def test_package_an_ambiguously_resolving_import_produces_an_edge_to_each_candidate_package():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "vendor_a/pkg/other.py", "def x():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "vendor_b/pkg/other.py", "def x():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "main/entry.py",
        "from pkg.other import x\n\n\nx()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["edges"] == [
        {"source": "main", "target": "vendor_a/pkg"},
        {"source": "main", "target": "vendor_b/pkg"},
    ]


def test_package_node_cap_truncates_directory_count_not_file_count():
    # Two files sharing one directory must count as ONE node against the
    # cap, not two -- node_cap bounds packages, not the underlying files.
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg/b.py", "def b():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package", node_cap=1)

    assert len(result["nodes"]) == 1
    assert result["truncated"] is False


def test_package_node_cap_truncates_when_directory_count_exceeds_it():
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg_a/a.py", "def a():\n    pass\n")
    _index(symbol_store, relation_store, index_meta_store, "pkg_b/b.py", "def b():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package", node_cap=1)

    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["path"] == "pkg_a"
    assert result["truncated"] is True


def test_package_files_in_a_truncated_away_directory_produce_no_edge_and_are_not_external():
    # pkg_b is truncated out of view (node_cap=1, pkg_a sorts first). An
    # import from pkg_a into pkg_b must not appear as an edge, and must not
    # inflate pkg_a's external_dependency_count either -- it resolved
    # in-repo, it's just outside this capped view (same simplification as
    # file granularity's out-of-scope-root case, extended to the cap).
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg_b/target.py", "def helper():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg_a/a.py",
        "from pkg_b.target import helper\n\n\nhelper()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package", node_cap=1)

    assert result["nodes"] == [{"path": "pkg_a", "kind": "package", "external_dependency_count": 0}]
    assert result["edges"] == []


def test_package_full_true_has_no_effect_on_synthetic_package_nodes():
    # Unlike file granularity, a package node is not a real Symbol -- there
    # is no confidence/provenance to reveal.
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/a.py", "def a():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package", full=True)

    assert "confidence" not in result["nodes"][0]
    assert "provenance" not in result["nodes"][0]


def test_package_granularity_is_echoed_in_the_envelope():
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store, granularity="package")

    assert result["granularity"] == "package"


# ---------------------------------------------------------------------------
# architecture() -- v1 slice C5, layering-violation detection.
# ---------------------------------------------------------------------------


def _write_layer_config(tmp_path, payload: dict) -> None:
    acie_dir = tmp_path / ".acie"
    acie_dir.mkdir(exist_ok=True)
    (acie_dir / "config.json").write_text(json.dumps(payload))


def test_no_repo_root_supplied_layering_is_disabled():
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store)

    assert result["layering_enabled"] is False
    assert result["layer_violations"] == []


def test_repo_root_supplied_but_no_acie_config_layering_is_disabled(tmp_path):
    symbol_store, relation_store, index_meta_store = _stores()

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    assert result["layering_enabled"] is False
    assert result["layer_violations"] == []


def test_acie_config_present_with_no_crossing_edges_layering_enabled_no_violations(tmp_path):
    _write_layer_config(
        tmp_path,
        {"layers": {"api": ["pkg/api/*"], "core": ["pkg/core/*"]}, "allowed_dependencies": {}},
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/api/a.py", "def a():\n    pass\n")

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    assert result["layering_enabled"] is True
    assert result["layer_violations"] == []


def test_disallowed_cross_layer_import_is_reported_as_a_violation(tmp_path):
    # api -> core is fine (declared), but core has no declared dependency on
    # api at all, so a core file importing an api file must be flagged.
    _write_layer_config(
        tmp_path,
        {
            "layers": {"api": ["pkg/api/*"], "core": ["pkg/core/*"]},
            "allowed_dependencies": {"api": ["core"]},
        },
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/api/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/core/b.py",
        "from pkg.api.a import a\n\n\na()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    assert result["layer_violations"] == [
        {"source": "pkg/core/b.py", "target": "pkg/api/a.py", "from_layer": "core", "to_layer": "api"}
    ]


def test_allowed_cross_layer_import_produces_no_violation(tmp_path):
    _write_layer_config(
        tmp_path,
        {
            "layers": {"api": ["pkg/api/*"], "core": ["pkg/core/*"]},
            "allowed_dependencies": {"api": ["core"]},
        },
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/core/b.py", "def b():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/api/a.py",
        "from pkg.core.b import b\n\n\nb()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    assert result["layer_violations"] == []


def test_same_layer_import_never_flagged_even_with_no_allowed_dependencies_entry(tmp_path):
    _write_layer_config(tmp_path, {"layers": {"core": ["pkg/core/*"]}, "allowed_dependencies": {}})
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/core/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/core/b.py",
        "from pkg.core.a import a\n\n\na()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    assert result["layer_violations"] == []


def test_edge_where_an_endpoint_matches_no_declared_layer_is_not_a_violation(tmp_path):
    _write_layer_config(tmp_path, {"layers": {"core": ["pkg/core/*"]}, "allowed_dependencies": {}})
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/core/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "other/b.py",
        "from pkg.core.a import a\n\n\na()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    assert result["layer_violations"] == []


def test_ambiguous_layer_classification_fans_out_over_every_disallowed_combination(tmp_path):
    # pkg/shared/a.py matches both "shared" and "restricted" (overlapping
    # globs, a real config-authoring ambiguity) -- classify_layers (C4)
    # never folds this to one guess, and neither does violation detection:
    # every disallowed (from_layer, to_layer) combination is its own entry.
    _write_layer_config(
        tmp_path,
        {
            "layers": {
                "shared": ["pkg/shared/*"],
                "restricted": ["pkg/shared/*"],
                "consumer": ["pkg/consumer/*"],
            },
            "allowed_dependencies": {"consumer": ["shared"]},
        },
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/shared/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/consumer/b.py",
        "from pkg.shared.a import a\n\n\na()\n",
    )

    result = architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))

    # consumer->shared is allowed; consumer->restricted is not declared.
    assert result["layer_violations"] == [
        {
            "source": "pkg/consumer/b.py", "target": "pkg/shared/a.py",
            "from_layer": "consumer", "to_layer": "restricted",
        }
    ]


def test_violations_are_detected_regardless_of_requested_granularity(tmp_path):
    # Per memory b75c92b3: classify_layers was only ever validated at file
    # granularity (a naturally-authored glob like "pkg/api/*" does not
    # match the bare directory string "pkg/api"), so layering-violation
    # detection always classifies at file granularity internally, even
    # when the caller requested granularity="package" for nodes/edges.
    _write_layer_config(
        tmp_path,
        {
            "layers": {"api": ["pkg/api/*"], "core": ["pkg/core/*"]},
            "allowed_dependencies": {"api": ["core"]},
        },
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/api/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/core/b.py",
        "from pkg.api.a import a\n\n\na()\n",
    )

    result = architecture(
        symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path), granularity="package",
    )

    assert result["layer_violations"] == [
        {"source": "pkg/core/b.py", "target": "pkg/api/a.py", "from_layer": "core", "to_layer": "api"}
    ]


def test_violations_are_not_scoped_by_node_cap(tmp_path):
    # node_cap bounds response size for rendered nodes/edges; a layering
    # policy check must not silently miss a real violation just because
    # the caller asked for a small page of nodes.
    _write_layer_config(
        tmp_path,
        {
            "layers": {"api": ["pkg/api/*"], "core": ["pkg/core/*"]},
            "allowed_dependencies": {"api": ["core"]},
        },
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/api/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/core/b.py",
        "from pkg.api.a import a\n\n\na()\n",
    )

    result = architecture(
        symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path), node_cap=1,
    )

    assert result["layer_violations"] == [
        {"source": "pkg/core/b.py", "target": "pkg/api/a.py", "from_layer": "core", "to_layer": "api"}
    ]


def test_violations_respect_root_scope(tmp_path):
    # A disallowed edge whose endpoints are both outside root's scope is
    # not in `in_scope` at all, so it produces no violation -- root scoping
    # applies to layering detection the same way it applies to nodes/edges.
    _write_layer_config(
        tmp_path,
        {
            "layers": {"api": ["pkg/api/*"], "core": ["pkg/core/*"]},
            "allowed_dependencies": {"api": ["core"]},
        },
    )
    symbol_store, relation_store, index_meta_store = _stores()
    _index(symbol_store, relation_store, index_meta_store, "pkg/api/a.py", "def a():\n    pass\n")
    _index(
        symbol_store, relation_store, index_meta_store, "pkg/core/b.py",
        "from pkg.api.a import a\n\n\na()\n",
    )

    result = architecture(
        symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path), root="other",
    )

    assert result["layer_violations"] == []


def test_malformed_acie_config_raises_invalid_config_error(tmp_path):
    acie_dir = tmp_path / ".acie"
    acie_dir.mkdir()
    (acie_dir / "config.json").write_text("not valid json")
    symbol_store, relation_store, index_meta_store = _stores()

    try:
        architecture(symbol_store, relation_store, index_meta_store, repo_root=str(tmp_path))
        assert False, "expected InvalidConfigError"
    except InvalidConfigError:
        pass
