# Capability H, Slice H3 — Protocol Stub → Concrete Implementation Resolution

## Status: PLACEHOLDER — scoped only, not designed

Filed during the H2 grilling session (2026-09-05, SALTMDB memory
`f93f67cb`) to record a real gap discovered while grounding H2, and to give
it a name so it isn't silently dropped or re-discovered from scratch later.
Nothing below is a design — it's the problem statement and why it's
separate from H2, no more.

## The gap this exists to close

`febf3e07`'s original motivating example: SALTMDB's `EntitiesMixin` calls
`self.send_json()` 56+ times, `send_json` isn't defined on `EntitiesMixin`
or anywhere in its own base chain in the naive sense — except SALTMDB's
current live source has since grown a `TYPE_CHECKING`-gated `Protocol` base
(`ViewerHandlerProtocol` in `saltmdb/viewer/routes/_protocol.py`), added
for an unrelated mypy fix (see H2 spec's "The bug this closes" section for
the grounded detail). Once H1 lands, `self.send_json()` will resolve to
`ViewerHandlerProtocol.send_json` — a real, correctly-resolved edge, but a
**stub** (`def send_json(self, data, status: int = 200) -> None: ...`),
not the concrete `ViewerHandlerBase.send_json` implementation that
`find_references`/callers actually want.

H3's job: given a call resolved to a `Protocol` (or `abstractmethod`) stub,
find the concrete class(es) that structurally implement that stub, and
prefer/union that implementation over the stub as the resolved target.

## Why this is not just H2 with a different trigger

Considered and rejected during H2's grilling session (see H2 spec §1):
broadening H2's precondition to also catch "H1 found a stub" would require
building the exact same stub-vs-concrete ranking logic H3 needs anyway,
just gated on a narrower trigger (H1-found-a-stub, specifically) — H3 needs
the identical arbitration for stubs found any other way (e.g. a plain
`abstractmethod` with no Protocol/TYPE_CHECKING involvement at all,
already-resolved unambiguous inheritance where the base happens to be a
stub, etc.). Building it once here avoids the duplication.

The harder part, not present in H1 or H2 at all: Protocol conformance in
Python is **structural**, not declared. There is no `implements X` edge to
walk — determining "which classes structurally satisfy this Protocol"
means either (a) a full duck-typing/method-signature-compatibility check
across some candidate set of classes, or (b) a narrower heuristic (e.g.
"classes that are composed, anywhere in the repo, into the same
`SALTMDBHandler`-shaped hierarchy that also has the Protocol as a
`TYPE_CHECKING`-only base" — closer to H2's reverse-composition-site walk
in spirit, but conflating a Protocol match with an actual runtime
composition relationship is itself an unverified assumption, not a given).
This needs real design work, not an extension of H1/H2's mechanism.

## Explicitly not yet decided (do not silently assume any of this on pickup)

- Whether (a) full structural/duck-typing matching or (b) a narrower
  composition-site heuristic (or something else entirely) is the right
  mechanism — no live-source grounding has been done for this yet.
- Confidence tier for a resolved Protocol→implementation edge (this is
  inherently less certain than H1/H2's nominal-inheritance walks — Python
  allows a class to satisfy a Protocol without ever being composed with it
  the way SALTMDB's `SALTMDBHandler` example happens to be).
- Whether this belongs in the daemon's enrichment pass (matching H2's
  locked approach) or needs a different mechanism entirely, given the
  structural-matching cost is likely much higher than a targeted reverse
  relation-store walk.
- Whether "stub" detection itself (bare `...`/`pass`/docstring-only body)
  is a reliable enough signal, or produces false positives against a
  genuinely-final one-implementation abstract method.

## Workflow constraints carried into the spec

- Do not start implementation from this file — it is a scope placeholder,
  not a spec. A real grounding + grilling pass (same process as H1/H2) is
  needed before this is implementation-ready.
- Sequencing relative to H1/H2 not yet decided; likely after both land,
  since it's a strictly harder problem and doesn't block either.
- Never change SALTMDB (or any other target codebase) to accommodate an
  ACIE finding.

## Verification note

Not grounded against live source yet beyond what H2's grilling session
already established about SALTMDB's `_protocol.py`/`entities.py` shape
(see H2 spec and SALTMDB memory `f93f67cb`, Finding 1). Filed in git
worktree `worktree-spec2-mixin-mro-grounding` at commit `2035bca`.
