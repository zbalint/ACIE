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
