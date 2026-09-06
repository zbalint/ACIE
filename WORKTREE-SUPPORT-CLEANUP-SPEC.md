# Worktree Support Cleanup — Follow-Up Fixes for 9bb7ee6

## Status: LOCKED for handoff (Claude review, 2026-09-06)

`WORKTREE-SUPPORT-SPEC.md`'s mechanism landed and is committed at `9bb7ee6`
on this same `worktree-support` branch (spec-compliant on all 7 locked
decisions, 836/836 tests passing — see SALTMDB memories `0af7a383`,
`70d2c00e`). This follow-up spec is the non-blocking cleanup pass that
review identified: none of these are correctness bugs the test suite
would catch differently than it already does, but they're worth fixing
before this branch merges to master. Implement on top of `9bb7ee6` in
this same worktree/branch — do not create a new worktree, do not re-touch
any of the locked mechanism this spec doesn't call out.

## Locked decisions

1. **Stale architecture docs.** `ARCHITECTURE.md:16`, `ARCHITECTURE.md:53`,
   and `ARCHITECTURE.md:217` still describe the pre-`9bb7ee6` model — "git
   worktrees of one logical repository share one `<repo-id>`" and the
   `ARCHITECTURE.md:217` paragraph the original `WORKTREE-SUPPORT-SPEC.md`
   was written specifically to resolve (it explicitly quotes this exact
   paragraph as the deferred problem it fixes). `DAEMON.md:73` and
   `DAEMON.md:118` carry the same stale "keyed on `repo_id` alone, worktrees
   share one write queue/bootstrap flag" description. `watcher.py`'s own
   module/class docstrings (lines ~13, 22, 341–349) repeat the identical
   stale claim.
   - **Fix**: rewrite all of the above to describe the actual current
     behavior — a primary worktree keeps today's `repo_id`-keyed identity
     unchanged; a linked worktree gets its own `worktree_id`
     (`repo_id.py:resolve_worktree_id`), its own `index.sqlite` nested at
     `repos/<repo_id>/worktrees/<worktree_id>/`, its own write queue, its
     own bootstrap-readiness flag, and its own `Observer` — full isolation,
     not shared state. Keep whatever context is still accurate (e.g. a
     symlink/realpath'd spelling of the *same* worktree still collapses to
     one `worktree_id`/one `Observer` — that part was never wrong).
   - Do **not** re-derive this from scratch — the actual current behavior
     is already fully described in `WORKTREE-SUPPORT-SPEC.md`'s "Locked
     mechanism" section and in `9bb7ee6`'s commit message; this is a
     documentation sync, not new design work.

2. **`runtime.py`'s `db_path_for` duplicates `repo_id.py`'s ID-format
   knowledge.** `create_daemon`'s `db_path_for(worktree_id)` (in
   `runtime.py`) reimplements the primary-vs-linked path decision by
   string-partitioning `worktree_id` on `"-"` and checking
   `len(repo_id) == 16 and len(worktree_suffix) == 16` — magic numbers that
   duplicate `repo_id.py`'s private `_REPO_ID_HEX_LENGTH = 16` constant
   without importing it. If that constant ever changes, this silently goes
   out of sync with no test catching it.
   - **Fix**: move the worktree_id → directory decision into `repo_id.py`
     as a small shared helper (e.g. a function that takes `worktree_id`
     and `base_dir` and returns the repo directory, reusing
     `_REPO_ID_HEX_LENGTH` directly rather than a re-typed literal), and
     have `runtime.py`'s `db_path_for` call it instead of reimplementing
     the partition/length check inline. `repo_id.py` already owns
     `resolve_index_db_path`, which needs the original `repo_path` (not
     available inside `db_path_for`'s closure, which only ever receives
     the already-resolved ID string) — so this is a new, narrower helper
     alongside it, not a call to `resolve_index_db_path` itself.
   - Keep `db_path_for`'s existing behavior byte-for-byte identical for
     every current caller (`WriteQueue`, `BootstrapCoordinator`,
     `handle_notify_hook`) — this is a pure refactor, not a behavior change.

3. **`bootstrap.py`'s fallback file reader reimplements `dispatch.py`'s
   ignore-matching instead of reusing it.** `BootstrapCoordinator`'s
   default `read_files` (used whenever a caller doesn't inject one — several
   of `test_bootstrap.py`'s existing seed tests rely on this default) is
   its own static method `_read_changed_files`, which filters files with a
   bare `.py`-suffix check plus a naive "any path segment starts with `.`"
   dotdir skip. `dispatch.py`'s `read_repo_files` (added in `9bb7ee6` and
   what production actually wires in via `runtime.py`'s
   `read_files=read_repo_files`) does the equivalent job correctly, reusing
   the shared `ignore.get_ignore_matcher` that `walk_repo` itself already
   uses — so the two implementations can silently diverge on what counts as
   ignored.
   - **Fix**: delete `BootstrapCoordinator._read_changed_files` and default
     `read_files` (when the constructor arg is `None`) to
     `dispatch.read_repo_files` instead. Confirm there's no import cycle
     (there isn't — `dispatch.py` does not import `bootstrap.py`). Existing
     tests that rely on the default (e.g.
     `test_linked_worktree_tombstones_a_seeded_file_deleted_in_the_worktree`,
     `test_seed_fallback_does_not_leave_primary_only_deleted_symbols`,
     `test_linked_worktree_tombstones_a_seeded_untracked_file_missing_from_worktree`,
     `test_linked_worktree_does_not_seed_a_dirty_primary_index_into_clean_worktree`,
     `test_linked_worktree_falls_back_when_primary_head_metadata_is_missing`)
     must still pass unchanged against the new default — they use real git
     repos under `tmp_path` with no ignore rules, so `read_repo_files`'s
     stricter/more-correct filtering should be a no-op difference for them.

4. **Dead tombstone loop in `make_index_job`.** The `if source_text is
   None: for symbol in symbol_store.list_by_path(path):
   symbol_store.delete(...)` block never executes: `index_file(path,
   source_text="", ...)` already computes `prior_symbol_ids -
   {new symbol ids}` and deletes every removed symbol itself (`new_symbols`
   is `[]` for empty source, so every prior symbol for that path is already
   gone by the time this loop's own `list_by_path(path)` call runs — it
   always iterates zero times). It also never touched relations either way,
   so it was never a complete manual tombstone mechanism to begin with.
   - **Fix**: delete the dead `if source_text is None: ...` block from
     `make_index_job`'s `job()` closure. `index_file`'s own existing
     diff-based removal already handles the tombstone case correctly and
     completely (symbols *and* relations) whenever it's called with an
     empty `source_text` — no replacement logic is needed.
   - Existing tests already cover the tombstone behavior end-to-end
     (`test_linked_worktree_tombstones_a_seeded_file_deleted_in_the_worktree`,
     `test_linked_worktree_tombstones_a_seeded_untracked_file_missing_from_worktree`)
     — they must keep passing unchanged; no new test is required for this
     item specifically.

5. **Bare `sqlite3.connect()` bypasses this codebase's `open_connection()`
   safety seam.** `BootstrapCoordinator._seed_worktree` opens
   `sqlite3.connect(source_db)` directly to read the primary's
   `index_meta`/`symbols_live` rows, and `_copy_index` opens
   `sqlite3.connect(source_db)` / `sqlite3.connect(target_db)` directly for
   the backup. `storage/connection.py:open_connection()` exists
   specifically so every raw connection in this codebase gets the same
   WAL/`synchronous=NORMAL` pragma setup and retry-safe WAL transition —
   built after a real production incident (bug-2, SALTMDB `c90f7a6e`)
   where a connection that skipped this setup caused reader/writer lock
   contention.
   - **Fix**: replace all three bare `sqlite3.connect(...)` calls in
     `_seed_worktree`/`_copy_index` with `open_connection(...)` (already
     importable from `acie.storage.connection`, same call signature —
     `open_connection(db_path: str) -> sqlite3.Connection`). No behavior
     change expected; this is a consistency/defense-in-depth fix.

6. **Style: missing blank-line separation between top-level defs.**
   `repo_id.py` (`is_primary_worktree` glued directly under
   `resolve_repo_id` with no blank line), `bootstrap.py` (`_seed_worktree`
   after `_run_bootstrap`, `_persist_head_sha` after `_run_indexing_pass`,
   both missing the blank-line separation the rest of the file uses), and
   several new test functions in `tests/daemon/test_bootstrap.py` (each new
   `def test_...` glued directly onto the previous test's closing line).
   `tests/daemon/test_bootstrap.py` is also missing its final trailing
   newline.
   - **Fix**: add the missing blank lines (two blank lines between
     top-level defs, matching the rest of each file's existing style) and
     the missing trailing newline. Purely cosmetic — no lint gate is
     configured in this repo to catch it, but it should match the
     surrounding code.

7. **Multi-watcher shutdown test doesn't directly prove "no duplicated
   events."** `test_watcher_registry_closes_multiple_real_worktree_watchers_and_indexes_each_once`
   (added in `9bb7ee6`, satisfying `WORKTREE-SUPPORT-SPEC.md` decision 7)
   asserts final symbol state per worktree and bounded `close()`, but
   doesn't directly instrument submission/reindex counts — a duplicate
   reindex of the same file would likely still pass today only because
   `SymbolStore.upsert` is idempotent by symbol id, not because the test
   proves no extra work happened.
   - **Fix (lower priority than 1–6)**: extend the existing test with a
     non-vacuous assertion that each worktree's file was submitted/indexed
     exactly once (e.g. count `WriteQueue` job submissions per
     `worktree_id`, or an `on_indexed`/generation-counter assertion — pick
     whichever existing seam is closest to a real submission count without
     adding new production instrumentation). If no clean seam exists
     without adding new production code, leave this one as an explicitly
     documented known test-coverage gap in the test's own docstring/comment
     rather than skipping it silently.

## Explicitly not changing

- None of `WORKTREE-SUPPORT-SPEC.md`'s locked mechanism (identity
  derivation, seed-and-diff bootstrap flow, storage layout, watcher
  keying) — this spec is cleanup only, not a design revision. If any fix
  above turns out to require touching that mechanism's actual behavior,
  stop and flag it rather than expanding scope.
- No schema migration to `SymbolStore`/`RelationStore`/`IndexMetaStore`.
- No new test coverage beyond item 7 — items 1–6 are refactors/doc fixes
  that existing tests must continue to prove correct, not new capability
  needing new tests.

## Files to touch

- `ARCHITECTURE.md`, `DAEMON.md` — decision 1.
- `src/acie/daemon/watcher.py` — decision 1 (docstrings only, no code
  change).
- `src/acie/repo_id.py` — decision 2 (new shared path helper), decision 6
  (blank line).
- `src/acie/daemon/runtime.py` — decision 2 (`db_path_for` calls the new
  helper).
- `src/acie/daemon/bootstrap.py` — decision 3 (`_read_changed_files`
  removed, default `read_files` becomes `dispatch.read_repo_files`),
  decision 4 (dead tombstone loop removed), decision 5 (`open_connection`
  swap), decision 6 (blank lines).
- `tests/daemon/test_bootstrap.py` — decision 6 (blank lines, trailing
  newline); no behavior/assertion changes required by decisions 3–4 (they
  must keep passing as-is).
- `tests/daemon/test_watcher.py` — decision 7 (if a clean seam exists).

## Workflow constraints

- Implement in this same worktree/branch (`worktree-support`), on top of
  commit `9bb7ee6` — do not create a new worktree for this.
- Run the full test suite (`.venv/bin/python -m pytest -q`) as the final
  step; expect 836 passed. Note:
  `tests/test_cli.py::test_daemon_stop_actually_terminates_the_os_process`
  is a known pre-existing daemon-lifecycle flake unrelated to this spec's
  files (reproduces in isolation as a pass, root-caused to
  `DaemonServer.shutdown()` not joining its accept thread) — if it fails
  once, rerun the full suite once before treating it as a real regression;
  do not attempt to fix it as part of this spec (it touches `server.py`,
  outside this spec's file list).
- Stop before commit — leave the diff uncommitted/unstaged in this
  worktree for Claude to review and commit, same convention as the parent
  spec.

## Verification note

Every item in this spec traces to a specific, already-diagnosed finding
from two independent Claude reviews of `9bb7ee6`'s diff — SALTMDB memories
`0af7a383` (first review, items 1–6 as findings 1–6) and `70d2c00e` (second
review, confirming 1–6 still present post-commit and adding item 7 from
cross-referencing omp's own advisor-loop transcript). No new grounding or
design work is required; this is a directly actionable cleanup list.
