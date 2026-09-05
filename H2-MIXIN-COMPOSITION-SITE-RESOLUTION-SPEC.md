# Capability H, Slice H2 — self.method() Resolution Through Mixin Composition Sites

## Status: design exploration, NOT locked for handoff

Unlike H1 (`H1-SELF-METHOD-DIRECT-BASE-RESOLUTION-SPEC.md`), this spec has a
real open architectural fork (see "The open decision" below) that needs
explicit user sign-off before it should be treated as ready for
implementation. Everything else here is grounded against live source the
same way H1 and G1 were; the fork is flagged rather than silently resolved,
per this spec's own standing rule against silently resolving low-confidence
details.

## The bug this closes

The actual concrete case `febf3e07` (external ground-truth validation,
`aa8679e0` item 2) documents: SALTMDB's `SALTMDBHandler(EntitiesMixin,
EventsMixin, RelationsMixin, ..., ViewerHandlerBase)` composes 8 mixin
classes, each in its own file, against a shared base (`ViewerHandlerBase`)
supplying primitives like `send_json` referenced via `self.` from every
mixin. `EntitiesMixin` (where `self.send_json()` is actually called, 56+
real sites) has **no base-class relationship to `ViewerHandlerBase` at
all** — the two are only ever siblings in `SALTMDBHandler`'s base list, in
a fourth file (`routes/__init__.py`). H1's forward base-walk (however
transitive, however many files) cannot find this, because there is nothing
to find by walking `EntitiesMixin`'s own bases — the relationship exists
only at the point where a third class composes both together.

This is the failure mode that produced the 93% miss rate (`find_references`
on `ViewerHandlerBase.send_json` found 5 of 61 real call sites) that made
`febf3e07` worth filing in the first place.

## Why this needs a reverse walk, not a bigger forward walk

Grounded this session (`f9f7927f`): resolving `self.send_json()` inside
`EntitiesMixin` correctly requires knowing that *some other class* composes
`EntitiesMixin` together with a base that defines `send_json`. That is
inherently a **reverse** question ("who uses me as a mixin, and what else
do they bring in?"), not a forward one ("what do my own bases define?").

## Grounded mechanism: reuse existing store primitives, don't build a new reverse index

Verified live (`src/acie/storage/relation_store.py:293-345`): `RelationStore`
already exposes both directions on the live relation table —

```python
def list_by_target(self, target: str, *, predicates: set[str] | None = None) -> list[Relation]:
    """Live relations whose target is exactly the given symbol id... Used by
    find_references to list every reference site pointing at a resolved symbol."""

def list_by_source(self, source: str, *, predicates: set[str] | None = None) -> list[Relation]:
    """Live relations whose source is exactly the given symbol id... Used by
    graph's downstream traversal to list every outbound edge from a symbol."""
```

Both are already used elsewhere in `indexer.py` (`list_by_target` in the
stale-cross-file-relation cleanup logic, `index_file` lines ~86-96). This
means the reverse-composition walk does **not** need a new index built
from scratch:

1. For a `self.<method>()` miss on class `M` where H1's forward walk (same-
   file transitive + one-level cross-file) also found nothing: query
   `relation_store.list_by_target(M.id, predicates={"inherits"})` to get
   every live `inherits` relation whose target is `M` — i.e. every class
   `C` that currently lists `M` as a base, anywhere in the repo, already
   resolved (same-file or cross-file, since `inherits` edges are fully
   resolved live relations by the time this would run, not raw AST data).
2. For each such `C`, query `relation_store.list_by_source(C.id,
   predicates={"inherits"})` to get `C`'s own full immediate base list
   (again, already-resolved symbol ids, cross-file included).
3. For each of `C`'s bases other than `M` itself, resolve the method the
   same way `_resolve_deferred_overrides` already does:
   `symbol_store.find_by_qualname_and_kind(f"{base.qualname}.{method_name}",
   kind="method")`, filtered to `method.path == base.path` (the same
   2026-09-02 codex-review fix that prevents an unrelated same-named class
   elsewhere in the repo from leaking in).
4. Union every candidate found across every composing site `C` and every
   sibling base — mirrors `_overrides_relations`'s existing union-ambiguity
   rule, extended across composition sites (a mixin reused in several
   unrelated composed classes with different sibling mixins is a new
   ambiguity shape this codebase hasn't needed before, but the mechanism —
   union everything found, flag `AMBIGUOUS` if more than one candidate — is
   the same one already established).

## The open decision: when does this actually run?

This is the part that is NOT mechanically simple, and is why this spec is
marked design-exploration rather than locked.

Every existing deferred-resolution kind (`DeferredImportCall`,
`DeferredImportInherit`, `DeferredImportOverride`, and H1's new
`DeferredImportSelfCall`) knows its target's module path at extraction
time and just waits for that one specific file to get indexed, then
resolves in a single pass. **H2's miss doesn't know which file(s) will
eventually compose `M`** — `EntitiesMixin` on its own has no way to know
`SALTMDBHandler` will someday list it as a base. Two real possibilities:

- If `SALTMDBHandler`'s file is indexed **before** `EntitiesMixin`'s file:
  by the time `EntitiesMixin` is indexed and hits the self-call miss, the
  `inherits` edge `SALTMDBHandler → EntitiesMixin` already exists in
  `relations_live`, so the reverse walk above would find it immediately if
  run right then.
- If `SALTMDBHandler`'s file is indexed **after** `EntitiesMixin`'s file
  (the more common real-world order, if mixins tend to get written/indexed
  before whatever composes them): the reverse walk finds nothing at
  `EntitiesMixin`-index-time, because `C`'s `inherits` edge doesn't exist
  yet. The miss needs to be re-attempted later, once `SALTMDBHandler` is
  indexed — but nothing currently watches for "a new `inherits` edge just
  appeared, go re-check old misses that might now resolve."

Two candidate approaches, not yet chosen:

**(a) Piggyback on Capability E's existing repo-level re-enrichment
trigger.** Verified live (`src/acie/daemon/enrichment_scheduler.py:1-55`):
a `RepoEnrichmentGuard`-style scheduler already "coalesce[s] every
enrichment trigger source through one repo guard," firing on
bootstrap/migration/reconciliation events (`trigger_now`, line 54) — this
already exists, already runs periodically/on-trigger across the whole
repo, and was built for a related purpose (re-running pyright-based
enrichment to upgrade ambiguous edges). H2 would record every unresolved
self-call miss as a new deferred kind (e.g. `DeferredMixinSelfCall`:
source symbol id, enclosing class qualname, method name, site info — no
target module path, since none is known), persisted the same way other
deferred items are, and have each repo-level re-enrichment pass also sweep
outstanding `DeferredMixinSelfCall` items against the *current*
`relations_live` table via the reverse walk above. Advantage: reuses
existing, already-built, already-scheduled infrastructure instead of
inventing new invalidation triggers. Needs confirmation: whether E1's
scheduler's current trigger conditions (bootstrap/migration/reconciliation)
would actually fire often enough in normal daemon operation to catch a
newly-discovered composition promptly, or whether a repo-wide `acie scan`
re-run is the only thing that would currently invoke it — this needs
reading `enrichment_scheduler.py`/`runtime.py` in full before committing,
not assumed from the docstring alone.

**(b) Query-time (on-demand) resolution.** Don't persist anything at
index time; instead, have `find_references`/`get_definition`/
`impact_analysis` themselves perform the reverse walk live, on every query
against a symbol that has zero same-file `calls` edges, as a fallback.
Advantage: always current, no staleness window, no new deferred-item
plumbing. Disadvantage: pushes new logic into every query surface instead
of the storage/indexing layer where every other relation kind is resolved
once and cached as a live relation — a more invasive change to the MCP
tool layer, and a query-time cost (extra store queries per miss) on every
relevant lookup rather than paid once at index time.

**Recommendation, not yet confirmed:** (a) looks like the better fit —
it's additive to an existing, working trigger mechanism rather than a new
architectural layer in the query tools, and keeps the "relations are
pre-resolved, queries just read them" invariant every other capability in
this codebase relies on. But this spec does not lock it in; the
`enrichment_scheduler.py`/`runtime.py` read needed to confirm trigger
frequency has not been done yet this session (time-boxed out of this
grounding pass), and the user has not been asked to confirm the approach.

## Scope (assuming approach (a) — revisit if (b) is chosen instead)

### In scope

- Single-level composition only: `M` composed directly as a base of some
  `C`. Not: `M` composed into `C`, and `C` itself further composed as a
  mixin into `D` elsewhere, with the method actually living on one of
  `D`'s other bases. Mirrors H1's own single-hop cross-file limit — deeper
  transitivity is a real residual gap, named explicitly, not silently
  dropped.
- Union-ambiguity across every composing site and every sibling base found,
  per the mechanism above.
- New `DeferredMixinSelfCall` deferred kind + a new resolution sweep,
  wired into whatever the confirmed trigger mechanism turns out to be.

### Out of scope

- Multi-level composition chains (see above).
- Joint ambiguity modeling between H1's same-hierarchy candidates and H2's
  composition-site candidates for the same call site — resolved
  independently, same shortcut precedent `_overrides_relations` and H1
  both already carry forward.
- Any change to query-time MCP tool behavior, if approach (a) is confirmed.

## Files likely to touch (approach (a); revisit if (b))

- `src/acie/adapters/python/extract_relations.py` — self-branch emits
  `DeferredMixinSelfCall` when both same-file/transitive and one-level
  cross-file lookups (H1's logic) also miss.
- `src/acie/ir/relation.py` (confirm exact location) — new
  `DeferredMixinSelfCall` dataclass.
- `src/acie/daemon/enrichment_scheduler.py` and/or `runtime.py` — new sweep
  step reusing `relation_store.list_by_target`/`list_by_source` per the
  mechanism above, invoked wherever the confirmed trigger fires.
- Persistence for outstanding `DeferredMixinSelfCall` items across daemon
  runs — confirm whether existing deferred items are persisted anywhere
  durable already (SQLite table?) or held in memory only per-process; this
  materially affects whether H2's items survive a daemon restart, and was
  not verified this session.
- Test files mirroring H1's, plus a repo-wide/daemon-level integration test
  shaped exactly like SALTMDB's `routes/__init__.py` (per `febf3e07`'s own
  original recommendation) — N mixins in N files, composed in a file
  indexed after them, asserting the self-call resolves once the composing
  file is indexed and the sweep runs.

## Workflow constraints carried into the spec

- **Do not start implementation from this spec as-is.** Get the open
  decision (approach (a) vs (b)) confirmed with the user first, and read
  `enrichment_scheduler.py`/`runtime.py` in full to verify (a)'s trigger
  frequency before locking it in.
- Sequence after H1 lands, not concurrently — H2's forward-miss precondition
  ("H1's logic also found nothing") depends on H1 existing.
- Implement with the `tdd` skill once unlocked; one slice per session, stop
  before commit for review (memory `9b020543`).
- Never change SALTMDB (or any other target codebase) to accommodate an
  ACIE finding.

## Verification note

Grounded live against `src/acie/storage/relation_store.py` (lines 293-345,
confirming `list_by_target`/`list_by_source` already exist and are already
used cross-file elsewhere in `indexer.py`) and
`src/acie/daemon/enrichment_scheduler.py` (line 1's module docstring and
`trigger_now`, line 54 — read only enough to confirm the mechanism exists,
NOT read in full; approach (a)'s viability is provisional pending that
fuller read). All read in git worktree `worktree-spec2-mixin-mro-grounding`
at commit `59983b6`. SALTMDB memory `f9f7927f` records the grounding
session both H1 and H2 were drafted from.
