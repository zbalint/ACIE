# Worktree Support — Per-Worktree Index Isolation for Concurrent Git Worktrees

## Status: LOCKED for handoff (grilling session, 2026-09-05 / 2026-09-06)

`ARCHITECTURE.md:217` names this as a deliberately deferred "real product
question, not a keying bug." This spec resolves it, via a multi-round
`grilling`-skill interview (SALTMDB memories `14c34609`, `4dfaf02c`,
`c13e43fd` — full question/answer trail there). Ready for TDD
implementation.

## The problem

- `repo_id.py`'s `resolve_repo_id()` hashes the shared `.git` common dir —
  every worktree of one repo (the main checkout and every `git worktree
  add` off it) collapses to the *same* `repo_id`, and therefore the same
  `~/.acie/repos/<repo_id>/index.sqlite`. `resolve_repo_root()` instead
  resolves to the calling worktree's own on-disk root and stays distinct
  per worktree.
- Storage is fully flat per `repo_id`: one `index.sqlite`, one singleton
  `index_meta` row (`head_sha`, `cross_file_pass_version`,
  `last_enrichment_fingerprint` — see `storage/index_meta_store.py`). No
  worktree/branch dimension anywhere in `SymbolStore`/`RelationStore`/
  `IndexMetaStore`'s schema.
- `BootstrapCoordinator.register()` (`daemon/bootstrap.py`) only ever does
  a real symbol/relation walk **once per `repo_id`, ever** — the first
  worktree to register. Every later `register()` call for an
  already-`repo_ready` `repo_id` — including a second worktree with
  entirely different checked-out content — runs
  `_maybe_schedule_cross_file_migration`/`_maybe_schedule_reconciliation_check`
  instead, which is enrichment catch-up only (pyright/LSP), never a fresh
  symbol/relation walk of the new worktree's actual content.
- Watchers are already `repo_root`-keyed (`WatcherRegistry`, one
  `Observer` per real worktree directory), but every watcher for a given
  `repo_id` still submits its jobs into the one shared write-queue worker,
  which writes into the one shared index. Two worktrees with divergent
  content at overlapping relpaths therefore don't just serve stale data —
  their live watcher/git-hook-triggered reindex jobs (`watcher.py`'s
  `make_reindex_job`, tier 1; `notify_hook.py`'s tier 2) visibly overwrite
  each other's symbol data as edits alternate between them. This is a live
  risk in this repo's own actual usage pattern, not theoretical: every
  worktree this project has ever created (H1–H3, this design spec's own
  worktree, and the concurrently-active
  `h3-alias-aware-stub-context-detection` worktree) is exactly this
  short-lived, high-divergence, overlapping-window case.
- A drift-detection mechanism already half-exists (`repo_fingerprint.py`'s
  `compute_repo_fingerprint()`, hashing `HEAD` + `git diff HEAD` +
  untracked-file stats for one `repo_root`, compared each `register()`
  call against the single `last_enrichment_fingerprint` stored for the
  whole `repo_id`). Today a mismatch only re-triggers enrichment, not a
  reindex — the machinery to detect divergence exists but wasn't built to
  fix it.

## Locked decisions (grilling Q1–Q7 — see `c13e43fd` for the full trail)

1. **Scope**: short-lived/throwaway spec worktrees with high divergence at
   few paths and a limited concurrent window — not long-lived
   permanently-diverged worktrees, for now.
2. **Correctness target**: true per-worktree isolation. Each worktree's
   queries only ever reflect its own content; no cross-contamination even
   under concurrent use. Some duplicated storage for identical files is an
   accepted cost.
3. **Storage shape**: fully separate `index.sqlite` per worktree, keyed by
   a new identity derived from `(repo_id, repo_root)`. No schema migration
   to `SymbolStore`/`RelationStore`/`IndexMetaStore`.
4. **Bootstrap cost**: a newly-registered worktree seeds its index from an
   existing sibling index rather than always doing a full fresh walk, then
   uses `repo_fingerprint.py`'s existing git-diff machinery to compute
   which files actually differ and only re-walks those.
5. **Seed source**: always the repo's primary/main worktree's index if
   it's ready, falling back to a full walk if it isn't. Not an N-way diff
   against every existing worktree — this repo's spec worktrees always
   branch directly off the main checkout, so the main worktree is already
   the closest match in the overwhelming common case.
6. **Cleanup lifecycle**: explicitly out of scope. Orphaned per-worktree
   `index.sqlite` files left behind after `git worktree remove` are not
   garbage-collected by this design. Left open for a possible future
   separate cleanup-pass spec.
7. **Test coverage folded in**: the open `WatcherRegistry.close()`
   multi-watcher-shutdown gap (`2a299228` — never exercised end-to-end
   under multiple real simultaneous worktree watchers, only smoke-tested
   in a `finally` block with no bounded-shutdown assertion) becomes part
   of this spec's acceptance criteria, since per-worktree isolation makes
   multi-watcher-per-repo the normal case, not an edge case.

## Locked mechanism

1. **New identity + primary-worktree detection** (`repo_id.py`):
   - `is_primary_worktree(repo_path: str) -> bool`: `True` iff
     `git rev-parse --git-dir` (realpath'd) equals
     `git rev-parse --git-common-dir` (realpath'd) for `repo_path` — true
     only for the original checkout, never for a linked
     `.git/worktrees/<name>` worktree.
   - `resolve_worktree_id(repo_path: str) -> str | None`: for the primary
     worktree, returns `resolve_repo_id(repo_path)` unchanged (byte-
     identical to today — no migration for the overwhelmingly common
     single-worktree case). For a non-primary worktree, returns
     `f"{repo_id}-{sha256(realpath(repo_root)).hexdigest()[:16]}"`.
   - `resolve_index_db_path` (or wherever the current
     `~/.acie/repos/<repo_id>/index.sqlite` path is built) takes the
     `worktree_id` instead of the raw `repo_id`. Primary worktrees resolve
     to the existing path unchanged; non-primary worktrees resolve to
     `~/.acie/repos/<repo_id>/worktrees/<worktree_id>/index.sqlite` — kept
     nested under the primary's `repo_id` directory (not a flat sibling)
     so the whole repo's on-disk footprint stays discoverable/removable as
     one unit.
2. **Rekey the bootstrap/write-queue layer** (`daemon/runtime.py`,
   `daemon/bootstrap.py`): `BootstrapCoordinator`/`WriteQueue`, currently
   keyed on `repo_id` alone (decision 10, `b599171a`), get rekeyed to
   `worktree_id`. Every call site that currently resolves and passes
   `repo_id` (dispatch's `register()` call, watcher/notify-hook job
   submission) resolves and passes `worktree_id` instead — `repo_root` is
   already available at every one of those call sites, so this is a
   keying-scope change, not new plumbing. `structural_search`'s own file
   read (`dispatch.py:_read_source_files`, keyed on `repo_root` already)
   needs no change — it was already correctly worktree-scoped.
3. **Seeding on first registration for a new worktree_id**
   (`bootstrap.py`'s `register()`/`_run_bootstrap`):
   - If `worktree_id` is not yet `repo_ready` AND this is not the primary
     worktree: look up the primary worktree's `worktree_id` (same
     `repo_id`, primary-detected path) and check whether *it* is
     `repo_ready`.
     - If yes: copy the primary's `index.sqlite` (including its
       `index_meta` row) into the new worktree's index path as the seed.
       Then compute the actual file-level diff between the seed's stored
       `head_sha`/fingerprint and this worktree's current
       `compute_repo_fingerprint()` (extending `repo_fingerprint.py` with
       a function that returns the *list* of changed relpaths, not just a
       hash/boolean mismatch — today's fingerprint only supports
       equality comparison). Enqueue one write-queue reindex job per
       differing file (same one-file-per-job granularity `_run_bootstrap`
       already uses), rather than walking the whole tree.
     - If no (primary isn't indexed yet either): fall back to today's
       unchanged `_run_bootstrap` full-walk path for this worktree, same
       as if it were the only worktree.
   - If this *is* the primary worktree: unchanged — today's full-walk
     `_run_bootstrap` path, byte-identical to current behavior.
4. **Watcher/notify-hook reindex jobs**: no change to `WatcherRegistry`
   itself (already `repo_root`-keyed, one `Observer` per worktree). The
   jobs they submit now naturally land in each worktree's own
   `worktree_id`-keyed write-queue/index instead of a shared one, purely
   as a consequence of step 2 — no separate change needed here.
5. **Multi-watcher shutdown test** (folded-in acceptance criterion, Q3):
   add a real end-to-end test with two or more actual `Observer` instances
   registered under different worktree roots of the same `repo_id`,
   asserting `WatcherRegistry.close()` shuts all of them down within a
   bounded time and with no lost/duplicated events — not just "didn't
   crash" in a `finally` block (current coverage, per `2a299228`).

## Explicitly not changing

- `SymbolStore`/`RelationStore`/`IndexMetaStore` schema — no worktree/
  branch column added anywhere (Q3 decision).
- Cleanup of orphaned per-worktree indexes after `git worktree remove` —
  out of scope (Q6); they accumulate on disk until a possible future
  cleanup-pass spec.
- Long-lived, permanently-diverged worktree usage — this design targets
  the short-lived/spec-worktree pattern (Q1); revisit separately if that
  usage pattern changes.
- Seed-source selection beyond "always the primary worktree" — no N-way
  diff against every existing worktree to find the closest match (Q7
  alternative, rejected as unneeded cost for this repo's actual usage).
- `structural_search`'s file-reading path (`dispatch.py:_read_source_files`)
  — already correctly `repo_root`-scoped; confirmed via SALTMDB memory
  `180b5d7a` (unrelated `DAEMON_UNAVAILABLE` dogfooding finding, root-
  caused and folded into this design discussion, then confirmed
  orthogonal to it).

## Files to touch

- `src/acie/repo_id.py` — `is_primary_worktree()`, `resolve_worktree_id()`,
  updated index-path resolution.
- `src/acie/daemon/bootstrap.py` — `register()`, `_run_bootstrap()`, new
  seed-and-diff path.
- `src/acie/daemon/runtime.py` — `BootstrapCoordinator`/`WriteQueue`
  construction and keying; every call site that resolves `repo_id` for
  these two needs to resolve `worktree_id` instead.
- `src/acie/daemon/dispatch.py` — thread `worktree_id` through wherever
  `register()` is invoked from an incoming request.
- `src/acie/daemon/repo_fingerprint.py` — new function returning the list
  of changed relpaths between two fingerprints, not just an equality
  check.
- `src/acie/daemon/watcher.py` — verify no change needed beyond
  confirming reindex jobs already carry enough context (`repo_root`) to
  resolve the right `worktree_id` downstream; add the multi-watcher
  shutdown test here or in the daemon test suite, whichever exercises
  `WatcherRegistry.close()` today.
- `src/acie/storage/index_meta_store.py` (or wherever the on-disk
  `~/.acie/repos/...` path layout is actually built) — nested
  `worktrees/<worktree_id>/` path for non-primary worktrees.
- Tests: `tests/test_repo_id.py`, `tests/daemon/test_bootstrap.py`,
  `tests/daemon/test_runtime.py`, `tests/daemon/test_repo_fingerprint.py`,
  and the daemon watcher test suite (new multi-watcher-shutdown test).

## Workflow constraints carried into this spec

- Implement with the `tdd` skill, one slice, stop before commit for
  review (memory `9b020543`) — same convention as every other capability
  spec in this repo.
- Sequence independently of any in-flight capability work (e.g. H3's
  alias-aware stub-context detection, currently being implemented by omp
  in worktree `h3-alias-aware-stub-context-detection`) — the two touch
  disjoint areas (IR/symbol-resolution vs. daemon/storage keying) and have
  no ordering dependency, but confirm via `git diff master...h3-alias-aware-stub-context-detection --stat`
  before merging either, since no one has actually diffed the two
  worktrees' file sets against each other yet (SALTMDB memory `d6f00ba3`).

## Verification note

Grounded across two sessions (2026-09-05 grounding: `repo_id.py`,
`bootstrap.py`, `watcher.py`, `runtime.py`,
`storage/index_meta_store.py`, `repo_fingerprint.py`; 2026-09-06:
`repo_id.py`'s `resolve_repo_id`/`resolve_repo_root`/
`resolve_git_common_dir` re-verified against current `HEAD`, and
`bootstrap.py`'s `register()` re-read in full) on `master` at `9578628`.
Design locked via `grilling`-skill interview, full question/recommendation/
answer trail in SALTMDB memories `14c34609` (Round 1), `4dfaf02c` (Rounds
1–2 locked), `c13e43fd` (fully resolved). The unrelated `structural_search`
`DAEMON_UNAVAILABLE` dogfooding finding raised during this discussion was
root-caused separately (`180b5d7a`) and confirmed orthogonal to this spec.
