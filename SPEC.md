# SPEC: Tier-4 lazy staleness check must not tombstone INFERRED relations

Status: LOCKED — ready for OMP/Luna implementation (TDD, red-green-refactor).
Source finding: SALTMDB memory `daa3157f` (bug), `8a1a0a7b` (sweep), `5dd04904`
(paired coordinate-error correction, informational only — no code change needed
for that one). ACIE Full Tool Sweep, 2026-09-08.

## 1. Problem statement

`find_references`/`get_definition` called with `position={file, line, column}`
(and, identically, `list_imports(file=...)`) trigger a synchronous "tier 4 lazy
staleness check" (`ensure_fresh` in `src/acie/daemon/runtime.py`) that
re-extracts the named file via `make_reindex_job` → `index_file` before
answering the query. This re-extraction is **tree-sitter-only**: it can never
reproduce a `Confidence.INFERRED` relation, because INFERRED relations are
produced exclusively by the separate, out-of-band basedpyright enrichment pass
(`src/acie/daemon/lsp_enrichment.py::run_enrichment_pass`).

`index_file`'s tombstone logic (`src/acie/indexer.py`) diffs *every* relation
currently sited in the file against what the fresh extraction just produced,
and deletes whatever isn't reproduced:

```python
prior_relation_keys = {_relation_key(r) for r in relation_store.list_by_site_file(path)}
...
removed_relation_keys = prior_relation_keys - {_relation_key(r) for r in new_relations}
for source, target, predicate, site_file, site_line, site_col in removed_relation_keys:
    relation_store.delete(...)
```

Because `new_relations` here can never contain an INFERRED relation, **every**
live cross-file INFERRED relation sited in the queried file is unconditionally
deleted the first time tier 4 fires for that file (confirmed live: a `calls`
edge with `confidence: INFERRED` flipped to `"deleted": true` immediately
after a single `find_references(position=...)` call at that edge's own site;
`find_references(symbol_id=...)`'s `total_count` dropped 171 → 170 in the same
session with no other write). This is a **read-only query silently destroying
unrelated, correct data**, not a stale-but-safe answer.

This is worse than a self-healing race: `ensure_fresh` submits the reindex job
directly to the `WriteQueue` and never notifies `EnrichmentScheduler`, so
nothing re-derives the destroyed INFERRED relation afterward. It stays gone
until some *unrelated* future trigger (a real edit elsewhere, a daemon
restart's reconciliation pass, a manual `acie scan`) happens to run a full
enrichment pass over the repo again.

(The ordinary filesystem-watcher path — a real edit — shares the same
`index_file` tombstone logic and therefore the same momentary loss, but it
already calls `scheduler.on_watcher_edit(...)` afterward, which schedules a
debounced full-repo enrichment pass that repairs it within `quiet_seconds`
(30s) to `max_wait_seconds` (300s). That self-healing path is **out of scope**
for this spec — see §5.)

## 2. Root cause (confirmed by direct source read, not inference)

`index_file()` (`src/acie/indexer.py`) has no concept of "this extraction pass
cannot possibly prove or disprove an INFERRED relation" — it treats the
absence of a relation from `new_relations` as proof the relation is gone,
which is only true for EXTRACTED/AMBIGUOUS relations (tree-sitter's own
output). Compare `src/acie/daemon/merge_policy.py::apply_enrichment_write`,
which the enrichment pass actually uses to add/retire INFERRED relations: it
never blanket-diffs a file's relations, it only ever retires a same-site
AMBIGUOUS sibling of a relation it just confirmed. `index_file`'s per-file
diff is a strictly blunter instrument, and it was never meant to run against
a file whose relation set includes enrichment-only facts it cannot recompute.

## 3. Fix

Add a keyword-only parameter to `index_file`, defaulting to today's behavior
so every existing caller and every existing test is unaffected unless it
explicitly opts in:

```python
def index_file(
    path: str,
    source_text: str,
    observed_at: str,
    symbol_store: SymbolStore,
    relation_store: RelationStore,
    index_meta_store: IndexMetaStore,
    *,
    prune_inferred: bool = True,
) -> IndexResult:
```

When `prune_inferred=False`, exclude relations whose `confidence ==
Confidence.INFERRED` from `prior_relation_keys` (equivalently: never place an
INFERRED relation sited in `path` into `removed_relation_keys`).

- The cross-file `stale_cross_file_relations` sweep (relations elsewhere in
  the repo targeting a symbol just removed from `path`) is **not** exempted
  by `prune_inferred` — a symbol's removal from `path` is a fact tree-sitter
  *can* prove (via `extract_symbols`), so a relation targeting a
  now-nonexistent symbol is genuinely dangling regardless of its confidence,
  and must still be tombstoned. **This requires one correction to today's
  implementation, not just a pass-through:** the sweep currently skips a
  candidate via a location proxy —
  `if relation.site_file == path: continue` — reasoning that any such
  same-site relation was "already removed by the diff above." That proxy is
  only valid when the same-site diff considers every confidence level, which
  is true today but stops being true once `prune_inferred=False` carves
  INFERRED relations out of that diff. Left unpatched, an INFERRED relation
  sited in `path` whose target symbol is *also* defined in and removed from
  `path` (a same-file self-referencing edge — plausible for
  `lsp_enrichment.py`'s mixin/composition resolution) would be skipped by
  **both**
  mechanisms: excluded from the diff (INFERRED, `prune_inferred=False`) and
  skipped by the sweep (`site_file == path`) — never tombstoned, silently
  contradicting the "removal is unconditional" rule this bullet just stated.

  Fix the sweep to check actual membership in the diff's own result instead
  of approximating it by location — compute `removed_relation_keys` first
  (as today, respecting the `prune_inferred` exclusion above), then:

  ```python
  for symbol_id in removed_symbol_ids:
      for relation in relation_store.list_by_target(symbol_id):
          if _relation_key(relation) in removed_relation_keys:
              continue  # already deleted by the same-site diff above
          relation_store.delete(...)
          stale_cross_file_relations += 1
  ```

  This is behavior-identical to today's `site_file == path` check whenever
  `prune_inferred=True` (provably: a relation with `site_file == path`
  targeting a symbol just removed from `path` can never be reproduced by the
  fresh extraction under the default full diff, so it is always already a
  member of `removed_relation_keys` in that case — the two conditions
  coincide). It only diverges — correctly — when `prune_inferred=False`
  left an INFERRED same-site relation out of the diff, which is exactly the
  gap this correction closes. **Governing contract for §6 items 3-4 (the
  question OMP raised):** a relation targeting a genuinely-removed symbol
  must always be tombstoned regardless of confidence AND regardless of
  whether its site is
  in `path` or elsewhere — that invariant governs, and the sweep is
  corrected as above to actually satisfy it in every case, not just the
  cross-file one it happened to cover before `prune_inferred` existed.
- `IndexResult.relations_tombstoned` still reports however many rows were
  actually deleted (i.e., it naturally reports fewer when `prune_inferred`
  held some back — no separate counter needed).
- Docstring: add a short paragraph to `index_file`'s docstring (currently has
  none beyond the module-level comments; add one) stating the contract of
  `prune_inferred` in the terms above — future readers must not have to
  reconstruct this reasoning from the diff alone.

### 3.1 Call site: `make_reindex_job` (`src/acie/daemon/watcher.py`)

Add the same keyword-only parameter, threaded only into the **modify/reindex
branch** (the `index_file(path=rel_path, source_text=source_text, ...)` call
around line 137-141). Do **not** add it to the delete branch's
`index_file(path=rel_path, source_text="", ...)` call (around line 94-98) —
when the file is genuinely gone, every relation sited there, INFERRED
included, is correctly stale and must still be tombstoned; that branch keeps
today's default (`prune_inferred=True`, i.e. omit the argument there
entirely).

```python
def make_reindex_job(
    repo_root: str, rel_path: str, *, prune_inferred: bool = True,
) -> Callable[[sqlite3.Connection], None]:
    ...
    def job(conn: sqlite3.Connection) -> None:
        ...
        # delete branch: index_file(..., source_text="", ...) -- UNCHANGED,
        # no prune_inferred argument, keeps default True.
        ...
        # modify branch:
        index_file(
            path=rel_path, source_text=source_text, observed_at=observed_at,
            symbol_store=SymbolStore(conn=conn), relation_store=RelationStore(conn=conn),
            index_meta_store=IndexMetaStore(conn=conn),
            prune_inferred=prune_inferred,
        )
        ...
```

Default `prune_inferred=True` on `make_reindex_job` itself means every
existing caller (the filesystem watcher, `_run_triggered_enrichment`'s
migration/reconciliation paths if any call it, any existing test) is
byte-for-byte unaffected unless it explicitly passes `prune_inferred=False`.

### 3.2 Call site: `ensure_fresh` (`src/acie/daemon/runtime.py`)

This is the only call site that should ever pass `prune_inferred=False` — it
is the one place a read-only query triggers reindexing with no paired
enrichment-repair step:

```python
future = write_queue.submit(
    repo_id, make_reindex_job(repo_root, rel_path, prune_inferred=False),
)
```

No other change to `ensure_fresh`'s signature, error handling, or timeout
behavior.

## 4. Why this scope, not a broader one

Three fix shapes were on the table (SALTMDB memory `daa3157f`, "Recommended
next step"):

(a) exclude cross-file INFERRED relations sited in a file from the
    reconciliation diff,
(b) make the lazy staleness check run the full enrichment pass instead of
    tree-sitter extraction,
(c) stop invoking any staleness/reconciliation write path from a read-only
    tool call — make position resolution genuinely read-only.

This spec locks **(a)**, scoped narrowly to the tier-4 call site only (not the
watcher's real-edit path — see below), for these reasons:

- **(b)** is far more expensive per query (a full LSP `textDocument/definition`
  round trip through `pyright_process` on every single position/list_imports
  lookup) for a guarantee the caller doesn't need: tier 4 exists to make a
  *narrow* single-file answer fresh enough, not to keep the whole repo's
  enrichment graph authoritative on every read.
- **(c)** (skip tier 4's reindex/write step entirely for position/list_imports)
  would reopen the exact staleness bug tier 4 was built to close (DAEMON.md
  "Incremental Indexing Wiring") — a `list_imports(file=...)` or
  `position=...` call right after an edit tier 1-3 haven't caught yet would
  go back to answering from a stale index. (a) keeps tier 4's freshness
  guarantee for EXTRACTED/AMBIGUOUS data (what tree-sitter *can* prove) while
  removing its destructive blind spot for INFERRED data (what it can't).
- **Scoping (a) to `ensure_fresh` only, not also to the watcher's real-edit
  path**: the watcher's tombstone-then-reenrich window is already
  self-healing (via `scheduler.on_watcher_edit`), and changing its pruning
  behavior too raises a **separate, harder question this spec does not
  settle**: if a real edit deletes the last call site that used to justify an
  INFERRED relation, what retires that now-genuinely-stale relation, if not
  `index_file`'s per-file diff? (`_worklist` in `lsp_enrichment.py` only
  *adds/reconfirms* sites, it does not sweep for INFERRED relations whose
  site no longer exists in source.) That's a real design question but it is
  not the bug this sweep found — the reported bug is specifically "a read
  query corrupts data," which tier-4-only scoping fixes completely. Leave the
  watcher path exactly as it is.

## 5. Explicitly out of scope

- Changing `make_reindex_job`'s delete-branch behavior (must keep tombstoning
  everything, INFERRED included, when the file is actually gone).
- Changing the filesystem-watcher's real-edit reindex behavior in any way.
- The coordinate-error correction from memory `5dd04904` (position mode
  resolving to a symbol's own `start_col`, not its identifier's column) —
  that was a test methodology finding, not a code defect; no fix needed.
- `architecture(root=<absolute path>)` (memory `5021d1a9`) — separate spec.

## 6. Required tests (write first, red before green)

### `tests/test_indexer.py`

Add near the existing `test_removing_a_symbol_from_source_tombstones_it_and_its_relations`
family:

1. `test_prune_inferred_false_preserves_an_inferred_relation_the_fresh_extraction_cannot_reproduce`
   — seed an INFERRED relation sited in a file directly via
   `relation_store.upsert(Relation(..., confidence=Confidence.INFERRED, ...))`
   (a `calls`/`references` edge whose site is *not* something plain
   tree-sitter extraction of that same source would produce on its own —
   e.g. simulate a resolved-via-LSP cross-file call the way
   `lsp_enrichment.py` would write it). Call `index_file(..., prune_inferred=False)`
   with the file's *unchanged* source. Assert the INFERRED relation is still
   present and not `deleted`.
2. `test_prune_inferred_true_still_removes_a_stale_inferred_relation_by_default`
   — same setup, call `index_file(...)` with `prune_inferred` omitted
   (default `True`). Assert the relation IS removed — proves the default is
   unchanged from today's behavior (no regression for any existing caller).
3. `test_prune_inferred_false_still_tombstones_a_cross_file_relation_whose_target_symbol_was_removed`
   — an INFERRED relation sited in a *different* file (`caller.py`) targeting
   a symbol defined in `path`; reindex `path` with that symbol genuinely
   deleted from its new source. Assert the cross-file relation is still
   tombstoned even with `prune_inferred=False` — this is the ordinary
   `stale_cross_file_relations` sweep, `site_file != path`, unaffected by
   this spec's change either before or after the §3 correction.
4. `test_prune_inferred_false_still_tombstones_a_same_file_relation_whose_target_symbol_was_removed`
   — the case that actually exercises §3's sweep correction: an INFERRED
   relation sited **in `path` itself**, targeting a symbol **also defined in
   and removed from `path`**'s new source (a same-file self-referencing
   edge). Assert it is still tombstoned with `prune_inferred=False`. Without
   §3's fix to the sweep (checking `removed_relation_keys` membership
   instead of `site_file == path`), this relation is skipped by both the
   diff (INFERRED, excluded) and the sweep (`site_file == path`, wrongly
   assumed already handled) and survives — this test must fail on an
   implementation that only adds the `prune_inferred` parameter without also
   correcting the sweep.
5. `test_prune_inferred_false_still_removes_a_stale_extracted_relation`
   — an EXTRACTED (not INFERRED) relation sited in `path` whose call site is
   actually deleted from the new source. Assert it's still removed even with
   `prune_inferred=False` — proves the parameter only carves out INFERRED,
   not tree-sitter's own authoritative EXTRACTED/AMBIGUOUS diff.

### `tests/daemon/test_watcher.py`

6. `test_watch_job_with_prune_inferred_false_preserves_an_inferred_relation_on_reindex`
   — `make_reindex_job(repo_root, "mod.py", prune_inferred=False)`, same
   INFERRED-seeding approach as test 1, assert survival after the job runs
   against unchanged-but-newly-mtime'd content (the job's mtime/hash check
   must actually reach `index_file`, i.e. change mtime or omit prior
   `FileStateStore` state so the reindex isn't skipped by decision 1's cheap
   check).
7. `test_watch_job_default_prune_inferred_still_tombstones_on_real_watcher_edits`
   — `make_reindex_job(repo_root, "mod.py")` (no `prune_inferred` argument at
   all), same INFERRED seed, assert it IS removed — confirms the watcher's
   own real-edit call site is unaffected by this change (matches §3.1's "do
   not touch the watcher's own default" decision explicitly, not just by
   omission).
8. `test_watch_job_delete_branch_tombstones_an_inferred_relation_regardless_of_prune_inferred`
   — seed an INFERRED relation sited in `mod.py`, then run the *delete*
   branch (remove the file from disk, run the job). Assert the relation is
   gone — confirms §3.1's explicit exclusion of the delete branch from the
   new parameter.

### `tests/daemon/test_runtime.py`

9. `test_ensure_fresh_position_lookup_does_not_tombstone_a_live_inferred_relation`
   — end-to-end through `dispatch()`/`ensure_fresh` (mirror the existing
   `test_runtime_get_definition_by_position_sees_a_fresh_edit_with_no_wait_for_the_watchers_debounce`
   fixture setup): seed a repo where a file has one INFERRED relation sited
   in it (write it directly into the index the same way that existing test
   seeds its fixture, or via a fake/stub enrichment write — whatever this
   test file's existing INFERRED-seeding convention is; if none exists yet,
   add a small helper rather than duplicating one inline per test). Issue a
   `find_references`/`get_definition` call with `position=` pointing at an
   *unrelated* line in the same file (not the INFERRED relation's own site —
   the bug is that the whole file's INFERRED relations die, not just the
   queried site). Assert the INFERRED relation is still present and
   undeleted immediately after the call returns. This is the direct
   regression test for the exact bug reproduced in memory `daa3157f`.
10. `test_ensure_fresh_list_imports_does_not_tombstone_a_live_inferred_relation`
    — same shape as test 9, but through `list_imports(file=...)`, since
    `staleness.py` documents it as sharing tier 4's exact scope.

Run the repo's actual test command (`uv run pytest`, or whatever
`CONTRIBUTING`/CI uses — check before assuming) as the mandatory final step;
paste real failures, fix, re-run, never declare done on assumed correctness
(Coding Standards rule 18).

## 7. Non-goals / do not touch while here

- Do not refactor `index_file`'s existing diff logic beyond adding the one
  exclusion described in §3.
- Do not touch `merge_policy.py` or `lsp_enrichment.py` — this fix does not
  change how INFERRED relations are added, only how they can (and cannot) be
  removed by tier 4.
- Do not change `_LAZY_STALENESS_TIMEOUT_SECONDS` or any other tier-4 timing.
- Do not reorder imports or touch unrelated functions in any file this spec
  touches (Coding Standards rule 6).

## 8. Definition of done

- All 10 new tests pass; full existing suite still passes with no regressions.
- `git diff` shows changes confined to: `src/acie/indexer.py`,
  `src/acie/daemon/watcher.py`, `src/acie/daemon/runtime.py`, and the three
  test files above. No incidental changes elsewhere.
- Manual sanity check against the report's own repro shape is welcome but not
  required — the automated tests above are the actual acceptance bar.
