# Capability H, Slice H3 — Protocol/ABC Stub → Concrete Implementation Resolution

## Status: LOCKED for handoff (grounding + grilling session, 2026-09-05)

Filed as a scope-only placeholder during H2's grilling session (memory
`fc88cee5`). This session ran its own grounding pass (memory `c73da47a`)
against live source in both SALTMDB and ACIE's in-progress H1 diff, then a
full grilling round (8 questions, all resolved with the user). Ready for
TDD implementation once its precondition (below) is met — not before.

## The bug this closes

`febf3e07`'s original motivating example: SALTMDB's `EntitiesMixin` calls
`self.send_json()` 56+ times. `send_json` isn't defined on `EntitiesMixin`
or anywhere in its own base chain in the naive sense — but SALTMDB's real
source declares a `TYPE_CHECKING`-gated `Protocol` base
(`ViewerHandlerProtocol` in `saltmdb/viewer/routes/_protocol.py`) on
`EntitiesMixin` (and identically on all 7 other feature mixins, and on
`ViewerHandlerBase` itself), added for an unrelated mypy fix. Once H1
lands, `self.send_json()` resolves cleanly, at `EXTRACTED` confidence, to
`ViewerHandlerProtocol.send_json` — a real, correctly-declared edge, but a
**stub** (`def send_json(self, data, status: int = 200) -> None: ...`),
not the concrete `ViewerHandlerBase.send_json` implementation
`find_references`/callers actually want (`ViewerHandlerBase.send_json` is a
full implementation: response headers, CORS origin check, `self.wfile
.write(...)` — grounded live, `base.py:45-58`).

H3's job: given a `calls` relation whose target method is a stub, find the
concrete class(es) that share a declared relationship to that stub's class
and override the same method concretely, and add that as an additional
resolved target.

## Grounded finding: SALTMDBHandler still nominally composes ViewerHandlerBase directly

Answers the open question `f93f67cb` left unresolved. `routes/__init__.py:
35-45`:

```python
class SALTMDBHandler(
    EntitiesMixin, EventsMixin, RelationsMixin, StatsMixin, SearchMixin,
    SessionsMixin, ScatterplotMixin, EntityDetailMixin,
    ViewerHandlerBase,
):
```

`ViewerHandlerBase` is a real, direct, same-file, unconditional base of
`SALTMDBHandler` — H2's plain nominal-composition mechanism is not moot in
general, it simply never fires for `EntitiesMixin.send_json()` specifically
because H1 resolves that call from `EntitiesMixin`'s own declared bases
only (`ViewerHandlerProtocol`), never reaching `SALTMDBHandler`'s
composition list.

Also grounded: **every** one of the 8 feature mixins, and `ViewerHandlerBase`
itself, independently declares `ViewerHandlerProtocol` as a base (`base.py:
32`: `class ViewerHandlerBase(http.server.BaseHTTPRequestHandler,
ViewerHandlerProtocol):`). This case is not zero-declaration duck-typing —
every participant nominally names the same Protocol class.

## Why H1/H2's trigger shape does not transfer: this fires on success, not failure

Grounded against H1's in-progress diff (uncommitted, main repo, `omp`
confirmed still working it live — read-only, not touched): `indexer.py`'s
new `_resolve_deferred_self_calls` reuses `_imported_base_method_candidates`
(the same helper `_resolve_deferred_overrides` already used) — single
candidate on the calling method's own declared base → **`Confidence
.EXTRACTED`**, not `AMBIGUOUS`, not unresolved. H1 succeeds cleanly and
lands on the stub.

**Consequence**: H3 cannot key off `unresolved_deferred_sites` (H1's own
gap surface) or off low confidence (H1's landing confidence here is its
highest tier). It must scan *already-resolved* `calls` relations and ask
"is the resolved target itself a stub?" — inspecting success, not chasing
failure, unlike every other capability in this codebase (H1's forward walk,
H2's reverse walk, the existing `AMBIGUOUS`-recheck in `_worklist`).

## Scope (locked)

### In scope

Both real-world shapes below reduce to **one mechanism** — grounded and
confirmed during grilling, this is not two designs:

1. **Protocol-sharing case** (the real SALTMDB example): stub is declared
   on a `Protocol` subclass; a sibling class independently declares the
   same Protocol as a base and concretely overrides the method.
2. **ABC/abstractmethod case**: stub is `@abstractmethod` on an ABC; a
   subclass overrides it concretely. Subclassing an ABC already produces
   an ordinary `inherits` edge from the subclass to the ABC — the exact
   same query shape as case 1, just with the roles of "declares the same
   base" already implied by ordinary inheritance rather than a shared
   third-party marker.

Mechanism (query shape identical for both cases):

1. A method `Symbol` is a **stub** iff its body reduces to a bare `...`,
   `pass`, or docstring-only statement, **and** its declaring class is
   either a `Protocol` subclass or the method itself is
   `@abstractmethod`-decorated on an ABC. (Body-shape alone was considered
   and rejected — see "Decisions locked" §2.) This is computed once,
   stored as a new persisted field on `Symbol` (exact name TBD at
   implementation time), not recomputed by re-reading source at resolution
   time.
2. For a `calls` relation whose target `Symbol.is_stub` is true, with
   declaring class `S`: query `relation_store.list_by_target(S.id,
   predicates={"inherits"})` to find every class `C` that currently
   declares `S` as a base, anywhere in the repo (already-resolved,
   cross-file included).
3. For each such `C` (excluding `S` itself), look up
   `symbol_store.find_by_qualname_and_kind(f"{C.qualname}.{method_name}",
   kind="method")`, filtered to `method.path == C.path` (same
   already-established filter `_resolve_deferred_overrides`/H1 use to keep
   an unrelated same-named class elsewhere from leaking in), and check that
   candidate method is itself **not** a stub.
4. **Union**, don't override: keep the original stub-targeting `calls`
   relation as-is; add one additional `calls` relation (same source, same
   site, target = each concrete candidate found) for every non-stub
   candidate across every `C`. A single candidate → `Confidence.INFERRED`.
   More than one → `Confidence.AMBIGUOUS`. Never `EXTRACTED` — see
   "Decisions locked" §4.
5. Runs as a new, independent step inside `run_enrichment_pass`, sibling to
   `_worklist`, **not** routed through it — no LSP/pyright round-trip
   involved anywhere in this mechanism (nothing for a type checker to
   contribute; the answer lives entirely in `relation_store`/`symbol_store`
   once `is_stub` exists). Recomputed fresh on every triggered pass
   (bootstrap/reconciliation/`on_watcher_edit`, the same triggers `_worklist`
   already uses), matching the codebase's no-persisted-deferred-state
   convention — only the final union relations are persisted, same as any
   other resolved relation.
6. Scans by `target.is_stub`, not by the call relation's own confidence —
   applies uniformly whether the stub-targeting `calls` relation itself is
   `EXTRACTED`, `INFERRED`, or `AMBIGUOUS`.

### Out of scope

- **True duck-typing** — a class that structurally satisfies a Protocol's
  method signatures without ever declaring it as a base anywhere. No graph
  edge exists to walk for this case; it would need a full
  signature-compatibility scan across repo classes, a categorically
  different (and more expensive) mechanism. Filed as a future placeholder,
  **H4**, not designed here, not blocking H3.
- **Composition verification.** H3 does *not* confirm that the stub-caller's
  class and the candidate concrete implementer are ever actually composed
  together into one real MRO (e.g. via some class `Z`'s own base list).
  "Both declare the same base, and the candidate has a concrete override"
  is accepted as sufficient signal on its own — see "Decisions locked" §3
  for the false-positive risk this accepts, explicitly, rather than
  engineers around.
- Body-shape-only stub detection (rejected — see "Decisions locked" §2).
- Any new relation predicate distinct from `calls` for the stub↔implementer
  pairing itself (e.g. a general "implements" fact) — not asked for, not
  built; H3 only ever adds `calls` edges at the specific call sites that
  needed them.
- Any change to query-time MCP tool behavior — the union edges are written
  at enrichment time; queries just read them, same invariant H2 preserved.
- Any change to SALTMDB (or any other target codebase) to accommodate an
  ACIE finding.

## Decisions locked this session (grilling round, 8 questions, all resolved)

### §1 — Scope tier: declared-relationship only; duck-typing deferred to H4

Both motivating scenarios (Protocol-sharing, ABC/subclass) reduce to the
identical `list_by_target(..., predicate="inherits")` reverse-walk — cheap,
bounded, reuses existing `inherits` edges. Full duck-typing (zero declared
relationship) is a categorically more expensive, different-shaped problem
and isn't needed to close `febf3e07`'s real example. Explicitly not
designed here; named H4 so it isn't silently dropped or re-discovered from
scratch.

### §2 — Stub detection requires Protocol/ABC context, not body-shape alone

Body-shape alone (`...`/`pass`/docstring) would false-positive on
legitimate no-op methods never meant to be "resolved past" (event handler
hooks with intentionally empty default bodies, etc.). Gated additionally on
the declaring class being a `Protocol` subclass, or the method carrying
`@abstractmethod` on an ABC.

### §3 — No composition verification; false-positive risk accepted and documented, not engineered around

Nothing in the repo declares that `SALTMDBHandler` composes `EntitiesMixin`
with `ViewerHandlerBase` other than the class statement itself — no
consumer of ACIE's output gets that fact without ACIE surfacing it,
consistent with ACIE's own purpose (its consumers are agents, not humans
reading the source directly). Requiring proof of actual shared-MRO
composition before crediting a candidate would eliminate the (currently
hypothetical, unobserved) false-positive risk of two unrelated classes
coincidentally sharing a Protocol/ABC name, at the cost of a real,
non-trivial verification pass (walking every composition-root class's full
base list). Rejected for now: mirrors H1's own resolution philosophy
(single unambiguous candidate → confident answer, without proving the call
is dynamically reached), matches "narrow scope, ship the real case," and
the risk is undemonstrated in either codebase today. `Confidence.INFERRED`
(§4) is the actual mitigation if this ever needs tightening later — not
this spec.

### §4 — Confidence: always INFERRED for a single candidate, never EXTRACTED

H1's `EXTRACTED` means "the AST directly declares this, no interpretation
needed." H3's edge is a strictly weaker claim even with exactly one
candidate — it rests on §3's unverified-composition assumption, not a
directly-read declaration. Single candidate → `Confidence.INFERRED`;
multiple candidates → `Confidence.AMBIGUOUS` (the literal multi-candidate
case). This is the intended use of the `INFERRED` tier per the original
confidence taxonomy design, not a new carve-out invented for H3.

## Workflow constraints carried into the spec

- **Sequence strictly after both H1 and H2 land** — explicit user decision
  this session (overriding this spec author's own recommendation to build
  independently in parallel, since H3 has no actual mechanism dependency on
  either). Do not start TDD before both are merged, even though a synthetic
  fixture could technically exercise H3 earlier.
- Implement with the `tdd` skill once unlocked; one slice per session, stop
  before commit for review (memory `9b020543`).
- Never change SALTMDB (or any other target codebase) to accommodate an
  ACIE finding.
- `is_stub` (or equivalent) is a genuine `Symbol` schema change — every
  adapter that constructs a `Symbol` needs updating, not just the Python
  adapter. No migration path required: cheaper to delete the local index DB
  and let it rescan the repo than to version the schema change (explicit
  user decision this session).

## Verification note

Grounded live, read-only, this session: SALTMDB's `routes/__init__.py`
(`SALTMDBHandler`'s full base list), `_protocol.py` (`ViewerHandlerProtocol`
in full), `base.py` (`ViewerHandlerBase.send_json`'s concrete body), and all
8 mixin files' class declarations (grep-confirmed each declares
`ViewerHandlerProtocol` as its sole base). Also grounded against ACIE's own
in-progress H1 diff (`src/acie/ir/relation.py`, `src/acie/indexer.py`,
`src/acie/daemon/lsp_enrichment.py` — uncommitted, `omp` confirmed alive
mid-implementation, read-only, not touched) to confirm H1's landed
resolution confidence and mechanism shape. `src/acie/ir/symbol.py` read in
full — confirmed zero existing stub/abstract/Protocol concept anywhere in
ACIE's IR today. Full grounding writeup: SALTMDB memory `c73da47a`. Filed
and committed in git worktree `worktree-spec2-mixin-mro-grounding`.
