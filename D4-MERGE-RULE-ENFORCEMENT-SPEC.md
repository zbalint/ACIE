# v1 Slice D4 — Merge-Rule Enforcement

Status: spec-and-plan only, written for external implementation (same role
split as D1–D3 — this document was **not** implemented in the session that
wrote it; a fresh session should implement it, and another fresh session
should review the resulting diff against this spec before commit).

Implements the sixth piece of Capability D (wayfinder ticket `89be4cc1`)
per the locked D1–D6 breakdown (SALTMDB memory `3627eece`): **the merge-rule
enforcement slice** — "upgrade AMBIGUOUS/unresolved → INFERRED only, never
overwrite a clean EXTRACTED fact," made a real write-time-enforced
invariant instead of an incidental property of D3's own site-selection
logic — plus retiring a resolved `AMBIGUOUS` site's now-stale sibling
candidate rows, the one deliberate gap D3 itself named and left open
(`ad9a3675`, `D3-LSP-BACKGROUND-ENRICHMENT-SPEC.md`'s "What D3 does not
do," `393928e8`'s review sign-off, and `DAEMON.md` line 134's own "stale-
candidate cleanup remain[s]... D4 work").

## Why D4 is next

D3 is committed (`b8c69c5`) and independently review-signed off (memory
`393928e8`): `run_enrichment_pass` resolves `AMBIGUOUS`/unresolved
`calls`+`inherits` sites and submits each winning relation through the
per-repo `WriteQueue` as `confidence=INFERRED`, but every write still goes
straight to `RelationStore.upsert()` with no merge-policy layer above it,
and no code anywhere retires a resolved site's losing `AMBIGUOUS`
candidates. D3's own review explicitly named D4 as "next" (`393928e8`'s
"Next per the locked breakdown" section) with exactly this scope.

Baseline reconfirmed this session: `.venv/bin/pytest` → **707 passed, 3
failed** (the same pre-existing election-port flake tracked since slice
A1, unchanged: `test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down`,
`test_daemon_stop_actually_terminates_the_os_process`,
`test_serve_mcp_exposes_and_routes_the_ten_tools`).

## What this session verified live against actual source (not guessed)

### 1. Sibling `AMBIGUOUS` rows are real, and share everything but `target`

`RelationStore`'s primary key is `(source, target, predicate, site_file,
site_line, site_col)` (`relation_store.py` lines 10–22). Confirmed by
reading every AMBIGUOUS-producing call site directly
(`extract_relations.py` lines 163–179 for same-file `calls`/`references`,
lines 454–470-ish for same-file `overrides`, and `indexer.py`'s
`_resolve_deferred` lines 131–158 for cross-file deferred `calls`/
`inherits`): whenever a site has more than one same-qualname candidate,
**one relation row is inserted per candidate**, all sharing the exact same
`(source, predicate, site_file, site_line, site_col)` and all stamped
`confidence=Confidence.AMBIGUOUS` — only `target` differs between them.
This is exactly the "two `AMBIGUOUS` rows at one site, targets A and B"
scenario D3's own spec and review already named. D3's `_worklist` builder
already collapses these into one `_Site` per (source, site, predicate)
tuple (`_Site` has no `target` field — `lsp_enrichment.py` lines 28–34),
so one pyright query already covers a whole sibling group; the gap is
purely on the *write* side.

### 2. An EXACT `EXTRACTED`-key collision with a D3 write is not reachable today — the guard is a genuine forward-looking invariant, not currently a no-op-detector

Read `extract_relations.py`'s `resolve()` (lines 163–179) and
`indexer.py`'s `_resolve_deferred` (lines 131–158) in full: within either
function, a single call site's confidence (`EXTRACTED` if exactly one
candidate, else `AMBIGUOUS`) is computed **once** and applied to every
relation row produced for that site — a site's rows are never a mix of
`EXTRACTED` and `AMBIGUOUS`. And a site is resolved by *exactly one* of
same-file resolution (`extract_relations.py`) or cross-file deferred
resolution (`indexer.py`), never both (`resolve()`'s own docstring: a
zero-same-file-candidate call becomes a `DeferredImportCall` instead).
D3's worklist only ever draws from currently-`AMBIGUOUS` rows or currently
*unpersisted* (zero-candidate) deferred items — never from an `EXTRACTED`
key. So today, no code path can make D3 resolve a site whose exact write
key already coincides with a live `EXTRACTED` row. **This means D4's
guard is currently unexercised by any real caller** — it exists to make
"never regress `EXTRACTED`" a write-time-enforced policy (matching the
locked breakdown's literal wording, which states it as a rule, not an
accident of D3's current site-selection) rather than leaving the
invariant to depend forever on every future caller of the merge-policy
layer re-deriving D3's own site-selection discipline correctly. A future
slice that broadens site selection (or a bug) is exactly the case this
guard exists for.

### 3. `Confidence` already has an undocumented-but-real ordinal use, currently private and duplicated if D4 adds its own

`src/acie/tools/confidence.py` already ranks `Confidence` for
`min_confidence` filtering: `_RANK = {EXTRACTED: 0, INFERRED: 1, AMBIGUOUS:
2}` (most-certain-first), explicitly scoped by its own docstring as "a
filter-only rank, not a reinterpretation of the [non-ordinal] taxonomy."
D4's merge rule needs the exact same ordering — "never let a write set a
*less* certain confidence than what's already there" is the general form
of "never overwrite `EXTRACTED`" — so this is now a **second** genuine
need for the same ordinal concept (Coding Standard 4/16: extend, don't
duplicate). See design decision 1.

## Scope (narrow, deliberately not D5/D6)

One new module, `src/acie/daemon/merge_policy.py`
(`apply_enrichment_write`), one small promotion of an existing private
ranking dict from `tools/confidence.py` into `acie/ir/symbol.py` as a
public `confidence_rank()` helper (both `tools/confidence.py` and the new
module then share one canonical ranking — no second copy), and the single
necessary integration point in D3's own committed write path
(`lsp_enrichment.py`'s job-building helper calls the new policy function
instead of raw `RelationStore.upsert()`). Restricted to the exact same
`calls`+`inherits` scope D3 itself is restricted to — `overrides` is not
touched (D3 never produces `overrides` writes, so there is nothing for D4
to guard there yet). Does not touch `runtime.py` / `dispatch.py` /
`mcp_server.py` / `bootstrap.py` (D6's job), no `acie scan` CLI (D5), no
MCP surface, no `lsp_client.py` / `lsp_protocol.py` / `symbol_store.py`
changes (nothing here needs them).

### What D4 does **not** do (named explicitly, not silently dropped)

- **Cross-pass re-ambiguation cleanup.** If a *later* enrichment pass
  resolves an already-`INFERRED` site to a *different* target than an
  *earlier* pass did (only reachable if an intervening reindex makes the
  site newly `AMBIGUOUS` or newly zero-candidate again — see D3's own
  worklist categories, `ad9a3675` design decision 3), the stale `INFERRED`
  row from the earlier pass is **not** retired by this design — only
  `AMBIGUOUS` siblings are retired (see design decision 3). This matches
  the locked breakdown's literal wording ("retiring a resolved AMBIGUOUS
  site's now-stale sibling candidate rows") exactly, rather than silently
  broadening it. Marked `# shortcut:` in `merge_policy.py` with an upgrade
  trigger: a real repo where this cross-pass scenario is observed to
  leave a visibly wrong stale `INFERRED` fact around (not expected before
  D6 exists, since nothing schedules repeat passes yet).
- **The staleness-window follow-up from D3 review (memory `87e7e80b`).**
  That follow-up is about *when* an enrichment pass runs relative to
  `walk_repo`'s live-disk read vs. `relations_live`'s last-indexed state —
  a trigger-*cadence* question. D4's guard operates only on
  `relation_store` state read at write time, inside the write-queue job
  (see design decision 2) — it cannot make a stale `walk_repo` snapshot
  fresh, no matter how the write-time policy is designed. **Decision:
  `87e7e80b` stays D6's to own**, exactly as that memory's own "Action for
  whoever specs/reviews D6" section already directs — D4 does not
  silently absorb it, and does not silently drop it either. One
  incidental mitigation *is* a side effect of D4's `EXTRACTED`-regression
  guard: a stale write can never clobber a freshly-reindexed `EXTRACTED`
  fact, guard or no guard involved in that specific window — but that is
  a narrower guarantee than "the staleness window is closed," and this
  spec does not claim the broader one.
- **Wrapping the winning upsert and every sibling retirement in one
  database transaction.** Each `RelationStore.upsert()`/`delete()` call
  already commits on its own (existing methods, unchanged). A crash
  between the winning write and a sibling's retirement leaves that one
  stale `AMBIGUOUS` sibling in place — harmless and self-healing: it just
  re-enters D3's own worklist on the next pass (same "AMBIGUOUS site"
  category as before), gets re-queried, resolves to the same winning
  target again (an idempotent no-op `upsert`), and this time is retired
  cleanly. This mirrors `indexer.py`'s own established "reindex-on-edit
  eventually closes that gap" precedent for deferred-candidate misses —
  not a new kind of eventual consistency this project hasn't already
  accepted elsewhere. `# shortcut:` no cross-call transaction wrapping —
  upgrade trigger: a real observed case where a crash mid-retirement
  leaves a visibly wrong state longer than "the next pass," which
  shouldn't be possible given the self-healing argument above; reaching
  into `RelationStore`'s private connection from outside it to force one
  bigger transaction would also be a worse abstraction leak than the risk
  it removes.
- **Changing `run_enrichment_pass`'s `resolved: list[Relation]` return
  contract to reflect per-site merge-policy outcome.** See design
  decision 4 — deliberately kept as-is.

## Design decisions

1. **Promote the existing private confidence ranking to a shared, public
   helper: `acie.ir.symbol.confidence_rank(confidence: Confidence) -> int`.**
   Moves `tools/confidence.py`'s `_RANK` dict (unchanged values:
   `EXTRACTED: 0, INFERRED: 1, AMBIGUOUS: 2`, most-certain-first) into
   `ir/symbol.py` — the module `Confidence` itself already lives in, and
   already a dependency of both `tools/confidence.py` and
   `daemon/lsp_enrichment.py` today, so this introduces no new
   cross-layer edge. `tools/confidence.py` is updated to import and call
   `confidence_rank()` instead of keeping its own copy of the dict —
   behavior-preserving, its existing test file
   (`tests/tools/test_confidence.py`) must pass unmodified. The new
   docstring on `confidence_rank()` explicitly names both call sites
   (`tools/confidence.py`'s min-confidence filter, `daemon/merge_policy.py`'s
   regression guard) and reiterates ARCHITECTURE.md's "non-ordinal
   taxonomy" framing so a third caller doesn't casually reinterpret it
   into a general certainty score. This is the one small, justified
   refactor-adjacent touch to an already-existing file outside D3/D4's
   own new modules — motivated directly by Coding Standard 4 ("extend
   rather than duplicate," now triggered a second real time, not
   speculative).

2. **`src/acie/daemon/merge_policy.py`, new module. Single entry point:
   `apply_enrichment_write(relation_store: RelationStore, relation:
   Relation) -> MergeOutcome`,** called from *inside* a write-queue job
   (design decision 5) — i.e. on the repo's single writer thread, using
   that job's own already-open `conn`-backed `RelationStore`. This is
   deliberate, not incidental: `WriteQueue`'s own docstring and `_run`
   loop (`write_queue.py` lines 273–308) establish that exactly one job
   runs at a time per repo, strictly serially, on one dedicated thread
   reusing one connection — so a read-then-write sequence *inside* a
   single job is race-free against any other job for that repo by
   construction, with no new locking needed. Reading current state
   *outside* the write queue (e.g. in `run_enrichment_pass`'s own
   worklist-building code, before submission) and deciding there instead
   would reopen exactly the kind of read-then-write race D3 itself
   already accepts for worklist *discovery* (worst case: a slightly stale
   worklist, self-healing next pass) but which is not acceptable for a
   *correctness guarantee* like "never regress EXTRACTED" — that
   guarantee needs to be checked against the actual, current, about-to-be
   -committed state, which only the write-queue job itself can see
   atomically relative to every other write for that repo.

   ```python
   @dataclass(frozen=True)
   class MergeOutcome:
       applied: bool
       retired_siblings: int
       reason: str | None = None  # set only when applied is False
   ```

3. **The guard (regression check).** `apply_enrichment_write` first calls
   `relation_store.get(source=relation.source, target=relation.target,
   predicate=relation.predicate, site_file=relation.site_file,
   site_line=relation.site_line, site_col=relation.site_col)` — the
   *existing* `RelationStore.get()`, an exact full-primary-key lookup, no
   new store method needed. If `existing is not None and
   confidence_rank(relation.confidence) > confidence_rank
   (existing.confidence)` (the incoming write is *less* certain than what
   is already there — the general form of "would overwrite EXTRACTED with
   INFERRED"), the write is refused: log at `WARNING` naming the site and
   both confidences, return `MergeOutcome(applied=False,
   retired_siblings=0, reason="would_regress_existing_confidence")`, and
   do **not** call `upsert()` or touch any sibling row. Otherwise (no
   existing row, or existing is at least as uncertain as the incoming
   write — covers `AMBIGUOUS`→`INFERRED` upgrades and idempotent
   `INFERRED`→`INFERRED` re-observation on a later pass) proceed to
   decision 4.

4. **The write + sibling retirement, in that order.** `relation_store
   .upsert(relation)` (existing method, unchanged) first — the winning
   fact is durably present before anything is deleted, so a crash between
   these two steps never loses the resolution (see "What D4 does not do"
   for why not wrapping both in one transaction is an accepted,
   self-healing shortcut). Then `relation_store.list_by_site
   (site_file=relation.site_file, site_line=relation.site_line,
   site_col=relation.site_col, predicates={relation.predicate})` —
   **the existing exact-point query, already used today by
   `get_definition`'s own position resolution** — filtered in Python to
   siblings where `sibling.source == relation.source and sibling.target
   != relation.target and sibling.confidence == Confidence.AMBIGUOUS`,
   and each matching sibling is retired via the *existing*
   `relation_store.delete(...)` (which already writes a `tombstone=1`
   `relations_history` row and removes the `relations_live` row — no new
   storage method or schema change anywhere in this slice), using
   `relation.provenance.observed_at` as `delete()`'s required
   `observed_at` (the same clock read already made for this site's own
   `Relation`, not a second one — keeps this deterministic under the same
   `observed_at_fn` seam D3 already established, no new seam needed).
   Both `source`-equality and `confidence == AMBIGUOUS` filters are
   deliberate, not redundant: the former is defense-in-depth matching the
   full primary key even though a site's `(site_file, site_line,
   site_col)` alone should already uniquely pin one call/inherit
   expression in practice; the latter is the literal scope boundary named
   in "What D4 does not do" above (an `INFERRED` row at the same site
   from a hypothetical earlier pass is never touched by this call).
   Returns `MergeOutcome(applied=True, retired_siblings=<count>)`.

5. **Integration point in `lsp_enrichment.py` (the one necessarily-touched
   line in D3's own committed file).** `_make_upsert_job` (private helper,
   renamed `_make_merge_job` for clarity since its role changes) currently
   does:

   ```python
   def _make_upsert_job(relation: Relation):
       def job(conn) -> None:
           RelationStore(conn=conn).upsert(relation)
       return job
   ```

   becomes:

   ```python
   def _make_merge_job(relation: Relation):
       def job(conn) -> merge_policy.MergeOutcome:
           return merge_policy.apply_enrichment_write(RelationStore(conn=conn), relation)
       return job
   ```

   (`merge_policy` imported at module level, alongside D3's existing
   imports.) The call site (`run_enrichment_pass`'s
   `submitted.append(write_queue.submit(repo_id, _make_upsert_job
   (relation)))`) is updated to the new name only — no other line in
   `run_enrichment_pass` changes. This is the *only* edit to
   `lsp_enrichment.py`'s existing control flow; every other line (worklist
   building, response mapping, per-site error handling, the
   `submitted[-1].result()` drain-wait) is untouched, matching Coding
   Standard 6.

6. **`run_enrichment_pass`'s `resolved: list[Relation]` return contract is
   deliberately left unchanged**, still meaning "submitted for write this
   pass" (D3's own decision 6 already documents this as "the pass —
   writes included — has actually drained, not just that LSP traffic
   finished," never as "every write's merge-policy outcome was
   individually confirmed"). Retrofitting `resolved` to reflect each
   job's actual `MergeOutcome` would require waiting on *every* submitted
   `Future` (not just the last one, per D3's own decision 6's shortcut) —
   a real behavior change to already-reviewed D3 control flow, out of
   proportion to a guard proven in decision 2 above to be currently
   unreachable by any real caller. The guard's outcome is observable via
   its `WARNING` log line and via `merge_policy.apply_enrichment_write`'s
   own return value (exercised directly by this slice's unit tests, not
   through `run_enrichment_pass`). Named explicitly here, not silently
   glossed over, since it is a real, deliberate scope boundary — same
   spirit as D3's own "what D3 does not do" section.

## Files to touch

1. `src/acie/daemon/merge_policy.py` (new) — `MergeOutcome`,
   `apply_enrichment_write`, and a small private `_retire_stale_siblings`
   helper.
2. `src/acie/ir/symbol.py` — add `confidence_rank()` (design decision 1).
3. `src/acie/tools/confidence.py` — replace the local `_RANK` dict/lookup
   with `confidence_rank()` calls; delete the now-redundant dict.
4. `src/acie/daemon/lsp_enrichment.py` — rename `_make_upsert_job` to
   `_make_merge_job`, change its body to call
   `merge_policy.apply_enrichment_write` (design decision 5), add the
   `merge_policy` import. No other line changes.
5. `tests/daemon/test_merge_policy.py` (new) — the bulk of new coverage,
   against a real in-memory `RelationStore(":memory:")` (no fakes needed
   — this module has no LSP/process/queue dependency at all): existing
   row is `EXTRACTED` → write refused, DB unchanged, correct
   `MergeOutcome`; existing row is `AMBIGUOUS` → write applied; existing
   row is `INFERRED` (idempotent second-pass re-observation) → write
   applied; no existing row (the "unresolved"/zero-candidate case) →
   write applied, `retired_siblings == 0`; a same-site `AMBIGUOUS`
   sibling with a *different* target is retired (gone from
   `list_by_site_file`/`list_by_site`, present with `tombstone=1` via
   `RelationStore.history`); a same-site `AMBIGUOUS` row with the *same*
   target as the winner is not double-counted as a retired sibling (it's
   the row being upgraded, not a stale one); a same-site row from a
   *different* `source` is never retired; a same-site `INFERRED` row
   (simulating the cross-pass re-ambiguation corner named in "What D4
   does not do") is confirmed **not** retired, documenting the scope
   boundary as an executable test, not just prose.
6. `tests/daemon/test_lsp_enrichment.py` — one new integration test that
   builds the real (non-fake) job via `lsp_enrichment._make_merge_job`
   (or exercises it indirectly through a real, non-fake `WriteQueue`
   against a `tmp_path` sqlite file rather than `FakeWriteQueue`, whose
   `submit()` never actually calls the job — confirmed by reading
   `FakeWriteQueue.submit()` in the existing test file, lines 23–31: it
   only records `(repo_id, job, future)` and immediately resolves the
   future with `None`, so today's D3 tests never execute the job closure
   against a real connection at all) — seeds an `EXTRACTED` relation at
   the exact key a contrived resolution would target, executes the
   submitted job for real, and asserts the `EXTRACTED` row is untouched
   in the database afterward. This is the one test in this slice proving
   the guard is actually wired into the real write path, not merely unit
   -tested in isolation against `merge_policy.py` directly.
7. `tests/tools/test_confidence.py` — no new test required; must pass
   unmodified after decision 1's refactor (confirms `confidence_rank()`'s
   promotion is byte-identical in behavior for `filter_by_min_confidence`).
8. `DAEMON.md` — one new paragraph after D3's (currently the last
   sentence of the LSP-enrichment section, ending "...missing
   capability/process and per-site failures remain no-ops. Trigger wiring
   and stale-candidate cleanup remain D6 and D4 work respectively.").
   That trailing sentence is edited in place (not left stale once D4
   exists) to read "Trigger wiring remains D6 work." and a new paragraph
   follows it describing `merge_policy.py`: the write-time guard against
   regressing an `EXTRACTED` fact, and the sibling-`AMBIGUOUS`-retirement
   behavior, in the same one-paragraph-per-module style C2–C6/D1–D3
   already established — not a new top-level section, and no
   `ARCHITECTURE.md` change (no MCP surface, no schema change: `delete()`
   and `get()`/`list_by_site` are pre-existing methods, `confidence_rank`
   is a new function not a new column).

## Workflow constraints carried into the spec

- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the 3 known pre-existing failures (baseline
  reconfirmed this session: 707 passed, 3 failed).
- No new runtime dependencies — everything here is pure Python over
  already-open `sqlite3` connections via `RelationStore`'s existing
  methods.
- Do not touch `runtime.py`/`dispatch.py`/`mcp_server.py`/`bootstrap.py` —
  daemon-trigger wiring is D6's job, not D4's.
- Do not implement D5 (`acie scan` CLI) or D6 (daemon trigger wiring).
- Do not silently drop the D3-review staleness-window follow-up (memory
  `87e7e80b`) — this spec explicitly hands it to D6 (see "What D4 does
  not do").
- Do not broaden sibling retirement beyond `AMBIGUOUS` siblings at the
  literal site — the cross-pass `INFERRED`-vs-`INFERRED` reconciliation
  corner is a named, deliberate non-goal (`# shortcut:` in
  `merge_policy.py`), not silently folded in.
- Standing dogfooding ground rule (wayfinder map `5d8fa498`'s Notes) still
  applies: live `mcp__acie__*` tools stay v0-pinned throughout
  implementation.

## Verification note

This spec was written against the actual current source this session:
`relation_store.py` (full read — schema, `upsert`, `get`, `delete`,
`list_by_site`, `list_by_site_file`, `list_by_target`, `list_by_source`,
`is_tombstoned`, `_content_differs`), `ir/relation.py` (`Relation`,
`DeferredImportCall`, `DeferredImportInherit`, `DeferredImportOverride`),
`ir/symbol.py` (`Confidence`, `Provenance`, `Symbol`), the committed
`daemon/lsp_enrichment.py` in full (all 175 lines — worklist building,
response mapping, the existing `_make_upsert_job`), `indexer.py`'s
`_candidates_for`/`unresolved_deferred_sites`/`_resolve_deferred`/
`_resolve_deferred_overrides` (confirming the "one row per candidate, one
shared confidence per site" pattern that makes sibling rows possible and
an `EXTRACTED`/`AMBIGUOUS` mix at one site currently impossible),
`extract_relations.py` lines 140–180 and ~430–470 (same-file `calls`/
`overrides` confidence-batching, confirming the same pattern
independently for the non-deferred path), `tools/confidence.py` (the
existing private `_RANK` and its own "filter-only, non-ordinal-taxonomy"
framing), `write_queue.py` in full (confirming the single-writer-thread,
strict-FIFO, one-job-at-a-time-per-repo model that makes an in-job
read-then-write race-free), and `tests/daemon/test_lsp_enrichment.py` in
full (confirming `FakeWriteQueue.submit()` never actually executes the
submitted job — the reason design decision 5's integration test uses a
real connection rather than extending the existing fake-based tests).
`DAEMON.md`'s current LSP-enrichment section (lines 126–134) was read to
match its established one-paragraph-per-module convention and to find the
exact trailing sentence decision 8's file-touch edits in place.

No new live LSP/pyright smoke tests were needed for this slice — D4 does
not touch the LSP client/protocol/process layers at all, only the
storage-write policy layered above D3's already-verified LSP-facing code.
