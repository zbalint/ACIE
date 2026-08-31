import subprocess

from acie.indexer import index_file
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
