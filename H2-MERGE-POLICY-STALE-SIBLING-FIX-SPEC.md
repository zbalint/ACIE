# H2 Hardening — Stale AMBIGUOUS Sibling Retirement Across Passes

## Status: LOCKED for handoff (planning session, 2026-09-06)

Follow-up to Capability H, slice H2 (`aa92a51`, spec
`H2-MIXIN-COMPOSITION-SITE-RESOLUTION-SPEC.md`), raised as round-1 review
Finding A (SALTMDB memory `22c18638`) and carried forward unfixed at
commit time (memory `21f50031`). User decision this session: fix properly,
not document as an accepted shortcut. Ready for TDD implementation.

## The gap

`src/acie/daemon/merge_policy.py:50-52`:

```python
def _retire_stale_siblings(relation_store: RelationStore, relation: Relation) -> int:
    if relation.confidence == Confidence.AMBIGUOUS:
        return 0
    ...
```

This unconditional early return exists for a real reason: `run_enrichment_pass`
(`src/acie/daemon/lsp_enrichment.py:87-106`) submits each `_MixinSite`
candidate as its own independent merge job — `_make_merge_job(relation)`
closes over a single `Relation`, nothing else (`lsp_enrichment.py:273-277`).
When a site has N>1 candidates this pass, N separate `AMBIGUOUS` writes land
one at a time; without the guard, each write would retire its same-site
siblings via the pre-existing "retire same-site AMBIGUOUS siblings with a
different target" rule, collapsing the whole union down to whichever
candidate got written last. The guard fixes that — but it fixes it by
skipping retirement for *every* AMBIGUOUS write, not just ones that are part
of the same pass's coordinated candidate set.

Consequence: if a composing class's sibling base changes across passes
(`ComposedB(Mixin, ProviderB)` → `ComposedB(Mixin, ProviderC)`, both
defining the same method) while the candidate *count* for that site stays
>1 (still ambiguous, just different membership), the stale row
(`ProviderB.provided`, still `AMBIGUOUS`) is never retired — nothing ever
compares it against the freshly-recomputed candidate set again. It would
only get cleaned up if the count later drops to exactly 1 (an `INFERRED`
write, which does still run retirement unconditionally).

This is a correctness gap in `merge_policy.py`'s general contract, not
something scoped to `_MixinSite` — any future site kind that can produce a
multi-candidate `AMBIGUOUS` union across passes would hit the identical
gap. The fix should live at that same general layer.

## Locked mechanism

Give the merge layer visibility into "the full candidate-target set this
exact pass computed for this site," not just the one relation being
written right now:

1. `apply_enrichment_write` and `_retire_stale_siblings` gain an optional
   parameter, `current_pass_targets: frozenset[str] | None = None`.
2. `_retire_stale_siblings`'s behavior when the incoming relation is
   `AMBIGUOUS`:
   - `current_pass_targets is None` (every existing caller — nothing else
     in the codebase writes `AMBIGUOUS` relations today): behavior is
     byte-identical to current code — return 0 immediately. No regression
     risk to D3/D4's existing non-H2 call sites.
   - `current_pass_targets` is supplied: instead of returning 0
     unconditionally, retire existing same-site `AMBIGUOUS` siblings
     (`sibling.source == relation.source and sibling.confidence ==
     Confidence.AMBIGUOUS`, matching the existing sibling-selection
     predicate) whose `sibling.target` is **not** in
     `current_pass_targets`. Siblings whose target **is** in the set are
     left untouched — this covers both a sibling already written earlier
     in the same pass (must survive) and one not yet written but about to
     be (never touched since it isn't "existing" yet, and once it lands it
     will itself be a no-op retirement pass over the same set).
3. `run_enrichment_pass`'s `_MixinSite` branch (`lsp_enrichment.py:87-106`)
   computes `current_pass_targets = frozenset(c.id for c in candidates)`
   once, before the per-candidate loop, and threads it through
   `_make_merge_job` into each of that site's N writes.
4. `_make_merge_job` gains a matching optional parameter (default `None`)
   so its one non-H2 call site (the LSP-resolution branch, line ~140)
   needs no change.

Net effect: within one pass, the union survives exactly as it does today
(nothing in `current_pass_targets` gets touched). Across passes, a target
that has genuinely dropped out of the fresh candidate set — because the
composing class's sibling bases changed — gets retired by the very next
write for that site, the same pass that discovers the new membership.

## Explicitly not changing

- The `len(candidates) == 1` → `INFERRED` path's existing unconditional
  retirement (`_retire_stale_siblings` when `relation.confidence !=
  AMBIGUOUS`) — already correct, not part of this gap.
- `merge_policy.py`'s two existing documented shortcuts (cross-pass
  `INFERRED` reconciliation; non-transactional upsert+retire) — unrelated,
  left as-is.
- Any persistence model — this stays inside the existing recompute-every-
  pass convention H2 already established; `current_pass_targets` is a
  transient value computed and consumed within one `run_enrichment_pass`
  call, never stored.

## Tests to add

1. **Primary regression** (the actual gap): two enrichment passes, same
   `_MixinSite`, sibling-base membership swaps between them
   (`ComposedB(Mixin, ProviderB)` → `ComposedB(Mixin, ProviderC)`) while
   candidate count stays 2 both times. Assert after pass 2 that
   `ProviderB.provided`'s row is gone and only `ProviderC.provided` (plus
   whatever the unchanged second candidate is) remains `AMBIGUOUS`.
2. **Same-pass union still survives** — this already has coverage
   (`test_preserves_ambiguous_siblings_when_incoming_candidate_is_ambiguous`,
   per round-1 review) but re-run it against the new
   `current_pass_targets`-aware code path to confirm no regression; extend
   rather than duplicate if the existing test can take the new parameter
   directly.
3. **Also add while touching this area** (folded in from the same review
   round, not part of the staleness fix itself but cheap to bundle):
   - A test for the spec's explicitly-named out-of-scope multi-level
     composition chain case (`H2-MIXIN-COMPOSITION-SITE-RESOLUTION-SPEC.md`
     "Out of scope" — `M` composed into `C`, `C` itself composed as a
     mixin into `D`, method actually on one of `D`'s other bases) —
     asserting **current silent-no-match behavior**, not implementing
     multi-level support. Cheap insurance against silent scope creep if
     `list_by_source`/`list_by_target` ever grow transitive semantics.
   - Cosmetic: remove the extra blank line (3 instead of the conventional
     2) before `_MixinSite`'s `@dataclass` decorator in
     `lsp_enrichment.py` (currently line ~38-40).

## Files to touch

- `src/acie/daemon/merge_policy.py` — `apply_enrichment_write`,
  `_retire_stale_siblings`.
- `src/acie/daemon/lsp_enrichment.py` — `_MixinSite` branch of
  `run_enrichment_pass`, `_make_merge_job`; the blank-line cosmetic fix.
- `tests/daemon/test_merge_policy.py` (or wherever the existing
  `test_preserves_ambiguous_siblings_when_incoming_candidate_is_ambiguous`
  lives) — new regression test.
- Wherever H2's enrichment-pass integration tests live — multi-level
  composition-chain no-match test.

## Workflow constraints carried into this spec

- Implement with the `tdd` skill, one slice, stop before commit for
  review (memory `9b020543`) — same convention as every other capability
  spec in this repo.
- Do not fold this into an unrelated commit; this is its own reviewable
  diff.

## Verification note

Grounded live this session against
`src/acie/daemon/merge_policy.py:1-81` (full file) and
`src/acie/daemon/lsp_enrichment.py:30-278` (full file) on master at
`aa92a51`. Prior findings sourced from SALTMDB memories `22c18638`
(round-1 review, Finding A) and `6374cc42` (round-2 review, confirms
Finding A is novel, not previously caught in omp's dev-session transcript).
