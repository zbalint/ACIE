"""Shared min_confidence filtering for the 3 tools whose results carry
genuinely graded per-item confidence (find_symbol, get_definition,
find_references) -- see ARCHITECTURE.md "Provenance & Confidence
Semantics" and DAEMON.md's "Deferred to Implementation" for why this was
missing from v0's shipped tool signatures despite being named there.

ARCHITECTURE.md's confidence taxonomy is explicitly non-ordinal (no
low/medium/high/very-high scale, no numeric probability) -- but a
"min_confidence" filter parameter, by its name, implies an ordering. This
module resolves that gap with a filter-only rank, not a reinterpretation
of the taxonomy itself: EXTRACTED (most certain) < INFERRED < AMBIGUOUS
(least certain). `min_confidence=X` keeps only items whose confidence is
at least as certain as X. In v0 practice this is close to a binary
EXTRACTED-only/everything switch, since INFERRED is reserved-but-unused
until v1's LSP layer.
"""

from typing import Callable, TypeVar

from acie.ir.symbol import Confidence
from acie.tools.errors import InvalidArgumentError

T = TypeVar("T")

# Index = certainty rank, most certain first -- filter-only ordering, see
# module docstring for why this doesn't reopen the ordinal-taxonomy
# question ARCHITECTURE.md already settled against.
_RANK = {Confidence.EXTRACTED: 0, Confidence.INFERRED: 1, Confidence.AMBIGUOUS: 2}


def filter_by_min_confidence(
    items: list[T], min_confidence: str | Confidence | None, *, key: Callable[[T], Confidence] | None = None
) -> list[T]:
    """Keeps only items at least as certain as min_confidence. None is a no-op.

    `key` extracts a Confidence from an item; defaults to `item.confidence`
    (Symbol and Relation's own attribute name), overridable for callers
    whose items name it differently.
    """
    if min_confidence is None:
        return items
    if isinstance(min_confidence, Confidence):
        floor = min_confidence
    else:
        try:
            floor = Confidence(min_confidence)
        except ValueError:
            raise InvalidArgumentError(f"min_confidence must be one of {[c.value for c in Confidence]}, got {min_confidence!r}")

    get_confidence = key if key is not None else (lambda item: item.confidence)
    floor_rank = _RANK[floor]
    return [item for item in items if _RANK[get_confidence(item)] <= floor_rank]
