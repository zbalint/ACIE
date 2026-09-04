import logging

from acie.daemon.merge_policy import MergeOutcome, apply_enrichment_write
from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, Provenance
from acie.storage.relation_store import RelationStore


def make_relation(*, target="target", confidence=Confidence.INFERRED, source="source", predicate="calls"):
    return Relation(
        source=source,
        target=target,
        predicate=predicate,
        site_file="pkg/caller.py",
        site_line=4,
        site_col=2,
        confidence=confidence,
        provenance=Provenance("pyright", "1", "2026-09-04T12:00:00Z"),
    )


def _key(relation: Relation) -> dict:
    return {
        "source": relation.source,
        "target": relation.target,
        "predicate": relation.predicate,
        "site_file": relation.site_file,
        "site_line": relation.site_line,
        "site_col": relation.site_col,
    }


def test_refuses_write_that_regresses_extracted_relation(caplog):
    store = RelationStore(":memory:")
    existing = make_relation(confidence=Confidence.EXTRACTED)
    incoming = make_relation(confidence=Confidence.INFERRED)
    store.upsert(existing)

    with caplog.at_level(logging.WARNING):
        outcome = apply_enrichment_write(store, incoming)

    assert outcome == MergeOutcome(False, 0, "would_regress_existing_confidence")
    assert store.get(**_key(existing)) == existing
    assert store.list_by_site(site_file=existing.site_file, site_line=existing.site_line, site_col=existing.site_col) == [existing]
    assert "would regress existing EXTRACTED" in caplog.text


def test_applies_write_over_ambiguous_relation():
    store = RelationStore(":memory:")
    existing = make_relation(confidence=Confidence.AMBIGUOUS)
    incoming = make_relation(confidence=Confidence.INFERRED)
    store.upsert(existing)

    outcome = apply_enrichment_write(store, incoming)

    assert outcome == MergeOutcome(True, 0)
    assert store.get(**_key(incoming)) == incoming


def test_applies_idempotent_inferred_reobservation():
    store = RelationStore(":memory:")
    existing = make_relation(confidence=Confidence.INFERRED, target="target")
    incoming = make_relation(confidence=Confidence.INFERRED, target="target")
    store.upsert(existing)

    outcome = apply_enrichment_write(store, incoming)

    assert outcome == MergeOutcome(True, 0)
    assert store.get(**_key(incoming)) == incoming


def test_applies_write_with_no_existing_relation():
    store = RelationStore(":memory:")

    outcome = apply_enrichment_write(store, make_relation())

    assert outcome == MergeOutcome(True, 0)


def test_retires_same_site_ambiguous_sibling_and_records_tombstone():
    store = RelationStore(":memory:")
    winner = make_relation(target="winner", confidence=Confidence.INFERRED)
    stale = make_relation(target="stale", confidence=Confidence.AMBIGUOUS)
    store.upsert(stale)

    outcome = apply_enrichment_write(store, winner)

    assert outcome == MergeOutcome(True, 1)
    assert store.get(**_key(stale)) is None
    assert store.is_tombstoned(**_key(stale))
    site_relations = store.list_by_site(
        site_file=stale.site_file, site_line=stale.site_line, site_col=stale.site_col, predicates={stale.predicate}
    )
    assert stale not in site_relations
    assert stale not in store.list_by_site_file(stale.site_file, predicates={stale.predicate})
    assert store.get(**_key(winner)) == winner


def test_does_not_retire_same_target_ambiguous_row():
    store = RelationStore(":memory:")
    existing = make_relation(target="winner", confidence=Confidence.AMBIGUOUS)
    incoming = make_relation(target="winner", confidence=Confidence.INFERRED)
    store.upsert(existing)

    outcome = apply_enrichment_write(store, incoming)

    assert outcome == MergeOutcome(True, 0)
    assert store.get(**_key(incoming)) == incoming
    assert not store.is_tombstoned(**_key(incoming))


def test_does_not_retire_different_source_sibling():
    store = RelationStore(":memory:")
    winner = make_relation(target="winner", confidence=Confidence.INFERRED)
    other_source = make_relation(target="stale", confidence=Confidence.AMBIGUOUS, source="other-source")
    store.upsert(other_source)

    outcome = apply_enrichment_write(store, winner)

    assert outcome == MergeOutcome(True, 0)
    assert store.get(**_key(other_source)) == other_source


def test_does_not_retire_cross_pass_inferred_sibling():
    store = RelationStore(":memory:")
    winner = make_relation(target="winner", confidence=Confidence.INFERRED)
    prior_winner = make_relation(target="prior", confidence=Confidence.INFERRED)
    store.upsert(prior_winner)

    outcome = apply_enrichment_write(store, winner)

    assert outcome == MergeOutcome(True, 0)
    assert store.get(**_key(prior_winner)) == prior_winner
