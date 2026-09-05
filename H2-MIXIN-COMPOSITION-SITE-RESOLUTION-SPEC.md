# Capability H, Slice H2 — self.method() Resolution Through Mixin Composition Sites

## Status: LOCKED for handoff (grounding + grilling session, 2026-09-05)

The open architectural fork this spec originally carried has been resolved
with the user (grilling session, memory `f93f67cb` → this update). Three
decisions were made explicitly, not silently assumed — see "Decisions
locked this session" below. Ready for TDD implementation, sequenced after
H1 lands, per the existing workflow constraints at the bottom of this file.

## The bug this closes — and the one it does NOT close

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

**IMPORTANT — grounded this session (memory `f93f67cb`, Finding 1, and
"Decisions locked" §1 below): H2 as scoped will NOT close this exact
example.** SALTMDB's real current source has since grown a
`TYPE_CHECKING`-gated `Protocol` base on each mixin (`ViewerHandlerProtocol`
in `_protocol.py`), added for an unrelated mypy fix. H1's own forward walk
*will* resolve `self.send_json()` inside `EntitiesMixin` to
`ViewerHandlerProtocol.send_json` — a real stub declaration, not the
concrete `ViewerHandlerBase.send_json` implementation `febf3e07` actually
wanted found. Because H2 only ever runs when H1's forward walk finds
**nothing** (see "Decisions locked" §1), this specific site never reaches
H2's reverse walk at all. Closing `febf3e07`'s literal motivating example
is now H3's job (Protocol-to-implementation resolution — see
`H3-PROTOCOL-TO-IMPLEMENTATION-RESOLUTION-SPEC.md`, filed alongside this
update, not yet designed in detail). H2 remains worth building for the
genuinely-still-real case below: a mixin composed by sibling with **zero**
declared relationship anywhere, Protocol or otherwise.

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

## Decisions locked this session (grilling session, memory `f93f67cb` → user sign-off)

### §1 — Trigger precondition stays exactly as originally spec'd: H1 found nothing

Considered broadening it (H2 also fires when H1 resolved to an abstract/
stub target, e.g. a `Protocol` method) so H2 alone could close `febf3e07`'s
literal motivating example. **Rejected.** Stub-vs-concrete arbitration
(deciding a concrete implementer should outrank a `Protocol`/`abstractmethod`
stub H1 already found) is the same "structural conformance" shape as H3's
whole job, regardless of whether the stub was reached via H1's declared-base
walk or discovered some other way — building that ranking logic into H2
*and* H3 would duplicate it for no benefit. H2 stays gated strictly on "H1's
forward walk (same-file transitive + one-level cross-file) found nothing at
all" — see the consequence for `febf3e07` called out above.

### §2 — Mechanism: fold into the existing re-derive-every-pass convention, no new persisted type

Verified live this session (`src/acie/daemon/lsp_enrichment.py`, full read —
not just the docstring, resolving the spec's own prior TODO to do this
before committing):

- **Trigger frequency is confirmed adequate.** `run_enrichment_pass` is
  invoked by `EnrichmentScheduler`'s `trigger_now` for bootstrap/migration/
  reconciliation (each deduped to at most once per repo per daemon
  lifetime) **and** by `on_watcher_edit`, debounced 30s quiet / 300s max —
  this is the real steady-state trigger and fires on every filesystem edit,
  which is prompt enough to catch a newly-indexed composing class without a
  dedicated new invalidation path.
- **But `run_enrichment_pass` does NOT persist any deferred item.**
  `_worklist` (`lsp_enrichment.py:119-131`) recomputes every unresolved
  `calls`/`inherits` site **from scratch on every pass** — full repo walk,
  fresh `extract_relations_with_deferred_edges` per file, fresh
  `unresolved_deferred_sites` call — and separately rechecks existing
  `AMBIGUOUS` relations via `relation_store.list_by_site_file`. Nothing
  from one pass survives to seed the next; this is the actual, consistent
  convention every existing deferred kind (`DeferredImportCall`,
  `DeferredImportInherit`, `DeferredImportOverride`, and H1's own
  `DeferredImportSelfCall`) already follows.

**Locked: approach (a) as originally drafted (new persisted
`DeferredMixinSelfCall` row, dedicated sweep step) is dropped** — it would
have been the first persisted deferred type in the codebase, a convention
break, not a reuse. **Approach (b) (query-time fallback in
`find_references`/`get_definition`/`impact_analysis`) is also dropped** —
it would break the "relations are pre-resolved, queries just read them"
invariant every other capability relies on. **Locked: a variant of (c)** —
fold H2's misses into `_worklist`'s existing recompute-every-pass
convention (no new persisted dataclass; H2 items are recomputed fresh from
source each pass exactly like the other three deferred kinds), but see §3
for why this still needs new plumbing in `run_enrichment_pass`, not just a
new entry in the existing uniform loop.

### §3 — New resolution branch required; the existing pyright-LSP path cannot answer this

Confirmed by reading `run_enrichment_pass`'s full body: every `_Site`
collected by `_worklist` is resolved identically — one
`textDocument/definition` LSP request per site, whatever pyright returns
is taken as the answer. **This cannot work for H2's misses at all**, for
two independent reasons, both grounded this session:

1. Pyright is a real, static type checker. For a call inside an
   unannotated mixin with **zero** declared base, pyright resolves `self`'s
   attributes from that mixin's own declared MRO — it has no way to look
   ahead to "which other class will someday compose this mixin together
   with something that defines the method," the exact reverse question
   this capability exists to answer. It would fail identically to H1,
   every time, for every H2-scoped site — never worth even trying.
2. `_Site` itself doesn't carry enough information to do the reverse walk
   locally either: it has `source, site_file, site_line, site_col,
   predicate` — sufficient for a pyright request (pyright resolves the
   identifier from the position), but the reverse walk (`relation_store
   .list_by_target`/`list_by_source`, per the "Grounded mechanism" section
   above) needs the **enclosing class's symbol id** and the **called
   method's name**, neither of which survives into the generic `_Site`
   shape today.

**Consequence for implementation:** this is not "add one more predicate to
the existing loop." It needs (a) H2-scoped misses to carry enclosing-class
id + method name through `_worklist` into `run_enrichment_pass` (e.g. a
distinct site subtype or a parallel collection alongside `sites`), and (b)
a new resolution branch in `run_enrichment_pass`'s main loop that, for
these specific items, performs the reverse walk directly against
`relation_store` and skips the LSP round-trip entirely (both because it
cannot help, per point 1, and because skipping it avoids a wasted
network/subprocess round-trip for a class of site pyright can never
answer).

## Scope (locked)

### In scope

- Single-level composition only: `M` composed directly as a base of some
  `C`. Not: `M` composed into `C`, and `C` itself further composed as a
  mixin into `D` elsewhere, with the method actually living on one of
  `D`'s other bases. Mirrors H1's own single-hop cross-file limit — deeper
  transitivity is a real residual gap, named explicitly, not silently
  dropped.
- Union-ambiguity across every composing site and every sibling base found,
  per the mechanism above.
- Strictly gated on H1 finding nothing (§1) — sites where H1 resolved to a
  stub/Protocol/abstract target are explicitly NOT H2's job (H3's, once
  designed).
- A new in-memory (never persisted) representation of an H2-eligible miss —
  enclosing class id + method name + site info — recomputed fresh every
  `run_enrichment_pass` invocation, mirroring how `DeferredImportCall`/
  `Inherit`/`Override`/`SelfCall` already work, plus a new resolution
  branch in `run_enrichment_pass` that resolves these directly against
  `relation_store` instead of asking pyright (§3).

### Out of scope

- Multi-level composition chains (see above).
- Joint ambiguity modeling between H1's same-hierarchy candidates and H2's
  composition-site candidates for the same call site — resolved
  independently, same shortcut precedent `_overrides_relations` and H1
  both already carry forward. Now additionally justified by §1: since H2
  only ever runs when H1 found nothing, there is no overlapping candidate
  set to reconcile in the first place.
- Stub-vs-concrete arbitration when H1 *did* find a Protocol/abstract
  target — deferred whole to H3 (§1, and the callout under "The bug this
  closes").
- Any change to query-time MCP tool behavior (approach (b) was considered
  and dropped, §2).
- Persistence of outstanding misses across daemon restarts (approach (a)'s
  original persisted-dataclass design was considered and dropped, §2) —
  a miss that isn't resolved this pass just gets recomputed and re-tried
  next pass, same as every other deferred kind.

## Files likely to touch

- `src/acie/adapters/python/extract_relations.py` — self-call branch
  (shared with H1) needs to surface which self-calls it could NOT resolve
  even after H1's same-file/transitive + one-level cross-file lookups, in
  a shape that preserves enclosing-class id + method name (H1's own
  `DeferredImportSelfCall`, if it already carries this, may be directly
  reusable here rather than needing a new type — confirm against H1's
  actual landed implementation before designing a parallel one).
- `src/acie/daemon/lsp_enrichment.py` — `_worklist` needs to also collect
  H2-eligible misses (still unresolved `DeferredImportSelfCall` items,
  filtered to those H1 could not place); `run_enrichment_pass`'s main loop
  needs a new branch that, for these items only, performs the reverse walk
  (`relation_store.list_by_target`/`list_by_source`, per "Grounded
  mechanism" above) instead of sending a `textDocument/definition` request
  — see §3 for why the pyright path cannot answer these at all.
- Test files mirroring H1's, plus a repo-wide/daemon-level integration test
  shaped exactly like SALTMDB's `routes/__init__.py` (per `febf3e07`'s own
  original recommendation) — N mixins in N files, composed in a file
  indexed after them, asserting the self-call resolves once the composing
  file is indexed and the next enrichment pass runs. Since `febf3e07`'s
  actual SALTMDB fixture now has a Protocol layer (Finding 1) and would
  therefore never reach H2's branch, this integration test needs a
  **synthetic** fixture with zero declared relationship anywhere — the
  real SALTMDB source no longer exercises H2's exact case.

## Workflow constraints carried into the spec

- Sequence after H1 lands, not concurrently — H2's forward-miss precondition
  ("H1's logic also found nothing") is an inherent dependency on H1's
  outcome, not just a scheduling convenience: H2 has no independent
  detection of a composition-site miss, it only catches what H1 already
  tried and failed on (confirmed explicitly this session).
- Implement with the `tdd` skill once unlocked; one slice per session, stop
  before commit for review (memory `9b020543`).
- Never change SALTMDB (or any other target codebase) to accommodate an
  ACIE finding.

## Verification note

Grounded live against `src/acie/storage/relation_store.py` (lines 293-345,
confirming `list_by_target`/`list_by_source` already exist and are already
used cross-file elsewhere in `indexer.py`) and, this session,
`src/acie/daemon/lsp_enrichment.py` in full (`run_enrichment_pass` and
`_worklist`, resolving the prior TODO to read `enrichment_scheduler.py`/
`runtime.py`'s trigger-frequency behavior before locking in a mechanism —
`on_watcher_edit`'s 30s/300s debounce is the real steady-state trigger and
was confirmed adequate). All read in git worktree
`worktree-spec2-mixin-mro-grounding` at commit `2035bca` (still based on
`59983b6`, not yet rebased onto master/G1). SALTMDB memory `f9f7927f`
records the original grounding session both H1 and H2 were drafted from;
memory `f93f67cb` records this session's two new findings and the grilling
round that resolved them.
