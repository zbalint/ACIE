# Capability E, Slice E1 — Recurring Re-Enrichment Trigger Wiring

Status: spec-and-plan only, written for external implementation (same role
split as C1–D6 — this document was **not** implemented in the session that
wrote it; a fresh session should implement it, and another fresh session
should review the resulting diff against this spec before commit).

Written directly in a `grilling` session (not a CADET/omp run — CADET's
`delegate_task` is currently broken in this environment, SALTMDB core memory
`71b2f460`; spec-writing has always been done directly by `claude` for every
C/D slice regardless, per `ac233a50`/`f06fd2a0`, so this is not a deviation).

## Why this is a new capability, not D7

Capability D (D1–D6) is closed and signed off (`f06fd2a0`, committed
`0fd9298`). Its own sign-off explicitly re-raised one unresolved question
before treating D as *fully* closed: whether the one-shot bootstrap/
migration trigger D6 built is the final word, or whether a recurring trigger
is still owed (`ac233a50`). The user's decision (this grill session,
2026-09-05): **new Capability E** (`E1`, `E2`, …), not `D7` — re-opening a
breakdown that was deliberately locked and signed off as complete would
undermine the "locked means locked" convention C1–D6 established. E1
resolves `ac233a50` directly.

This also elaborates the wayfinder v1 map (`5d8fa498`, status "DESTINATION
REACHED"): that map's own text names exactly this situation — "the next
step is implementation, not further planning, **unless new fog surfaces
during that work**" — and the D6 sign-off's caveat is precisely that fog,
surfacing after the map's destination was already reached, not before.

## Scope split (locked this session)

- **E1 (this spec)**: trigger wiring only. Reuses `run_enrichment_pass()`
  exactly as D6 already calls it — full-repo walk, unchanged, no new
  resolution/staleness plumbing.
- **E2 (named, not designed)**: per-site staleness tracking — a staleness
  bit on persisted enrichment facts, an invalidation hook off watcher edits,
  lazy re-resolution on read, plus whatever MCP-visible staleness signal
  that implies. Deliberately deferred to its own future design pass; E1's
  correctness does not depend on it.

## What this session verified live against actual current source

Read in full, post-D6-commit (`0fd9298`): `bootstrap.py`, `runtime.py`,
`watcher.py`, `pyright_process.py`, `index_meta_store.py`, `repo_id.py`.

### 1. ACIE already requires every indexed repo to be a git repository, everywhere, today — this is not a new constraint E1 introduces

`repo_id.py`'s `resolve_git_common_dir`/`resolve_repo_root` (lines 12–45)
both shell `git -C <repo_path> rev-parse ...` and return `None` on any
non-zero exit; `resolve_repo_id`/`resolve_repo_state_dir`/
`resolve_index_db_path` (lines 48–84) all propagate that `None`.
`runtime.py`'s `_resolve_repo` (lines 186–205) returns `None` on either
failing, which short-circuits `register_repo`/`ensure_fresh` in `dispatch()`
(lines 215–229) — a non-git `repo_path` is simply never registered,
watched, or bootstrapped. `scan.py` raises `ScanError` for the same case.
**Decision (user, this session): document this explicitly as a permanent,
intentional product constraint in `ARCHITECTURE.md`**, not merely an
implementation detail a reader has to infer from `repo_id.py` — ACIE's
audience (AI agents editing code) can be assumed to always be working
inside a git repository. `ARCHITECTURE.md` lines 16/53 currently only say
`<repo-id>` is "derived from" `.git` — they never assert that a non-git
directory is unsupported *by design*, permanently, rather than as an
unlocked v0 limitation. This is a doc-only change (see "Files to touch").

### 2. `IndexMetaStore` already has the exact lazy-migration idiom E1's new fingerprint field needs, and already stores a related-but-distinct git value

`index_meta_store.py`'s `head_sha` column (lines 9, 26–41, 81–91,
127–133) and `cross_file_pass_version` column (lines 10, 43–67, 93–114,
135–146) are each added via their own `_migrate_add_..._column_if_missing`
method (idempotent `PRAGMA table_info` check, `ALTER TABLE ... ADD COLUMN`,
commit), called from `__init__` (lines 74–75) — this is the established,
reused pattern for adding one new persisted field without breaking any
pre-existing `index.sqlite`. `head_sha` is a different concern (tier-2
git-hook reindex diffing) already in this table — E1 adds a **new**,
separate column, not a repurposing of `head_sha`.

### 3. `BootstrapCoordinator.register()` already has the exact shape E1's third one-shot trigger point needs, with zero new coupling to pyright/enrichment

`register()` (lines 79–113) branches on `repo_ready()`: an already-ready
repo takes the `_maybe_schedule_cross_file_migration(...)` branch (line
107) instead of a plain no-op — this is a **second** independent one-shot
completion concern layered on the same branch, guarded by its own
per-process set (`_migration_checked`, lines 54, 194–197) distinct from
`_in_progress`/`_ready`. E1's startup-reconciliation check is structurally
the same shape: a **third** independent one-shot-per-process-per-repo
concern, added to the same branch, with its own guard set — see design
decision 2 below. `bootstrap.py` still needs **zero new imports** for this;
it already calls `self._on_indexed(repo_id, repo_root)` from two places
(lines 163, 243) and E1's reconciliation path reuses that exact same call,
not a new coupling.

### 4. Tier 1's watcher has no hook today for anything besides submitting a per-file reindex job — E1's live trigger needs one, mirroring `on_indexed`'s existing shape exactly

`RepoWatcher._on_paths_changed` (`watcher.py` lines 269–271) unconditionally
just does `self._write_queue.submit(self._repo_id, make_reindex_job(...))`
for each debounced path — there is no injectable callback for "a debounced
batch of paths changed for repo X" anywhere in `RepoWatcher`/
`WatcherRegistry`. `_DebouncedEventHandler`'s own debounce
(`_DEBOUNCE_SECONDS = 0.5`, line 57) is tier 1's, unrelated to and
untouched by E1 — E1's live-trigger debounce (design decision 3) is a
second, independent, much longer debounce layered on top of tier 1's own
already-coalesced signal, not a replacement for it.

### 5. `PyrightProcessRegistry` needs zero changes

`ensure_process` (`pyright_process.py` lines 95–136) already keys one live
child per `repo_root`, reused across calls, with the existing 900s
(`idle_timeout_seconds`, line 82) idle-teardown timer reset on every
`touch()` (lines 138–147). Any recurring cadence tighter than 15 minutes
keeps the subprocess warm for free; a sparser cadence just respawns lazily,
already-supported behavior. No new resource-ceiling design needed.

## A cross-source race Q3's coalescing design must actually close (surfaced writing this spec, not caught during the grill itself)

The grill locked "coalesce to one pending follow-up, never drop or run
concurrently" (Q3) as a *policy*, but if D6's existing `on_indexed` ->
`trigger_enrichment()` path (used by the from-scratch-bootstrap,
migration-catchup, **and** E1's new startup-reconciliation completion
points) and E1's new live-watcher-trigger path each independently decide
"is a pass already running for this repo_id" using **separate** state, the
policy doesn't actually hold across sources — a reconciliation-triggered
pass and a live-edit-triggered pass could still race concurrently against
the same `PyrightProcessRegistry` entry, exactly the failure Q3 exists to
prevent. **Resolution: one `EnrichmentScheduler` instance per daemon,
owning the per-repo `running`/`pending_rerun` state exclusively, and every
trigger source (bootstrap, migration, reconciliation, live watcher edits)
routes through it** — see design decision 4. This is a direct consequence
of Q3 as already agreed, not a new scope decision; flagging it explicitly
here rather than letting it become a silent gap in the diff (per this
project's own standing lesson, `ac233a50`).

**Open item this spec does not close, named explicitly for the implementing
session rather than silently assumed safe:** `bootstrap.py`'s existing
`on_bootstrap_done` already documents a narrow, accepted race (its own
`# shortcut:` comment) where a concurrent `register()` call can observe
`repo_ready()==True` before `_mark_cross_file_migration_done`'s fire-and-
forget write actually persists, double-firing `on_indexed` for one repo.
E1 adds a **third** thread that can independently reach `on_indexed`/
`trigger_now` for the same repo (`_run_reconciliation_check`) alongside the
two D6 already had. `EnrichmentScheduler`'s coalescing (decision 4) should
absorb this exactly as it absorbs any other duplicate trigger — but this
spec has not written down an explicit argument (or test) confirming a
bootstrap-double-fire race and a reconciliation-check firing at
near-the-same-instant for the same repo_id can never together produce
anything worse than today's accepted "harmless duplicate pass" outcome.
**The implementing session must either add reasoning + a regression test
confirming this, or treat it as a real gap to close** — do not silently
assume `EnrichmentScheduler`'s general coalescing makes this specific
three-way interaction safe without checking.

## Design decisions

1. **New module `src/acie/daemon/repo_fingerprint.py`** — one function,
   no new third-party dependency (matches D6's "no new runtime
   dependencies" convention), reusing `repo_id.py`'s existing
   `subprocess.run(["git", "-C", repo_root, ...], capture_output=True,
   text=True, check=False)` shape exactly:

   ```python
   def compute_repo_fingerprint(repo_root: str) -> str | None:
       """Cheap, git-native fingerprint of repo_root's current working-tree
       state (E1 design decision 1). Content-based (git diff HEAD), not
       status-based (git status --porcelain alone would miss a file that
       was already dirty and got edited again while the daemon was
       offline -- porcelain only reports state transitions, not content
       deltas). Untracked files aren't covered by `git diff`, so they're
       folded in separately via (path, mtime, size) -- cheap, no content
       read, matching decision 1's "no full-tree walk" goal. None on any
       git subprocess failure -- callers must treat None as "assume dirty,
       run the pass", never as a match, per D1/D3's opportunistic
       never-load-bearing principle.
       """
       head = _run_git(repo_root, "rev-parse", "HEAD")
       if head is None:
           return None
       diff = _run_git(repo_root, "diff", "HEAD")
       if diff is None:
           return None
       status = _run_git(repo_root, "status", "--porcelain", "--untracked-files=all")
       if status is None:
           return None

       untracked: list[tuple[str, int, int]] = []
       for line in status.splitlines():
           if not line.startswith("??"):
               continue
           rel_path = line[3:]
           try:
               st = os.stat(os.path.join(repo_root, rel_path))
           except OSError:
               continue
           untracked.append((rel_path, st.st_mtime_ns, st.st_size))
       untracked.sort()

       hasher = hashlib.sha256()
       hasher.update(head.strip().encode("utf-8"))
       hasher.update(diff.encode("utf-8"))
       for rel_path, mtime_ns, size in untracked:
           hasher.update(f"{rel_path}\0{mtime_ns}\0{size}\0".encode("utf-8"))
       return hasher.hexdigest()


   def _run_git(repo_root: str, *args: str) -> str | None:
       result = subprocess.run(["git", "-C", repo_root, *args], capture_output=True, text=True, check=False)
       return result.stdout if result.returncode == 0 else None
   ```

   Named `# shortcut:` at this call site: a tracked file outside
   `walk_repo`'s own `.py`-extension filter (`dispatch.py` line 178)
   changing would still flip the fingerprint and trigger an unneeded pass —
   accepted, safe-direction (wasted work, not a missed change), since
   `git diff HEAD` has no visibility into `walk_repo`'s independent
   extension filter. Upgrade trigger: only worth narrowing if this is
   observed to cause noticeably frequent unnecessary passes in practice.

2. **`IndexMetaStore` gains one new column, `last_enrichment_fingerprint
   TEXT`, following the exact `head_sha` migration idiom** (`index_meta_store.py`
   lines 81–91): a new `_migrate_add_last_enrichment_fingerprint_column_if_missing`
   method, called from `__init__` alongside the two existing migration
   calls; `get_last_enrichment_fingerprint()`/`set_last_enrichment_fingerprint(fp:
   str)` mirroring `get_last_indexed_head_sha`/`set_last_indexed_head_sha`
   exactly (lines 127–133). `NULL` means "never recorded" — same lazy-
   migration semantics as `head_sha`, so a pre-E1 `index.sqlite` correctly
   reports "no stored fingerprint" (treated as a mismatch — see decision 3
   — so an upgraded installation's very next startup for an already-indexed
   repo runs one full reconciliation pass, then has a fingerprint from then
   on).

3. **`BootstrapCoordinator` gains a third one-shot-per-process guard,
   `_reconciliation_checked: set[str]`, wired into `register()`'s existing
   "already ready" branch alongside `_maybe_schedule_cross_file_migration`:**

   ```python
   def register(self, repo_id: str, repo_root: str) -> None:
       if self.repo_ready(repo_id):
           self._maybe_schedule_cross_file_migration(repo_id, repo_root)
           self._maybe_schedule_reconciliation_check(repo_id, repo_root)
           return
       ...  # unchanged

   def _maybe_schedule_reconciliation_check(self, repo_id: str, repo_root: str) -> None:
       with self._lock:
           if repo_id in self._reconciliation_checked:
               return
           self._reconciliation_checked.add(repo_id)
       threading.Thread(
           target=self._run_reconciliation_check, args=(repo_id, repo_root), daemon=True
       ).start()

   def _run_reconciliation_check(self, repo_id: str, repo_root: str) -> None:
       def read_stored(conn) -> str | None:
           return IndexMetaStore(conn=conn).get_last_enrichment_fingerprint()
       try:
           stored = self._write_queue.submit(repo_id, read_stored).result()
       except BaseException:
           with self._lock:
               self._reconciliation_checked.discard(repo_id)
           return
       current = compute_repo_fingerprint(repo_root)
       if current is not None and current == stored:
           return  # nothing changed while the daemon was offline -- skip
       self._on_indexed(repo_id, repo_root)
   ```

   This one-shot-per-process guard is exactly what makes this "startup"
   reconciliation without any explicit daemon-lifecycle-boundary concept:
   a repo's first `register()` call in a freshly-started daemon process is
   definitionally "the first time this process has looked at this repo
   since starting" — the same reasoning D6's own finding 3 already used to
   derive its one-shot bootstrap-trigger scope from this identical idiom.
   `bootstrap.py` still imports nothing pyright/enrichment-related — the
   only new coupling is one extra call to the exact same `self._on_indexed`
   hook D6 already built (a `compute_repo_fingerprint` import for the
   comparison, no LSP/pyright import).

4. **New module `src/acie/daemon/enrichment_scheduler.py`, `EnrichmentScheduler`,
   the single owner of per-repo "is a pass running / is one owed" state
   across every trigger source** (closes the cross-source race above):

   ```python
   class EnrichmentScheduler:
       """Coalesces every enrichment trigger source (D6's one-shot
       bootstrap/migration/E1's startup-reconciliation completions, and
       E1's own live watcher-edit stream) through one per-repo overlap
       guard, so no two sources can ever run a pass for the same repo_id
       concurrently (E1 design decision 4, resolving Q3 across sources).
       """

       def __init__(
           self,
           process_registry: PyrightProcessRegistry,
           write_queue: WriteQueue,
           db_path_for: Callable[[str], str],
           *,
           walk_repo: Callable[[str], Iterable[tuple[str, str]]] = walk_repo,
           quiet_seconds: float = 30.0,
           max_wait_seconds: float = 300.0,
       ) -> None:
           self._process_registry = process_registry
           self._write_queue = write_queue
           self._db_path_for = db_path_for
           self._walk_repo = walk_repo
           self._quiet_seconds = quiet_seconds
           self._max_wait_seconds = max_wait_seconds
           self._lock = threading.Lock()
           self._running: set[str] = set()
           self._pending_rerun: set[str] = set()
           self._quiet_timers: dict[str, threading.Timer] = {}
           self._first_pending_at: dict[str, float] = {}

       def trigger_now(self, repo_id: str, repo_root: str) -> None:
           """Bootstrap/migration/reconciliation completions: no debounce --
           each is already a naturally one-shot event, not a live edit
           stream. Matches D6's on_indexed hook signature exactly, so
           create_daemon()'s existing `_on_indexed` closure becomes a thin
           call to this instead of directly to trigger_enrichment.
           """
           with self._lock:
               self._fire_or_mark_pending_locked(repo_id, repo_root)

       def on_watcher_edit(self, repo_id: str, repo_root: str) -> None:
           """Tier-1's own already-debounced (~500ms) signal that repo_id
           had files touched. Applies E1's own, separate, much longer
           hybrid debounce-with-cap (Q7): resets a quiet-period timer on
           every call, but never delays past max_wait_seconds since the
           first pending edit since the repo's last pass.
           """
           with self._lock:
               now = time.monotonic()
               self._first_pending_at.setdefault(repo_id, now)
               if now - self._first_pending_at[repo_id] >= self._max_wait_seconds:
                   self._fire_or_mark_pending_locked(repo_id, repo_root)
                   return
               timer = self._quiet_timers.pop(repo_id, None)
               if timer is not None:
                   timer.cancel()
               timer = threading.Timer(self._quiet_seconds, self._on_quiet_elapsed, args=(repo_id, repo_root))
               timer.daemon = True
               self._quiet_timers[repo_id] = timer
               timer.start()

       def _on_quiet_elapsed(self, repo_id: str, repo_root: str) -> None:
           with self._lock:
               self._quiet_timers.pop(repo_id, None)
               self._fire_or_mark_pending_locked(repo_id, repo_root)

       def _fire_or_mark_pending_locked(self, repo_id: str, repo_root: str) -> None:
           # Caller already holds self._lock.
           if repo_id in self._running:
               self._pending_rerun.add(repo_id)
               return
           self._first_pending_at.pop(repo_id, None)
           self._running.add(repo_id)
           threading.Thread(target=self._run, args=(repo_id, repo_root), daemon=True).start()

       def _run(self, repo_id: str, repo_root: str) -> None:
           _run_triggered_enrichment(
               self._process_registry, self._write_queue, self._db_path_for,
               repo_id, repo_root, self._walk_repo,
           )
           with self._lock:
               self._running.discard(repo_id)
               owed = repo_id in self._pending_rerun
               self._pending_rerun.discard(repo_id)
           if owed:
               with self._lock:
                   self._fire_or_mark_pending_locked(repo_id, repo_root)
   ```

   `_run` calls `runtime.py`'s existing `_run_triggered_enrichment` (D6)
   directly, not `trigger_enrichment()` — `EnrichmentScheduler` already
   guarantees it never calls this from a writer thread (every call site is
   either a `threading.Timer` callback or `_fire_or_mark_pending_locked`'s
   own freshly-spawned thread), so D6's extra thread-hop
   (`trigger_enrichment`'s whole reason for existing, per D6 finding 2) is
   unnecessary here — reusing `_run_triggered_enrichment`'s body directly
   avoids one redundant thread hop per pass. `_run_triggered_enrichment`
   itself gains one new line (decision 5 below): persisting the fresh
   fingerprint after a successful pass, so it stays current after *every*
   trigger source, not just the first.

5. **`_run_triggered_enrichment` (`runtime.py`, D6-authored) gains one new
   step: persist the fresh fingerprint after a successful pass**, so
   decision 3's comparison is always against "state as of the last
   completed pass," not just the very first one:

   ```python
   def _run_triggered_enrichment(process_registry, write_queue, db_path_for, repo_id, repo_root, walk_repo):
       try:
           conn = open_connection(db_path_for(repo_id))
       except Exception:
           _logger.warning(...)
           return
       try:
           run_enrichment_pass(...)  # unchanged
           fingerprint = compute_repo_fingerprint(repo_root)
           if fingerprint is not None:
               IndexMetaStore(conn=conn).set_last_enrichment_fingerprint(fingerprint)
       except Exception:
           _logger.warning(...)
       finally:
           conn.close()
   ```

   A `None` fingerprint (git subprocess failure) leaves the stored value
   unchanged rather than clearing it — the next startup's comparison then
   correctly falls back to "run the pass" (decision 1's fail-toward-
   correctness principle), not to a false "nothing to compare, assume
   fresh."

6. **`create_daemon()` constructs one `EnrichmentScheduler` and re-points
   `_on_indexed` at it; `WatcherRegistry`/`RepoWatcher` gain one new
   injected callback, mirroring `on_indexed`'s own no-op-default shape:**

   ```python
   process_registry = PyrightProcessRegistry()
   scheduler = EnrichmentScheduler(process_registry, write_queue, db_path_for, walk_repo=walk_repo)

   def _on_indexed(repo_id: str, repo_root: str) -> None:
       scheduler.trigger_now(repo_id, repo_root)

   bootstrap = BootstrapCoordinator(..., on_indexed=_on_indexed)
   watchers = WatcherRegistry(write_queue, on_paths_changed=scheduler.on_watcher_edit)
   ```

   `WatcherRegistry.__init__` gains `on_paths_changed: Callable[[str, str],
   None] = lambda repo_id, repo_root: None`, threaded through to each
   `RepoWatcher` it constructs; `RepoWatcher._on_paths_changed` (`watcher.py`
   lines 269–271) gains one line after its existing per-path `submit` loop:
   `self._on_paths_changed_hook(self._repo_id, self._repo_root)`. `trigger_enrichment`
   (D6) stays in `runtime.py`, now unused by `create_daemon()` itself but
   left in place (not deleted) since `tests/daemon/test_runtime.py`'s
   existing D6 tests exercise it directly — removing it would be an
   unrelated test-breaking cleanup outside this slice's scope.

7. **`on_shutdown()` gains no new line.** `EnrichmentScheduler` holds no
   resources of its own needing explicit close (no subprocess, no file
   handle) — its `threading.Timer`s are all `daemon=True` (matching every
   other timer in this codebase, `pyright_process.py`/`watcher.py`), so
   they can never block process exit, and by the time `write_queue.close()`
   /`process_registry.close()` return, any in-flight or about-to-fire pass
   fails fast on its next write-queue submit or LSP call, same reasoning
   D6's own `on_shutdown` comment already gives for its detached thread.

## Files to touch

1. `src/acie/daemon/repo_fingerprint.py` — new module (decision 1).
2. `src/acie/storage/index_meta_store.py` — new column + migration method
   + two accessor methods (decision 2). No schema change to any other
   table.
3. `src/acie/daemon/bootstrap.py` — new `_reconciliation_checked` set,
   `_maybe_schedule_reconciliation_check`/`_run_reconciliation_check`
   (decision 3), one new call in `register()`'s existing branch. No new
   pyright/LSP import — only `compute_repo_fingerprint`.
4. `src/acie/daemon/enrichment_scheduler.py` — new module,
   `EnrichmentScheduler` (decision 4).
5. `src/acie/daemon/runtime.py` — `_run_triggered_enrichment` gains the
   fingerprint-persist step (decision 5); `create_daemon()` constructs
   `scheduler` and re-wires `_on_indexed`/`watchers` (decision 6).
   `trigger_enrichment` left in place, unused by production wiring.
6. `src/acie/daemon/watcher.py` — `WatcherRegistry.__init__`/`RepoWatcher.__init__`
   gain the `on_paths_changed` callback parameter (default no-op);
   `RepoWatcher._on_paths_changed` gains one call (decision 6).
7. `ARCHITECTURE.md` — add an explicit statement that a git repository is
   a permanent, intentional requirement for any repo ACIE indexes (see
   finding 1), near the existing `<repo-id>` derivation text (lines 16, 53).
8. `DAEMON.md` — extend the existing D6 paragraph (line 136) with E1's
   resolution: the three-way one-shot-per-process reconciliation guard,
   the fingerprint mechanism, and the live-watcher debounce+cap, pointing
   at this spec doc for full reasoning (same "extend in place, defer detail"
   convention as every prior slice's `DAEMON.md` edit).
9. Tests: `tests/storage/test_index_meta_store.py` (new column/migration/
   accessors, mirroring existing `head_sha` tests), `tests/daemon/test_bootstrap.py`
   (reconciliation-check firing/skipping/one-shot-per-process, mirroring
   the existing migration-catchup test shapes), `tests/daemon/test_enrichment_scheduler.py`
   (new — coalescing/pending-rerun/debounce-with-cap behavior, including a
   cross-source test: a `trigger_now` call and an `on_watcher_edit`-driven
   call for the *same* repo_id never run concurrently), `tests/daemon/test_watcher.py`
   (new callback fires with the right repo_id/repo_root after a debounced
   batch), `tests/daemon/test_runtime.py` (full-daemon wiring: an edit after
   bootstrap eventually re-enriches without a restart or `acie scan`;
   fingerprint round-trips through a real restart's reconciliation check).

## Not yet specified for a future slice

- **E2: per-site staleness tracking** — named and scoped out at the top of
  this document; a separate design pass.
- **Exact `quiet_seconds`/`max_wait_seconds` values** — `30.0`/`300.0` are
  this session's reasoned defaults (no existing precedent in this codebase
  to pull from; tier 1's `_DEBOUNCE_SECONDS = 0.5` is a different, much
  shorter concern). Tunable via `EnrichmentScheduler`'s constructor
  parameters without a design change if real usage shows they're wrong.
- **An `acie doctor`-style surface for "when did this repo last get a full
  enrichment pass, and why did the last startup skip/run one"** — not
  required by this slice; the wayfinder v1 map's own "Not yet specified"
  section already named a general LSP-availability diagnostics command as
  a future, not-yet-pain-point item (`5d8fa498`).

## Workflow constraints carried into the spec

- One slice per session, stop before commit for review (memory `9b020543`).
- No new third-party runtime dependency — `repo_fingerprint.py` only
  shells `git` via `subprocess`, matching `repo_id.py`'s existing pattern.
- Do not touch `lsp_client.py`/`lsp_protocol.py`/`pyright_process.py`
  internals, `lsp_enrichment.py`, `merge_policy.py`, `scan.py`,
  `dispatch.py`, or `mcp_server.py`/`server.py` — none of E1's decisions
  require any change to these.
- Every trigger source (bootstrap, migration catch-up, startup
  reconciliation, live watcher edits) **must** route through the single
  `EnrichmentScheduler` instance — a second, independent overlap-guard
  implementation for any one of these sources would silently reopen the
  cross-source race named above. This is not optional scope-trimming.
- Do not build E2 (per-site staleness) as part of implementing this spec.

## Verification note

This spec was written against the actual current source read directly this
session (post-D6-commit `0fd9298`): `bootstrap.py` (full read),
`runtime.py` (full read), `watcher.py` (full read), `pyright_process.py`
(full read), `index_meta_store.py` (full read), `repo_id.py` (full read),
plus `DAEMON.md`'s current D6 paragraph and `ARCHITECTURE.md`'s current
repo-id derivation text (grepped for exact line numbers, not assumed).

No source code was modified this session — only this spec doc was written.
