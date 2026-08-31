from dataclasses import dataclass
from enum import Enum


class Confidence(str, Enum):
    """Shared confidence taxonomy for Symbols and Relations.

    See ARCHITECTURE.md "Provenance & Confidence Semantics".
    """

    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


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
