import subprocess

from acie.indexer import index_file
from acie.ir.symbol import Confidence
from acie.repo_id import resolve_index_db_path
from acie.storage.index_meta_store import IndexMetaStore
from acie.storage.relation_store import RelationStore
from acie.storage.symbol_store import SymbolStore


def test_indexing_a_new_file_upserts_its_symbols_and_relations():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    source = "def foo():\n    pass\n"

    result = index_file(
        path="pkg/mod.py",
        source_text=source,
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store,
        relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert result.skipped is False
    module = symbol_store.get("pkg/mod.py:#module")
    foo = symbol_store.get("pkg/mod.py:foo#function")
    assert module is not None
    assert foo is not None
    defines = relation_store.get(
        source=module.id, target=foo.id, predicate="defines",
        site_file="pkg/mod.py", site_line=foo.start_line, site_col=foo.start_col,
    )
    assert defines is not None


def test_reindexing_unchanged_source_does_not_grow_history():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    source = "def foo():\n    pass\n"

    index_file(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="pkg/mod.py", source_text=source, observed_at="2026-08-31T01:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    foo_id = "pkg/mod.py:foo#function"
    assert len(symbol_store.history(foo_id)) == 1
    module = symbol_store.get("pkg/mod.py:#module")
    defines_key = dict(
        source=module.id, target=foo_id, predicate="defines",
        site_file="pkg/mod.py", site_line=1, site_col=0,
    )
    assert len(relation_store.history(**defines_key)) == 1


def test_removing_a_symbol_from_source_tombstones_it_and_its_relations():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    source_with_foo = "def foo():\n    pass\n"
    source_without_foo = ""

    index_file(
        path="pkg/mod.py", source_text=source_with_foo, observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    foo_id = "pkg/mod.py:foo#function"
    module_id = "pkg/mod.py:#module"
    defines_key = dict(
        source=module_id, target=foo_id, predicate="defines",
        site_file="pkg/mod.py", site_line=1, site_col=0,
    )
    assert symbol_store.get(foo_id) is not None
    assert relation_store.get(**defines_key) is not None

    result = index_file(
        path="pkg/mod.py", source_text=source_without_foo, observed_at="2026-08-31T01:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert result.symbols_tombstoned == 1
    assert result.relations_tombstoned == 1
    assert symbol_store.get(foo_id) is None
    assert symbol_store.is_tombstoned(foo_id)
    assert relation_store.get(**defines_key) is None
    assert relation_store.is_tombstoned(**defines_key)
    assert symbol_store.get(module_id) is not None


def test_reindexing_malformed_source_skips_and_preserves_prior_state():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    good_source = "def foo():\n    pass\n"
    malformed_source = "def foo(\n"

    index_file(
        path="pkg/mod.py", source_text=good_source, observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    foo_id = "pkg/mod.py:foo#function"
    foo_before = symbol_store.get(foo_id)

    result = index_file(
        path="pkg/mod.py", source_text=malformed_source, observed_at="2026-08-31T01:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert result.skipped is True
    assert result.symbols_upserted == 0
    assert result.symbols_tombstoned == 0
    assert result.relations_upserted == 0
    assert result.relations_tombstoned == 0
    assert symbol_store.get(foo_id) == foo_before
    assert not symbol_store.is_tombstoned(foo_id)


def test_first_time_indexing_malformed_source_skips_with_no_rows_created():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")

    result = index_file(
        path="pkg/mod.py", source_text="def foo(\n", observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert result.skipped is True
    assert symbol_store.get("pkg/mod.py:#module") is None
    assert symbol_store.list_by_path("pkg/mod.py") == []


def test_localized_syntax_error_in_one_function_still_freezes_the_whole_file():
    """Pins the whole-file skip granularity at realistic scale (slice 7
    follow-up e3414030): a small, localized error inside one function among
    several -- not a tiny single-def source -- must still skip the entire
    file's reindex, leaving even untouched, individually well-formed
    functions (alpha, gamma) exactly as they were, not selectively
    refreshed. This is the locked v0 tradeoff (determinism/simplicity over
    per-region recovery); a scoped alternative is deferred, not adopted.
    """
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    good_source = (
        "def alpha():\n    pass\n\n"
        "def beta():\n    return 1\n\n"
        "def gamma():\n    return alpha() + beta()\n"
    )
    # A single missing ")" inside beta only -- alpha and gamma are each
    # individually well-formed and untouched by the typo.
    source_with_localized_error = (
        "def alpha():\n    pass\n\n"
        "def beta(:\n    return 1\n\n"
        "def gamma():\n    return alpha() + beta()\n"
    )

    index_file(
        path="pkg/mod.py", source_text=good_source, observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    alpha_before = symbol_store.get("pkg/mod.py:alpha#function")
    beta_before = symbol_store.get("pkg/mod.py:beta#function")
    gamma_before = symbol_store.get("pkg/mod.py:gamma#function")
    assert alpha_before is not None
    assert beta_before is not None
    assert gamma_before is not None

    result = index_file(
        path="pkg/mod.py", source_text=source_with_localized_error, observed_at="2026-08-31T01:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert result.skipped is True
    assert symbol_store.get("pkg/mod.py:alpha#function") == alpha_before
    assert symbol_store.get("pkg/mod.py:beta#function") == beta_before
    assert symbol_store.get("pkg/mod.py:gamma#function") == gamma_before


def test_index_file_bumps_index_generation_on_successful_reindex():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    assert index_meta_store.current_generation() == 0

    index_file(
        path="pkg/mod.py", source_text="def foo():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert index_meta_store.current_generation() == 1


def test_index_file_does_not_bump_generation_when_skipped_for_malformed_source():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")

    result = index_file(
        path="pkg/mod.py", source_text="def foo(\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert result.skipped is True
    assert index_meta_store.current_generation() == 0


def test_cross_file_imported_call_resolves_once_the_callee_file_is_indexed():
    """Closes the generation-102 evaluation's cross-module-call gap
    (SALTMDB 98c4e550): a bare call to a name imported from another module
    resolves to a real `calls` edge once that module's file is in the same
    SymbolStore -- callee indexed first, matching bootstrap's own walk-then-
    resolve order within one file's index_file call for the already-indexed
    case (the arbitrary-order/not-yet-indexed case is covered separately
    below).
    """
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")

    index_file(
        path="pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="pkg/mod.py",
        source_text="from pkg.other import helper\n\n\nhelper()\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    calls = relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(calls) == 1
    assert calls[0].source == "pkg/mod.py:#module"
    assert calls[0].target == "pkg/other.py:helper#function"
    assert calls[0].confidence == Confidence.EXTRACTED


def test_cross_file_imported_call_stays_unresolved_when_the_callee_is_indexed_first_the_other_way_around():
    """Pins the known, accepted ordering limitation the plan calls out:
    indexing the *caller* before the callee leaves the call unresolved --
    there is no retarget-in-place primitive, so nothing retroactively fixes
    an already-written miss. Re-indexing the caller after the callee exists
    (e.g. bootstrap's second pass, or the watcher touching the caller again)
    is what closes it, proven here by literally reindexing the caller.
    """
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")

    index_file(
        path="pkg/mod.py",
        source_text="from pkg.other import helper\n\n\nhelper()\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    assert relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"}) == []

    index_file(
        path="pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:01:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    assert relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"}) == []

    index_file(
        path="pkg/mod.py",
        source_text="from pkg.other import helper\n\n\nhelper()\n",
        observed_at="2026-08-31T00:02:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    calls = relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(calls) == 1
    assert calls[0].target == "pkg/other.py:helper#function"


def test_cross_file_call_matching_two_same_suffix_module_paths_is_ambiguous():
    """Two files whose dotted-path suffix both match the imported module
    path (e.g. two vendored `pkg/other.py` copies under different roots) --
    genuinely ambiguous, since ACIE has no PYTHONPATH/source-root config to
    disambiguate them, so both surface as AMBIGUOUS candidates rather than
    guessing one.
    """
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")

    index_file(
        path="vendor_a/pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="vendor_b/pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="pkg/mod.py",
        source_text="from pkg.other import helper\n\n\nhelper()\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    calls = relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(calls) == 2
    targets = {r.target for r in calls}
    assert targets == {"vendor_a/pkg/other.py:helper#function", "vendor_b/pkg/other.py:helper#function"}
    assert all(r.confidence == Confidence.AMBIGUOUS for r in calls)


def test_removing_a_callee_symbol_tombstones_a_cross_file_call_edge_into_it():
    """Codex review finding (P1): index_file's diffing only ever removed
    relations sited in the file being reindexed -- correct pre-Slice-C,
    since every relation's target used to live in that same file. Slice C
    makes a `calls` edge's site (caller's file) and target (callee's file)
    genuinely different files, so removing/renaming the callee must also
    invalidate the caller-sited edge into it, even though the caller's own
    file was never touched by this reindex.
    """
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    index_file(
        path="pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="pkg/mod.py",
        source_text="from pkg.other import helper\n\n\nhelper()\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    assert len(relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})) == 1

    # Rename helper -> renamed in the callee's own file. Per ACIE's
    # deterministic-recompute identity model, this is "delete the old
    # symbol id" -- pkg/mod.py itself is never reindexed here.
    result = index_file(
        path="pkg/other.py", source_text="def renamed():\n    pass\n",
        observed_at="2026-08-31T00:01:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    assert symbol_store.get("pkg/other.py:helper#function") is None
    stale_calls = relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert stale_calls == []
    assert result.relations_tombstoned >= 1


def test_removing_a_callee_symbol_does_not_disturb_an_unrelated_ambiguous_sibling_edge():
    symbol_store = SymbolStore(":memory:")
    relation_store = RelationStore(":memory:")
    index_meta_store = IndexMetaStore(":memory:")
    index_file(
        path="vendor_a/pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="vendor_b/pkg/other.py", source_text="def helper():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    index_file(
        path="pkg/mod.py",
        source_text="from pkg.other import helper\n\n\nhelper()\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    assert len(relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})) == 2

    index_file(
        path="vendor_a/pkg/other.py", source_text="",
        observed_at="2026-08-31T00:01:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )

    remaining = relation_store.list_by_site_file("pkg/mod.py", predicates={"calls"})
    assert len(remaining) == 1
    assert remaining[0].target == "vendor_b/pkg/other.py:helper#function"


def test_index_file_persists_to_a_real_on_disk_index_for_a_resolved_repo(tmp_path):
    """Closes slice 7 follow-up e3414030's roadmap-gap half: proves
    index_file + SymbolStore/RelationStore actually work end-to-end against
    a real ~/.acie/repos/<repo-id>/index.sqlite path (both stores sharing
    one on-disk file, per ARCHITECTURE.md), not just :memory:.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    acie_home = tmp_path / "acie-home"

    db_path = resolve_index_db_path(str(repo), base_dir=str(acie_home))
    assert db_path is not None

    symbol_store = SymbolStore(db_path)
    relation_store = RelationStore(db_path)
    index_meta_store = IndexMetaStore(db_path)
    result = index_file(
        path="pkg/mod.py", source_text="def foo():\n    pass\n",
        observed_at="2026-08-31T00:00:00Z",
        symbol_store=symbol_store, relation_store=relation_store,
        index_meta_store=index_meta_store,
    )
    assert result.skipped is False

    # Fresh store instances reopened against the same on-disk file -- this
    # is the actual proof the data persisted to disk, not just in-process.
    reopened_symbols = SymbolStore(db_path)
    reopened_relations = RelationStore(db_path)
    assert reopened_symbols.get("pkg/mod.py:foo#function") is not None
    assert reopened_relations.get(
        source="pkg/mod.py:#module", target="pkg/mod.py:foo#function", predicate="defines",
        site_file="pkg/mod.py", site_line=1, site_col=0,
    ) is not None
