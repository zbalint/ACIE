from dataclasses import dataclass

from acie.ir.symbol import Confidence, Provenance


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    predicate: str
    site_file: str
    site_line: int
    site_col: int
    confidence: Confidence
    provenance: Provenance
