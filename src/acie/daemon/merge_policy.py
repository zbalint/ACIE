"""Write-time merge rules for LSP enrichment relations."""

import logging
from dataclasses import dataclass

from acie.ir.relation import Relation
from acie.ir.symbol import Confidence, confidence_rank
from acie.storage.relation_store import RelationStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeOutcome:
    applied: bool
    retired_siblings: int
    reason: str | None = None


def apply_enrichment_write(relation_store: RelationStore, relation: Relation) -> MergeOutcome:
    """Apply an enrichment relation without regressing a more-certain fact."""
    existing = relation_store.get(
        source=relation.source,
        target=relation.target,
        predicate=relation.predicate,
        site_file=relation.site_file,
        site_line=relation.site_line,
        site_col=relation.site_col,
    )
    if existing is not None and confidence_rank(relation.confidence) > confidence_rank(existing.confidence):
        _logger.warning(
            "Refusing enrichment write at %s:%s:%s: incoming confidence %s would regress existing %s",
            relation.site_file,
            relation.site_line,
            relation.site_col,
            relation.confidence.value,
            existing.confidence.value,
        )
        return MergeOutcome(applied=False, retired_siblings=0, reason="would_regress_existing_confidence")

    relation_store.upsert(relation)
    retired_siblings = _retire_stale_siblings(relation_store, relation)
    return MergeOutcome(applied=True, retired_siblings=retired_siblings)


def _retire_stale_siblings(relation_store: RelationStore, relation: Relation) -> int:
    siblings = relation_store.list_by_site(
        site_file=relation.site_file,
        site_line=relation.site_line,
        site_col=relation.site_col,
        predicates={relation.predicate},
    )
    stale_siblings = [
        sibling
        for sibling in siblings
        if sibling.source == relation.source
        and sibling.target != relation.target
        and sibling.confidence == Confidence.AMBIGUOUS
    ]
    # shortcut: do not reconcile stale INFERRED rows from cross-pass re-ambiguation;
    # retire them if a real repo exposes a visibly wrong stale fact.
    # shortcut: do not wrap upsert and sibling retirement in one transaction;
    # add transaction support if a crash leaves a stale sibling beyond the next pass.
    for sibling in stale_siblings:
        relation_store.delete(
            source=sibling.source,
            target=sibling.target,
            predicate=sibling.predicate,
            site_file=sibling.site_file,
            site_line=sibling.site_line,
            site_col=sibling.site_col,
            observed_at=relation.provenance.observed_at,
        )
    return len(stale_siblings)
