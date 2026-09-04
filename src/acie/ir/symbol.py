from dataclasses import dataclass
from enum import Enum


class Confidence(str, Enum):
    """Shared confidence taxonomy for Symbols and Relations.

    See ARCHITECTURE.md "Provenance & Confidence Semantics".
    """

    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"

_CONFIDENCE_RANK = {Confidence.EXTRACTED: 0, Confidence.INFERRED: 1, Confidence.AMBIGUOUS: 2}


def confidence_rank(confidence: Confidence) -> int:
    """Returns the shared filter/merge rank for a Confidence.

    This ordering serves `tools.confidence.py`'s min-confidence filter and
    `daemon.merge_policy.py`'s regression guard. It is a filter/merge
    ordering only, not a reinterpretation of ARCHITECTURE.md's explicitly
    non-ordinal confidence taxonomy.
    """
    return _CONFIDENCE_RANK[confidence]


@dataclass(frozen=True)
class Provenance:
    """Which tool observed a fact, that tool's version, and when."""

    provider: str
    version: str
    observed_at: str  # ISO 8601 timestamp


@dataclass(frozen=True)
class Symbol:
    id: str
    path: str
    qualname: str
    kind: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    confidence: Confidence
    provenance: Provenance
