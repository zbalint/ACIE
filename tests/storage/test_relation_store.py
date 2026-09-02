from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance
from acie.storage.relation_store import RelationStore


def _key(relation: Relation) -> dict:
    return dict(
        source=relation.source,
        target=relation.target,
        predicate=relation.predicate,
        site_file=relation.site_file,
        site_line=relation.site_line,
        site_col=relation.site_col,
    )


def make_relation(**overrides) -> Relation:
    defaults = dict(
        source="pkg/mod.py:foo#function",
        target="pkg/other.py:bar#function",
        predicate="calls",
        site_file="pkg/mod.py",
        site_line=5,
        site_col=4,
        confidence=Confidence.EXTRACTED,
        provenance=Provenance(
            provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T00:00:00Z"
        ),
    )
    defaults.update(overrides)
    return Relation(**defaults)


def test_overrides_predicate_is_accepted_by_the_schema():
    store = RelationStore()
    relation = make_relation(
        source="pkg/mod.py:Foo.bar#method",
        target="pkg/mod.py:Base.bar#method",
        predicate="overrides",
    )

    store.upsert(relation)

    assert store.get(**_key(relation)) == relation


def test_opening_a_pre_existing_relations_live_table_without_overrides_migrates_it_in_place():
    # Regression (code review, 2026-09-02): CREATE TABLE IF NOT EXISTS
    # (_SCHEMA) is a no-op against a real pre-existing index.sqlite from
    # before 'overrides' was added to the predicate CHECK constraint --
    # every such repo would otherwise reject every overrides relation with
    # a CHECK-constraint IntegrityError forever. Same lazy-migration idiom
    # as IndexMetaStore's head_sha/cross_file_pass_done regressions.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS relations_live (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            predicate TEXT NOT NULL CHECK (predicate IN ('imports', 'calls', 'references', 'defines', 'inherits')),
            site_file TEXT NOT NULL,
            site_line INTEGER NOT NULL,
            site_col INTEGER NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
            provenance_provider TEXT NOT NULL,
            provenance_version TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source, target, predicate, site_file, site_line, site_col)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO relations_live (
            source, target, predicate, site_file, site_line, site_col,
            confidence, provenance_provider, provenance_version, observed_at
        ) VALUES (
            'pkg/mod.py:foo#function', 'pkg/other.py:bar#function', 'calls',
            'pkg/mod.py', 5, 4, 'EXTRACTED', 'tree-sitter', '0.25.0', '2026-08-31T00:00:00Z'
        )
        """
    )
    conn.commit()

    store = RelationStore(conn=conn)  # must not raise OperationalError/IntegrityError

    # Pre-existing row survived the rebuild untouched.
    preexisting = make_relation()
    assert store.get(**_key(preexisting)) == preexisting

    # The whole point: an overrides relation is now accepted against the
    # migrated table.
    overrides = make_relation(
        source="pkg/mod.py:Foo.bar#method",
        target="pkg/mod.py:Base.bar#method",
        predicate="overrides",
    )
    store.upsert(overrides)
    assert store.get(**_key(overrides)) == overrides


def test_migration_self_heals_after_a_crash_leaves_a_partially_migrated_temp_table_behind():
    # Regression (agy/gemini review, 2026-09-02): the migration's `with
    # self._conn:` block does NOT protect its own leading CREATE TABLE --
    # Python's legacy sqlite3 transaction handling only auto-opens an
    # implicit transaction before the first DML statement (the INSERT), so a
    # standalone leading CREATE TABLE auto-commits immediately, independent
    # of anything that happens (or crashes) afterward. Empirically verified
    # outside this test: a crash between that CREATE and the final RENAME
    # leaves relations_live__migrating committed on disk (no data loss --
    # the original table's own DROP/RENAME never commits -- but the leftover
    # temp table blocks retrying the CREATE step on next startup, an
    # OperationalError that previously wedged RelationStore.__init__
    # permanently). The fix must be idempotent: a leftover
    # relations_live__migrating from an interrupted prior attempt must not
    # prevent this constructor from completing the migration.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS relations_live (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            predicate TEXT NOT NULL CHECK (predicate IN ('imports', 'calls', 'references', 'defines', 'inherits')),
            site_file TEXT NOT NULL,
            site_line INTEGER NOT NULL,
            site_col INTEGER NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
            provenance_provider TEXT NOT NULL,
            provenance_version TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source, target, predicate, site_file, site_line, site_col)
        );
        -- Simulates a prior interrupted migration attempt: its leading
        -- CREATE TABLE committed (per the mechanism above) before a crash
        -- prevented the rest of that attempt from finishing.
        CREATE TABLE relations_live__migrating (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            predicate TEXT NOT NULL CHECK (predicate IN ('imports', 'calls', 'references', 'defines', 'inherits', 'overrides')),
            site_file TEXT NOT NULL,
            site_line INTEGER NOT NULL,
            site_col INTEGER NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
            provenance_provider TEXT NOT NULL,
            provenance_version TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source, target, predicate, site_file, site_line, site_col)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO relations_live (
            source, target, predicate, site_file, site_line, site_col,
            confidence, provenance_provider, provenance_version, observed_at
        ) VALUES (
            'pkg/mod.py:foo#function', 'pkg/other.py:bar#function', 'calls',
            'pkg/mod.py', 5, 4, 'EXTRACTED', 'tree-sitter', '0.25.0', '2026-08-31T00:00:00Z'
        )
        """
    )
    conn.commit()

    store = RelationStore(conn=conn)  # must not raise OperationalError (self-heal)

    preexisting = make_relation()
    assert store.get(**_key(preexisting)) == preexisting

    overrides = make_relation(
        source="pkg/mod.py:Foo.bar#method",
        target="pkg/mod.py:Base.bar#method",
        predicate="overrides",
    )
    store.upsert(overrides)
    assert store.get(**_key(overrides)) == overrides


def test_conn_kwarg_reuses_an_already_open_connection_instead_of_opening_its_own():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    writer = RelationStore(conn=conn)
    relation = make_relation()
    writer.upsert(relation)

    reader = RelationStore(conn=conn)

    assert reader.get(**_key(relation)) == relation


def test_upsert_then_get_round_trips_relation():
    store = RelationStore(":memory:")
    relation = make_relation()

    store.upsert(relation)

    assert store.get(**_key(relation)) == relation


def test_first_upsert_creates_one_history_observation():
    store = RelationStore(":memory:")
    relation = make_relation()

    store.upsert(relation)

    assert store.history(**_key(relation)) == [relation]


def test_reupsert_with_identical_content_does_not_grow_history():
    store = RelationStore(":memory:")
    relation = make_relation()
    store.upsert(relation)

    reobserved = make_relation(
        provenance=Provenance(
            provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T01:00:00Z"
        )
    )
    store.upsert(reobserved)

    assert store.get(**_key(relation)) == reobserved
    assert store.history(**_key(relation)) == [relation]


def test_reupsert_with_upgraded_confidence_appends_second_history_entry():
    store = RelationStore(":memory:")
    relation = make_relation(confidence=Confidence.AMBIGUOUS)
    store.upsert(relation)

    resolved = make_relation(
        confidence=Confidence.EXTRACTED,
        provenance=Provenance(
            provider="tree-sitter", version="0.25.0", observed_at="2026-08-31T01:00:00Z"
        ),
    )
    store.upsert(resolved)

    assert store.get(**_key(relation)) == resolved
    assert store.history(**_key(relation)) == [relation, resolved]


def test_delete_hard_deletes_live_row_and_writes_tombstone():
    store = RelationStore(":memory:")
    relation = make_relation()
    store.upsert(relation)

    store.delete(**_key(relation), observed_at="2026-08-31T02:00:00Z")

    assert store.get(**_key(relation)) is None
    assert store.history(**_key(relation)) == [relation]
    assert store.is_tombstoned(**_key(relation))


def test_list_by_site_file_returns_only_live_relations_sited_in_that_file():
    store = RelationStore(":memory:")
    same_file = make_relation()
    other_file = make_relation(
        source="pkg/other.py:foo#function",
        site_file="pkg/other.py",
    )
    store.upsert(same_file)
    store.upsert(other_file)

    assert store.list_by_site_file("pkg/mod.py") == [same_file]


def test_list_by_site_file_filters_by_predicates_when_given():
    store = RelationStore(":memory:")
    call = make_relation(predicate="calls")
    imports = make_relation(
        target="os.path", predicate="imports", site_line=6, site_col=0,
    )
    store.upsert(call)
    store.upsert(imports)

    result = store.list_by_site_file("pkg/mod.py", predicates={"imports"})
    assert result == [imports]


def test_list_by_site_returns_all_live_relations_at_the_exact_site():
    store = RelationStore(":memory:")
    at_site = make_relation()
    elsewhere = make_relation(target="pkg/other.py:baz#function", site_line=6)
    store.upsert(at_site)
    store.upsert(elsewhere)

    assert store.list_by_site(site_file="pkg/mod.py", site_line=5, site_col=4) == [at_site]


def test_list_by_site_returns_multiple_rows_for_an_ambiguous_site():
    store = RelationStore(":memory:")
    candidate_a = make_relation(target="pkg/other.py:bar#function", confidence=Confidence.AMBIGUOUS)
    candidate_b = make_relation(target="pkg/third.py:bar#function", confidence=Confidence.AMBIGUOUS)
    store.upsert(candidate_a)
    store.upsert(candidate_b)

    result = store.list_by_site(site_file="pkg/mod.py", site_line=5, site_col=4)
    assert {r.target for r in result} == {"pkg/other.py:bar#function", "pkg/third.py:bar#function"}


def test_list_by_site_filters_by_predicates_when_given():
    store = RelationStore(":memory:")
    call = make_relation(predicate="calls")
    imports = make_relation(
        target="os.path", predicate="imports", site_line=5, site_col=4,
    )
    store.upsert(call)
    store.upsert(imports)

    result = store.list_by_site(
        site_file="pkg/mod.py", site_line=5, site_col=4, predicates={"calls", "references", "inherits"},
    )
    assert result == [call]


def test_list_by_site_returns_empty_when_nothing_matches():
    store = RelationStore(":memory:")
    store.upsert(make_relation())

    assert store.list_by_site(site_file="pkg/mod.py", site_line=999, site_col=0) == []


def test_list_by_target_returns_all_live_relations_targeting_that_symbol():
    store = RelationStore(":memory:")
    at_target = make_relation()
    elsewhere = make_relation(target="pkg/other.py:baz#function")
    store.upsert(at_target)
    store.upsert(elsewhere)

    assert store.list_by_target("pkg/other.py:bar#function") == [at_target]


def test_list_by_target_filters_by_predicates_when_given():
    store = RelationStore(":memory:")
    call = make_relation(predicate="calls")
    defines = make_relation(predicate="defines", site_line=1, site_col=0)
    store.upsert(call)
    store.upsert(defines)

    result = store.list_by_target(
        "pkg/other.py:bar#function", predicates={"calls", "references", "inherits"},
    )
    assert result == [call]


def test_list_by_target_returns_empty_when_nothing_matches():
    store = RelationStore(":memory:")
    store.upsert(make_relation())

    assert store.list_by_target("pkg/nope.py:nope#function") == []


def test_list_by_source_returns_all_live_relations_sourced_at_that_symbol():
    store = RelationStore(":memory:")
    from_source = make_relation()
    elsewhere = make_relation(source="pkg/other.py:baz#function")
    store.upsert(from_source)
    store.upsert(elsewhere)

    assert store.list_by_source("pkg/mod.py:foo#function") == [from_source]


def test_list_by_source_filters_by_predicates_when_given():
    store = RelationStore(":memory:")
    call = make_relation(predicate="calls")
    defines = make_relation(predicate="defines", target="pkg/other.py:qux#function", site_line=1, site_col=0)
    store.upsert(call)
    store.upsert(defines)

    result = store.list_by_source(
        "pkg/mod.py:foo#function", predicates={"calls", "references", "inherits"},
    )
    assert result == [call]


def test_list_by_source_returns_empty_when_nothing_matches():
    store = RelationStore(":memory:")
    store.upsert(make_relation())

    assert store.list_by_source("pkg/nope.py:nope#function") == []
