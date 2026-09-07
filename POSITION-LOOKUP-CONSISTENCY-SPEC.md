# Position-Mode Lookup Racing Live Reindex — Reproduce, Then Fix at the Root Cause

Status: spec-and-plan only, written for external implementation (same role
split as the D/H-series and MCP-*-SPEC docs — this document was **not**
implemented in the session that wrote it; a fresh session/agent should
implement it, and another fresh session should review the resulting diff
against this spec before commit).

Triggered by the 2026-09-07 re-verification sweep (SALTMDB memory
`2944aa77`), which re-confirmed memory `3d93c426`'s finding: `get_definition`/
`find_references` in `position` mode raise `SYMBOL_NOT_FOUND` even when
pointed at `store_memory`'s own `def` line — a trivial "click on the
definition itself" case, with byte-exact-verified coordinates, using
`symbol_id` mode's own known-good target as ground truth. Both sweeps that
observed this had live background reindexing running concurrently
(`index_generation` incrementing mid-session in both cases).

This is **not new scope**: `MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md`
already investigated this exact claim, found no logic defect in
`resolve.py`'s exact-match SQL, and — rather than guessing a fix —
correctly declined to touch it, hypothesizing "a transient
delete-then-reinsert of a symbol's row mid-reindex... landing between when
the report's coordinates were captured and when the position-mode call
executed" and adding a stable-index round-trip regression test as the
acceptance bar for a future spec if the claim recurred with new evidence.
It has now recurred, in an independent session, under the same
condition (live reindexing active) that the original investigation flagged
as the leading explanation. This spec is that follow-up: it confirms the
concrete mechanism that makes such a race possible, and requires a
reproducing test before any fix lands — not a second round of
speculation.

Companion spec `STRUCTURAL-SEARCH-RELIABILITY-SPEC.md` fixes the sweep's
other still-open finding (`structural_search`'s `DAEMON_UNAVAILABLE`). The
two specs touch entirely non-overlapping files (this one: `indexer.py`,
`storage/symbol_store.py`, `storage/relation_store.py`, `tools/resolve.py`;
the other: `daemon/client.py`, `daemon/dispatch.py`, `mcp_server.py`) and
can be implemented and reviewed independently, in parallel, in either
order.

## Mechanism confirmed this session (read directly from source, not
re-guessed)

Two design facts combine to make the reported symptom structurally
possible — this section proves *how* it could happen, not that it has
been reproduced under test yet (that is Phase 1 of this spec, below).

**1. `resolve_symbol_or_position` has no staleness/consistency guard, unlike
every other multi-step read path in this codebase.**

`src/acie/tools/resolve.py`'s exact-match queries
(`symbol_store.at_start`, `relation_store.list_by_site`) take no
`index_generation` input and cannot detect "the index changed mid-lookup."
Contrast this with cursor-based pagination elsewhere in the same report,
which pins to a generation up front and fails closed with
`STALE_INDEX_GENERATION` rather than silently returning an empty or
inconsistent result if the index moves underneath it. A single
`position`-mode call has no equivalent: it just reads live store state at
call time and treats an empty result as permanent absence.

**2. Every write to `symbols_live`/`relations_live` auto-commits per row,
and every read opens a fresh, independent connection.**

`SymbolStore.upsert` (`src/acie/storage/symbol_store.py:58-122`) and
`SymbolStore.delete` (`:135-146`) each end with their own
`self._conn.commit()`. `src/acie/indexer.py::index_file` (the function that
actually applies one file's reindex) calls these in two **separate
sequential loops**, each iteration its own commit:

```python
for symbol in new_symbols:
    symbol_store.upsert(symbol)          # commit per symbol
for relation in new_relations:
    relation_store.upsert(relation)      # commit per relation
...
removed_symbol_ids = prior_symbol_ids - {s.id for s in new_symbols}
for symbol_id in removed_symbol_ids:
    symbol_store.delete(symbol_id, observed_at=observed_at)   # commit per symbol
```

Meanwhile, `dispatch.py`'s own module docstring states the daemon's
read-only tools use "fresh-per-call store construction" — every
`get_definition`/`find_references` request opens a brand-new `SymbolStore`/
`RelationStore` connection against the same `index.sqlite` file at request
time. SQLite's default per-connection read consistency means a fresh
reader connection sees whatever the writer connection has committed *at
that instant* — not a single consistent snapshot of "the whole file's
reindex," because the whole file's reindex is not one transaction, it's N
independent ones.

**What this does and does not prove.** `SymbolStore.upsert` itself uses a
single atomic `INSERT ... ON CONFLICT(id) DO UPDATE` statement, so an
unchanged symbol keeping the same id is never actually absent between two
commits — its own upsert is atomic. The concrete window this mechanism
opens is narrower and needs to be pinned down precisely, not assumed:
candidates include (a) a symbol whose derived id changes between reindex
passes (content changed enough to shift its id) landing at the same
`(path, start_line, start_col)` an old, now-removed symbol occupied — the
old row's `delete()` and the new row's `upsert()` are two separate commits,
not one; (b) a `calls`/`references`/`inherits` relation row (used by
`list_by_site` for reference-site position lookups) being deleted and
re-derived across two separate relation-store commits during the same
file's reindex, since relations are diffed and applied in their own
separate loop from symbols; (c) a broader bootstrap-level rebuild (not just
a single file's incremental reindex) that this session did not trace end
to end — `src/acie/daemon/bootstrap.py:432` calls `symbol_store.delete`
too, and its surrounding sequencing was not read this session. **Phase 1
below exists precisely to turn "structurally possible, three plausible
candidate windows" into "confirmed, here is the exact one" before any
production code changes.**

## Locked decisions

1. **Phase 1 is mandatory and comes first: write a reproducing test before
   touching any production code.** Per this workspace's TDD/
   systematic-debugging norms, do not implement a fix for a race whose
   exact window hasn't been pinned down by a failing test — that risks
   fixing a mechanism that isn't the real one and leaving the actual bug
   in place. The reproducing test must:
   - Use two real `sqlite3` connections against the same on-disk `.sqlite`
     file (not the same connection, and not an in-memory `:memory:` DB —
     `:memory:` connections don't share state across connection objects the
     way this bug requires; the real bug involves two independent
     connections against the same file, matching dispatch.py's
     "fresh-per-call" pattern exactly).
   - Drive one connection through a realistic sequence: index a file once
     (baseline symbols/relations committed), then start a second
     reindex pass of the same file with a change that plausibly shifts a
     symbol's id (or a relation's derivation) at the exact same
     `(path, line, col)` an existing symbol/relation occupies — pause
     between the delete-commit and the corresponding upsert-commit (e.g.
     by calling the store methods directly in the test rather than going
     through `index_file` in one call, to control the exact interleaving).
   - From the second, independent connection, call
     `resolve_symbol_or_position`'s underlying queries
     (`symbol_store.at_start` / `relation_store.list_by_site`) in that
     paused window and assert whether they transiently return `None`/
     empty for a position a moment ago (and a moment later) resolves
     correctly.
   - If this reproduces: that pinpoints the exact window (symbol-id churn
     vs. relation churn vs. something else) — implement the narrowest
     correct fix for that confirmed window (see decision 2's fix menu,
     choose based on what Phase 1 actually shows).
   - If this does **not** reproduce after genuinely trying the candidate
     windows named above: do not fabricate a fix for an unconfirmed
     mechanism. Instead, extend the existing stable-index round-trip test
     (already added per `MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md`) to run
     under a concurrent reindex thread (real `threading.Thread`, not
     simulated interleaving) hammering `index_file` on the same file in a
     loop while the main thread repeatedly resolves by position — if that
     *still* doesn't reproduce after a reasonable number of iterations
     (e.g. a few thousand), report back with the negative result and do
     not merge a speculative fix; flag this back as a new, distinct
     finding (the two live sweeps' observations may have had a different,
     as-yet-unidentified cause, e.g. something specific to the daemon's
     real threaded accept loop that a single-process test can't exercise) —
     this is more valuable than a fix for a bug that isn't actually there.

2. **Fix menu, to be selected from based on what Phase 1's reproduction
   actually shows — do not implement more than the confirmed window
   requires:**
   - **(a) Batch a file's reindex into one transaction.** If Phase 1
     confirms the symbol-id-churn or relation-churn window: change
     `index_file` to defer commits until the end of the function (e.g. by
     having `symbol_store`/`relation_store` accept an
     `autocommit: bool = True` constructor flag, or by exposing an explicit
     `commit()` method callers can invoke once at the end while individual
     `upsert`/`delete` calls skip their own commit when a flag is set) so a
     fresh reader connection either sees the whole file's reindex applied,
     or none of it — never a partial mid-file state. This is the strongest
     fix (removes the race entirely for the single-file-reindex case) but
     is a real behavior change to `SymbolStore`/`RelationStore`'s
     transaction semantics, so it must not weaken any other caller's
     existing atomicity expectations — audit every other `upsert`/`delete`
     caller (`watcher.py`, `merge_policy.py`, `bootstrap.py`) before
     changing the default, and prefer an explicit opt-in parameter over
     silently changing the existing default for every caller.
   - **(b) Defense-in-depth at the read side, if (a) alone doesn't fully
     close the window (e.g. the confirmed window spans multiple files or a
     bootstrap-level rebuild decision 2(a) doesn't cover):** give
     `resolve_symbol_or_position` an `index_meta_store` parameter (already
     available to `get_definition`/`find_references` via `dispatch.py`'s
     existing injection, per `"index_meta_store" in sig_params`), read
     `current_generation()` before and after the exact-match queries, and
     if the generation changed during the lookup, retry the lookup once
     (not indefinitely) before raising `SymbolNotFoundError` — mirroring
     the "fail closed on a detected generation change" precedent cursor
     pagination already established, but as a bounded retry rather than a
     hard error, since a `position` lookup (unlike a cursor) has no
     client-visible cursor state to invalidate.
   - Do not implement (b) as a substitute for (a) if Phase 1 confirms a
     single-file in-process window — a bounded retry masks the race
     rather than closing it, and is only justified as a genuine
     defense-in-depth layer once (a) has removed the primary window.

3. **Scope boundary:** this spec's Phase 1 investigation is limited to
   `indexer.py`'s per-file `index_file` path and the two store classes'
   commit granularity. If Phase 1's negative-result branch triggers (no
   reproduction even under a real concurrent-thread stress test), do not
   expand into tracing `bootstrap.py`'s full rebuild sequencing or the
   daemon's real socket/threading layer as part of this spec — report the
   negative result and stop; that would be new investigative scope
   deserving its own spec, not a silent expansion of this one.

## Test plan (write first, red before green)

New tests in `tests/test_indexer.py` or a new
`tests/storage/test_symbol_store_concurrency.py` (choose based on which
existing file's fixture conventions are the closer match once you're
reading them — this spec doesn't lock the exact file, only that a real
two-connection, real-file-backed reproduction is required, not an
in-memory or single-connection simulation):

- Phase 1's reproducing test(s), per decision 1 — for whichever of the
  three candidate windows actually reproduces.
- If (a) is implemented: a test that a partially-applied reindex (killed
  or interrupted mid-way, if that's simulable, or simply asserted via
  "only one commit occurs for N upserts+deletes" via a commit-count spy on
  the connection) is all-or-nothing from a concurrent reader's perspective.
- If (b) is implemented: a test that a lookup retried once after a
  generation change succeeds when the retried read would find the symbol,
  and a test that it still raises `SymbolNotFoundError` (not an infinite
  retry loop) when the symbol genuinely never existed.
- Regression: the existing stable-index round-trip test added per
  `MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md`
  (`tests/tools/test_get_definition.py`, and the pre-existing
  `tests/tools/test_find_references.py:194` equivalent) must keep passing
  unchanged.

All new/changed tests must go red before the fix (Phase 1's test is
expected to go red first without any fix — that's the reproduction; then
green after whichever Phase 2 fix decision 1's result selects). If Phase 1
resolves to the negative-result branch, there is no red-to-green cycle for
a fix — report that outcome instead of forcing one.

## Files to touch

Exact file list depends on Phase 1's outcome; the following are the files
already identified this session as in-scope for either branch of decision
2 — do not touch any file outside this list without first confirming via
Phase 1 that it's part of the confirmed window:

- `src/acie/storage/symbol_store.py`, `src/acie/storage/relation_store.py`
  — commit-granularity change, if decision 2(a) is selected.
- `src/acie/indexer.py` — call the new explicit-commit path once at the end
  of `index_file`, if decision 2(a) is selected.
- `src/acie/tools/resolve.py` — bounded generation-check retry, if decision
  2(b) is selected (in addition to, not instead of, 2(a) if both are
  needed).
- `src/acie/daemon/dispatch.py` — only if `resolve_symbol_or_position`'s
  signature changes to accept `index_meta_store` and dispatch's existing
  injection logic needs a one-line addition (it already injects
  `index_meta_store` for tools whose signature declares it, per
  `"index_meta_store" in sig_params` — likely no change needed here at
  all, confirm while implementing).
- New/extended test file(s) per the Test Plan above.
- No change to `src/acie/tools/get_definition.py` or `find_references.py`
  themselves — both call `resolve_symbol_or_position` without touching
  `position` directly, same as the precedent already established in
  `MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md`.

## Verification note

Run `.venv/bin/python -m pytest -q` (the project's actual venv — plain
`python3 -m pytest` fails to collect, `acie` isn't installed on system
Python) before and after. Baseline confirmed freshly at spec-write time
(2026-09-07, on current master, commit `2c0b553`): **849 passed, 0
failed** — clean. If a prior spec's recorded 2 pre-existing failures
(`tests/daemon/test_runtime.py::test_runtime_isolates_divergent_worktree_indexes_and_storage`,
`tests/test_cli.py::test_daemon_stop_actually_terminates_the_os_process`)
reappear during this spec's own verification runs, treat them as
possibly flaky/environment-sensitive and unrelated to this spec's
storage/indexer changes rather than something this spec caused or must
fix — but do confirm the suite has no *other new* failures beyond that
pair.
