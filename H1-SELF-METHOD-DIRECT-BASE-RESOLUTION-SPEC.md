# Capability H, Slice H1 — self.method() Resolution Through Direct Base Classes

## What this is, and why it's a new capability letter, not a reopened one

Capabilities A-G are already locked/committed (A=overrides/inheritance,
B=affected-tests, C=architecture, D=pyright/LSP daemon, E=recurring
re-enrichment, F=cross-module attribute-call resolution, G=import-extraction
completeness). Per the convention those slices themselves establish (`58f6f748`,
`76720d46`): a bug found in an already-shipped capability during external
validation gets its own new letter, not a reopened old one — reopening a
locked-and-signed-off breakdown undermines the "locked means locked"
convention. This is **Capability H** (next unused letter), **Slice H1**.

H1 and its sibling **H2** (`H2-MIXIN-COMPOSITION-SITE-RESOLUTION-SPEC.md`,
separate spec, separate slice) both trace back to the single ground-truth
finding `febf3e07`, but fix two structurally different bugs — see "Why this
is split from H2" below. Do not implement H2's mechanism as part of H1, or
vice versa; they are deliberately sequenced as independent slices.

## The bug this closes (the part of it H1 actually fixes)

`febf3e07` (external ground-truth validation, `aa8679e0` item 2) found that
`self.<method>()` call resolution misses methods defined only on a base
class. This spec closes the **direct/transitive same-hierarchy** case: a
class `Foo` whose own MRO (via `class Foo(Base): ...`, arbitrarily many
levels deep, arbitrarily many same-file or cross-file bases) actually
contains the method being called via `self.`.

It does **not** close the mixin-composition case from `febf3e07`'s own
concrete example (`SALTMDBHandler(EntitiesMixin, ..., ViewerHandlerBase)`,
`self.send_json()` called inside `EntitiesMixin`, which has no base-class
relationship to `ViewerHandlerBase` at all — the two are only ever siblings
in a third class's base list, in a fourth file). See H2 for that.

## Why this is split from H2

Grounded this session (`f9f7927f`, elaborates on `febf3e07`) by tracing the
actual class shapes in `febf3e07`'s SALTMDB example against the real code:
`EntitiesMixin` (where the failing `self.send_json()` calls live) inherits
from nothing. A **forward** walk of `current_class`'s own base chain —
however far transitively, however many files it crosses — finds nothing for
this case, because structurally there is no base relationship to find in
that direction. Fixing it requires a **reverse** walk (find classes that use
`EntitiesMixin` as a base, then search their *other* bases) — a different
algorithm, a new reverse index, and new cross-composition-site ambiguity
semantics not present anywhere in this codebase yet. Bundling both into one
slice would hide a materially larger, less-verified piece of work (H2)
behind a well-understood, mechanically-simple one (H1). Splitting lets H1
ship on its own merits and lets H2 get the scrutiny its added complexity
warrants before any implementation commitment.

## What this session verified live against actual source (not guessed)

All of the below verified against `src/acie/adapters/python/extract_relations.py`
and `src/acie/indexer.py` at commit `59983b6` (branch
`external-ground-truth-validation-2026-09-05`), read in an isolated git
worktree to avoid touching the shared tree while omp implements G1 (core
memory `12ffacbe`).

### 1. Current self-branch resolution (`extract_relations.py:213-222`)

```python
elif function_node is not None and function_node.type == "attribute":
    object_node = function_node.child_by_field_name("object")
    attribute_node = function_node.child_by_field_name("attribute")
    if object_node is not None and object_node.type == "identifier" and attribute_node is not None:
        object_name = object_node.text.decode("utf-8")
        if object_name == "self" and current_class is not None:
            candidates = methods_by_class.get(current_class.qualname, {}).get(
                attribute_node.text.decode("utf-8"), []
            )
            resolve(attribute_node, source=current_source, candidates=candidates, predicate="calls")
```

`methods_by_class` (built by `_index_methods_by_class`, lines 248-263) only
ever contains methods physically defined in each class's own body, same
file. There is no fallback of any kind on a miss — `resolve()` (line 169)
returns immediately when `candidates` is empty. No edge, no `AMBIGUOUS`, no
deferred record — distinct from every other miss-handling path in this
file, all of which either defer (import-aliased names) or flag ambiguity
(redefinitions).

### 2. The existing base-walk pattern to extend (`_overrides_relations`, lines 372-510)

`_overrides_relations` already does almost exactly what H1 needs, just for
the `overrides` predicate instead of `calls`:

- Same-file immediate bases: resolved directly via `class_candidates_by_name`
  (looked up the same way `_inherits_relations` does, lines 322-325) and
  `methods_by_class` (looked up the same index H1 will also read).
- Cross-file immediate bases (base name not a same-file class but *is*
  `import_alias_map`): produces a `DeferredImportOverride` per (own method,
  deferred base) pair, resolved later by `indexer.py`'s
  `_resolve_deferred_overrides` (lines 204+) against the repo-wide symbol
  index — specifically `find_by_qualname_and_kind(f"{base.qualname}.
  {method_name}", kind="method")`, filtered to `method.path == base.path`
  (a documented 2026-09-02 codex-review fix preventing an unrelated
  same-named class elsewhere in the repo from leaking its own same-named
  method in).
- Multi-base ambiguity: union of every immediate SAME-FILE base's matching
  candidates (not per-base independently), because — per the function's own
  docstring, lines 396-401 — "Python's MRO would pick exactly one candidate
  deterministically, but tree-sitter alone cannot compute MRO linearization
  across multiple bases." Cross-file candidates are explicitly NOT folded
  into that same union (documented shortcut, lines 405-411): a method
  overriding both a same-file and a cross-file base gets two independently-
  confident edges instead of one true joint-ambiguous edge.

H1 reuses this exact resolution shape for `self.<method>()` calls that miss
in the enclosing class's own body.

### 3. Base-name extraction is currently duplicated, not shared

`_inherits_relations` (lines 298-369) and `_overrides_relations` each
independently walk `root.named_children` for top-level `class_definition`
nodes and read the `superclasses` field to get base identifiers. Neither is
a call-out to a shared helper. `_call_and_reference_relations` does not
currently have access to any class's base-name list at all.

## Scope

### In scope

1. **Transitive same-file base chain.** `class A(B): ...`, `class B(C): ...`,
   `self.m()` inside `A` resolving to `C.m` when neither `A` nor `B` define
   `m` — not just the immediate base. Same-file resolution has the full
   symbol table available up front at zero extra repo-wide cost, so there is
   no reason to stop at one hop the way `_overrides_relations` does (that
   function's single-hop-plus-BFS-later design is justified there by
   `impact_analysis` already walking `overrides` edges transitively — a
   `calls` edge from a `self.` site has no such downstream hop; if H1 only
   checked the immediate base, a 2+-level miss would still be silently
   invisible, reproducing the exact bug this spec exists to close).
2. **One level of cross-file base resolution**, via a new deferred item
   (see Design Decision 2) resolved the same way
   `_resolve_deferred_overrides` already resolves `DeferredImportOverride`.
   Explicitly NOT transitive across a cross-file base's own bases — see
   Out of scope.
3. **A shared `_top_level_base_names(root) -> dict[str, list[str]]` helper**,
   extracted from `_inherits_relations`'s and `_overrides_relations`'s
   duplicated AST-walk, used by both of them (replacing their own copies)
   and by the new self-branch logic. Reuse over reinvention (this
   workspace's standing coding-standards rule 4) — do not write a third
   independent copy of the same walk.
4. **Ambiguity via union**, mirroring `_overrides_relations` exactly: if the
   transitive same-file base search finds the method defined in more than
   one reachable base (multi-inheritance, diamond shapes included), every
   same-file candidate found becomes an `AMBIGUOUS` edge — not a "first
   found wins" pick, and not an attempt at real MRO linearization (same
   `tree-sitter can't compute MRO` justification `_overrides_relations`
   already documents).
5. **A cross-file base's candidates are NOT unioned with same-file
   candidates**, for the exact same documented reason
   `_resolve_deferred_overrides` already gives (independent confidence per
   deferred item) — mirror the existing shortcut rather than build the
   "joint same-file+cross-file MRO ambiguity" `_overrides_relations`'s own
   docstring explicitly defers ("revisit if... ever needs modeling" — it
   hasn't, and H1 doesn't need to be the slice that does either).

### Out of scope (named explicitly, not silently dropped)

- **The mixin-composition/reverse-walk case** — H2, not H1. A self-branch
  miss where the enclosing class has no base relationship at all to the
  class that actually defines the method is out of scope here by design;
  H1's fix will correctly still find nothing for that shape, and that is
  the expected, documented boundary between the two slices.
- **Transitive resolution through a cross-file base's own bases** (e.g.
  `class A(Base)` where `Base` is imported, and `Base` itself extends
  `GrandBase` in a third file, and the method lives only on `GrandBase`).
  One level of cross-file deferral only. Flagged as a known residual gap,
  not silently accepted as complete — worth its own future slice if a real
  codebase surfaces it, same treatment G1 gave its own deliberately-deferred
  items.
- **Joint same-file+cross-file MRO ambiguity modeling** — inherited
  out-of-scope status directly from `_overrides_relations`'s own documented
  shortcut; H1 does not attempt to close a gap the capability it's extending
  has never closed either.
- **Non-top-level classes** (nested class definitions) — matches
  `_inherits_relations`'s and `_overrides_relations`'s own existing
  top-level-only scope; not a new limitation H1 introduces.

## Design decisions

1. **New deferred type, not an overload of `DeferredImportOverride`.**
   Considered reusing `DeferredImportOverride` directly for the cross-file
   self-call case (its fields — source, base_name, method_name, module_path,
   site info, provenance — are structurally identical to what a deferred
   self-call needs) by adding a variable `predicate` field. Rejected: this
   codebase's established convention is one dataclass per relation kind
   (`DeferredImportCall` for `calls`, `DeferredImportInherit` for
   `inherits`, `DeferredImportOverride` for `overrides`) — overloading an
   existing type with a predicate parameter would be a wider behavioral
   change touching `overrides`' own call sites and tests for no benefit.
   Add `DeferredImportSelfCall` (or equivalent name — bikeshed at
   implementation time) with the same shape as `DeferredImportOverride`,
   and a new `_resolve_deferred_self_calls` in `indexer.py` that is
   structurally a near-copy of `_resolve_deferred_overrides` but emits
   `predicate="calls"` and sources from the calling method's symbol id
   instead of the overriding method's.
2. **`_top_level_base_names` computed once in `_extract`, passed down.**
   Rather than have `_call_and_reference_relations` re-derive base
   identifiers a third time independently (as `_inherits_relations` and
   `_overrides_relations` currently each do separately), extract the shared
   helper and compute it once at the `_extract` level, passed as a new
   parameter to all three functions. Reorders `_extract`'s existing call
   sequence only in that the shared base-name computation must happen
   before `_call_and_reference_relations` is called (currently
   `extract_relations.py:62`, before `_inherits_relations` at line 73) —
   pure data availability reordering, no semantic change to any of the
   three functions' own existing behavior for non-self cases.
3. **Transitive same-file walk is a plain BFS/DFS over
   `_top_level_base_names`**, visiting each same-file base's own bases in
   turn until either the method is found (recording every match found along
   the way, for the union-ambiguity rule) or the chain is exhausted (no
   same-file base left to check, or the next base is cross-file/undefined,
   at which point the single-level cross-file deferred check applies
   instead — cross-file bases do not get walked further).
4. **Confirm before implementing:** whether cycle protection is needed for
   the same-file BFS (a base cycle would itself be a pre-existing bug
   elsewhere, but H1's own walk should not infinite-loop on one it
   encounters) — a visited-qualname set is the obvious guard; call this out
   explicitly in the implementation rather than assume it's unnecessary.

## Files to touch

- `src/acie/adapters/python/extract_relations.py` — new
  `_top_level_base_names` helper; `_inherits_relations` and
  `_overrides_relations` updated to consume it instead of their own
  duplicated walk (behavior-preserving refactor, not a functional change to
  either); `_call_and_reference_relations`'s self-branch extended per
  Design Decisions 2-3; new `DeferredImportSelfCall` type (likely in
  `acie/ir/relation.py`, alongside the existing three deferred types —
  confirm exact location by reading that file, not assumed here).
- `src/acie/indexer.py` — new `_resolve_deferred_self_calls`, wired into
  `index_file`'s existing deferred-resolution sequence
  (`_resolve_deferred`/`_resolve_deferred_attribute_calls`/
  `_resolve_deferred_overrides` call chain, lines 51-60).
- `tests/adapters/python/test_extract_relations.py` — new tests: same-file
  immediate base, same-file transitive (2+ level) base, same-file diamond
  multi-inheritance ambiguity (union of matches), cross-file immediate base
  (deferred, resolved via a repo-index-backed test matching the existing
  `DeferredImportOverride` test pattern), and a negative case confirming
  the mixin-composition shape from `febf3e07` (unrelated sibling classes,
  no base relationship) still correctly produces no edge under H1 alone —
  this is the explicit scope boundary with H2 and should be asserted, not
  left implicit.
- Likely a mirroring test file for `indexer.py`'s new resolution function,
  matching wherever `_resolve_deferred_overrides` itself is tested.

## Workflow constraints carried into the spec

- **Implement with the `tdd` skill.**
- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the pre-existing known failures at whatever
  baseline count exists at implementation time — reconfirm the current
  baseline live rather than trusting the 764/3 numbers G1's spec recorded,
  since G1 itself may have changed that count by the time H1 starts.
- Never change SALTMDB (or any other target codebase) to accommodate an
  ACIE finding — the fix belongs entirely in ACIE's own extraction code,
  per the acie-usage skill's standing rule.
- Re-verify the exact `DeferredImportOverride`/`DeferredImportInherit`
  field layout and the exact `SymbolStore.find_by_qualname_and_kind` /
  `module_path_matches` signatures directly against source before writing
  the real implementation, rather than trusting this spec's paraphrase —
  same caution G1's own spec applied to its one lower-confidence grammar
  detail.
- Sequencing: do not start H1 while G1 is still an uncommitted in-progress
  working-tree edit in the shared repo (current state as of this spec's
  writing) — wait for G1 to land, or use an isolated worktree the way this
  spec itself was drafted in.

## Verification note

Written against live source read in git worktree
`worktree-spec2-mixin-mro-grounding` (reset to commit `59983b6`, the real
current HEAD of `external-ground-truth-validation-2026-09-05`, rather than
the default `fresh` worktree base which would have branched from
`origin/main` — confirmed stale, 54 commits behind `origin/master`, missing
Capability F entirely). Full read of `_call_and_reference_relations`,
`_index_methods_by_class`, `_inherits_relations`, `_overrides_relations`
(all of `extract_relations.py` lines 117-510); full read of
`indexer.py`'s `index_file`, `_candidates_for`, `_resolve_deferred`,
`_resolve_deferred_attribute_calls`, `_resolve_deferred_overrides` (lines
1-230+); grep confirmation of every existing `test_self_method_call_*` and
`test_override_*` scenario in `test_extract_relations.py`. SALTMDB memory
`f9f7927f` records the grounding session this spec was drafted from.
