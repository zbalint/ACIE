# v1 Slice D6 — Daemon Trigger Wiring

Status: spec-and-plan only, written for external implementation (same role
split as D1–D5 — this document was **not** implemented in the session that
wrote it; a fresh session should implement it, and another fresh session
should review the resulting diff against this spec before commit).

Implements the seventh and **last** piece of Capability D (wayfinder ticket
`89be4cc1`) per the locked D1–D6 breakdown (SALTMDB memory `3627eece`):
**daemon trigger wiring** — "after a repo's tree-sitter bootstrap index
runs, lazily spawn the enrichment pass" — plus the D3-review staleness-
window follow-up (`87e7e80b`), which D4's own spec explicitly declined to
absorb and handed to D6 by name (`1324b63e`, `D4-MERGE-RULE-ENFORCEMENT-
SPEC.md`'s "What D4 does not do": *"Decision recorded here: `87e7e80b`
stays D6's to own"*).

## omp config for this run

`terra:medium` prewalk → `luna:high` implementer → `luna:max` advisor (per
SALTMDB memory `b422c8dc`, the all-luna D6 experiment — `default`/prewalk
stays `terra:medium`, everything after prewalk, implementer **and**
advisor, runs on `luna`). Recorded here only as the config this run
actually used; not a standing default change (`953b08d4`'s "advisor cost
is non-negotiable" preference is unaffected — the advisor role itself is
still present, only which model fills it changed for this one slice).

## Why D6 is next

D5 is committed (`38d06f9`) and independently review-signed off (memory
`ae74cc2a`), whose own "Next per the locked breakdown" section named D6 as
the last remaining slice. Baseline reconfirmed this session:
`.venv/bin/pytest` → **731 passed, 3 failed** (the same pre-existing
election-port/MCP-transport flake tracked since slice A1, unchanged:
`test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down`,
`test_daemon_stop_actually_terminates_the_os_process`,
`test_serve_mcp_exposes_and_routes_the_ten_tools`).

## What this session verified live against actual source (not guessed)

### 1. There is no `PyrightProcessRegistry` anywhere in the daemon today

`runtime.py`'s `create_daemon()` constructs `WriteQueue`, `BootstrapCoordinator`,
and `WatcherRegistry` — no `PyrightProcessRegistry` (`pyright_process.py`) is
built or held anywhere in the daemon process. The only existing caller of
`run_enrichment_pass` (`lsp_enrichment.py`) is D5's `scan.py`, which
constructs a fresh, one-shot `PyrightProcessRegistry()` per CLI invocation
and closes it before the process exits (`scan.py` lines 46–47, 74–78). D6
must construct the daemon's **first** long-lived `PyrightProcessRegistry`,
matching `WriteQueue`/`BootstrapCoordinator`/`WatcherRegistry`'s existing
"one instance, lazy per-key creation inside it, lives for the daemon's
whole process life" shape (`DAEMON.md` "Write-Queue Concurrency" /
"Incremental Indexing Wiring") — not a fresh one per trigger, since its own
15-minute idle-timeout reuse (D1) only pays off if the same registry
instance is reused across every repo the daemon ever bootstraps.

### 2. A naive wiring — calling `run_enrichment_pass` directly from `BootstrapCoordinator`'s own completion callback — deadlocks the repo's writer thread

This is the load-bearing finding of this spec, verified by reading
`write_queue.py` in full, not assumed. `BootstrapCoordinator._run_indexing_pass`'s
`on_job_done` is registered via `future.add_done_callback(on_job_done)`
(`bootstrap.py` line 246) on each submitted index-file job's `Future`. Every
job's `Future.set_result()` is called from inside `_RepoWriter._run`
(`write_queue.py` lines 273–308) — **on that repo's own dedicated writer
thread** — and `add_done_callback` invokes its callback synchronously,
inline, on whichever thread completes the future. `write_queue.py`'s own
docstring says this explicitly: *"May synchronously run done-callbacks
(e.g. `BootstrapCoordinator.on_job_done`) that call `WriteQueue.submit()`
again for this same repo_key"* (lines 298–304) — so `on_bootstrap_done`
(and, by the same mechanism, the migration-catch-up path's own
`on_pass_done`) already runs **on the writer thread**, today, before D6
changes anything.

If D6 wired `on_bootstrap_done` to call `run_enrichment_pass(...)`
synchronously, `run_enrichment_pass` would call
`write_queue.submit(repo_id, _make_merge_job(relation))` for that **same**
`repo_id`, from **that same writer thread** — `WriteQueue.submit`'s own
`is_reentrant` check (`write_queue.py` lines 111–126) correctly *permits*
this particular re-entrant submit (it exists precisely for this shape of
caller). But `run_enrichment_pass` then calls `submitted[-1].result()`
(`lsp_enrichment.py` line 113) to block until that submitted job resolves —
and the **only** thread that ever dequeues and runs jobs for `repo_id` is
this exact writer thread, which is now blocked waiting on a `Future` that
only it could ever resolve. This is an unconditional, guaranteed deadlock,
not a rare race — every triggered enrichment pass that finds even one site
to resolve would permanently wedge that repo's writer thread (and,
transitively, every future tool call and reindex for that repo, since
nothing else ever drains its queue again).

**Resolution (design decision 2 below): the trigger callback must always
hand off to a brand-new thread before calling `run_enrichment_pass`, never
call it inline from the writer-thread callback.** This mirrors the existing
precedent `BootstrapCoordinator.register()` already sets for its own walk
(`threading.Thread(target=self._run_bootstrap, ...).start()`,
`bootstrap.py` line 110) — the project's established pattern for
background daemon work is exactly "spawn a fresh thread, never block the
caller."

### 3. `BootstrapCoordinator` has exactly two "a repo just got (re)indexed" completion points, and both are one-shot per repo per daemon process lifetime

`on_bootstrap_done` (`_run_bootstrap`, called once a from-scratch repo's
two-pass walk finishes) and `_run_cross_file_migration_if_needed`'s
`on_pass_done=lambda: self._mark_cross_file_migration_done(repo_id)`
(called once an already-`ready` repo's one-time cross-file-pass catch-up
finishes) are the **only** two places `BootstrapCoordinator` ever learns "a
full walk-and-index pass for this repo just completed." Both are
structurally one-shot per `repo_id` per process lifetime: `_run_bootstrap`
is guarded by `_in_progress`/`_ready` (a second `register()` call for the
same `repo_id` short-circuits at `repo_ready()`, `bootstrap.py` line 103),
and the migration catch-up path is separately guarded by
`_migration_checked` (line 169–171) — and the two paths are mutually
exclusive by construction (`register()`'s own branch: `if
self.repo_ready(repo_id): self._maybe_schedule_cross_file_migration(...);
return`, so migration-catchup only ever runs for a repo whose from-scratch
bootstrap either already completed earlier this same process, or never ran
this process at all because the index already existed on disk from a prior
daemon run). **This confirms the locked breakdown's literal wording ("after
a repo's tree-sitter bootstrap index runs") describes a naturally one-shot
trigger, not a per-edit one** — no extra per-repo "already triggered"
bookkeeping is needed in D6 itself; `BootstrapCoordinator`'s existing
guards already provide it for free.

### 4. Tier 1 (the filesystem watcher) is deliberately **not** wired to re-trigger enrichment, and this is a real, in-scope decision the locked breakdown's wording forces, not an oversight

`run_enrichment_pass`'s own `_worklist` (`lsp_enrichment.py` lines
119–131) walks **every** file in the repo on every call (`walk_repo(repo_root)`,
line 68) — it has no notion of "only the files touched since the last
pass." Wiring a fresh enrichment trigger onto `watcher.py`'s own per-file
debounced reindex (`RepoWatcher._on_paths_changed`, `watcher.py` lines
269–271 — the tier-1 path that fires on essentially every saved edit)
would mean every single saved edit anywhere in a repo re-walks the *entire*
repo, re-spawns/reuses a pyright conversation, and re-issues a
`definition` request for every still-unresolved site in the whole
codebase — a cost profile completely disproportionate to "the user saved
one file." The locked breakdown's own wording ("after a repo's tree-sitter
bootstrap index runs") names exactly one trigger event, not "after every
reindex," so this spec treats the one-shot-at-bootstrap-completion trigger
as the deliberate, literal scope — not a narrower version of a broader
intent. See "Not yet specified for a future slice" below for the
consequence this has for post-bootstrap-edit enrichment, and how `acie
scan` (D5) already covers the manual case.

## Resolving the D3-review staleness-window follow-up (`87e7e80b`)

`87e7e80b` asked D6 to explicitly decide and document whether an
enrichment pass is guaranteed to run only against an already-fresh index,
or whether it needs its own staleness guard (e.g. skip a site if the
file's on-disk content no longer matches what `index_file` last saw).

**Decision: no staleness guard is added in v1. The one-shot bootstrap/
migration-catchup trigger (finding 3 above) structurally bounds the
staleness window to the gap between `BootstrapCoordinator`'s own walk
completing its writes and the triggered background thread's own fresh
`walk_repo()` call inside `run_enrichment_pass` (design decision 3 below)
— on the order of milliseconds under any normal daemon startup, since the
trigger fires the instant the last index-file job of that walk resolves.**
A file would have to be edited in that exact narrow window — meaning a
repo would need to be under active edit at the precise moment the daemon
is bootstrapping it for the very first time (or completing a one-time
cross-file-pass migration) — for a stale `AMBIGUOUS` row to describe a
site that has since moved or disappeared. Even then, per `87e7e80b`'s own
analysis, this cannot crash (D3's response mapping already returns `None`
safely for any pyright response that doesn't cleanly resolve, and D4's
merge-policy guard already prevents a stale write from ever regressing a
freshly-reindexed `EXTRACTED` fact) — the worst case is a semantically
wrong `INFERRED` write landing on a site whose surrounding code has
shifted. This is `# shortcut:` in the new `runtime.py` trigger code, named
explicitly: no per-site mtime/content-hash freshness check against
`BootstrapCoordinator`'s own walk snapshot. **Upgrade trigger: a real repo
where this narrow race is actually observed to produce a visibly wrong
`INFERRED` fact** — at which point the fix is to have `BootstrapCoordinator`
pass its own already-read `files: list[tuple[str, str]]` (or at least a
`{path: content_hash}` snapshot of it) through to the triggered pass so
`_worklist` can skip a site whose fresh `walk_repo()` read no longer
matches what was actually indexed, rather than redesigning the trigger
cadence itself.

This is the same class of accepted, explicitly-named eventual-consistency
gap this codebase already lives with elsewhere (`merge_policy.py`'s own
"do not reconcile stale INFERRED rows from cross-pass re-ambiguation"
shortcut, `indexer.py`'s "reindex-on-edit eventually closes that gap" for
deferred-candidate misses) — not a new kind of risk this project hasn't
already accepted, and, per finding 4 above, it is a **one-time** window
(the only trigger this slice wires), not a recurring one.

## Scope (narrow — the last slice, deliberately closing Capability D, not reopening D1–D5)

Two small, surgical edits to already-existing daemon files
(`bootstrap.py` gains one injected callback parameter and two call sites;
`runtime.py` gains one new module-level function, one new daemon-lifetime
object, and one new wiring line each in `create_daemon()` and
`on_shutdown()`) — no new module. Does not touch `dispatch.py` (no new RPC
method, no new `DISPATCH_TABLE` entry — this is a pure internal daemon-
lifecycle side effect, invisible to the RPC envelope), `mcp_server.py` /
`server.py` (no new MCP tool, no new transport-level behavior),
`merge_policy.py` (D4's write-time guard is reused completely unmodified —
the triggered pass writes through the exact same `write_queue.submit(...,
_make_merge_job(...))` path D3/D4 already built), `lsp_client.py` /
`lsp_protocol.py` / `pyright_process.py` internals (D6 only *constructs*
one `PyrightProcessRegistry`; nothing about its own lifecycle logic
changes), `lsp_enrichment.py` (`run_enrichment_pass` itself is called with
exactly the same keyword arguments D5's `scan.py` already calls it with —
no signature change), `scan.py` (D5's own foreground CLI path is completely
independent of the daemon trigger and untouched), and `watcher.py` (see
finding 4 above — deliberately not re-wired to trigger enrichment).

### What D6 does **not** do (named explicitly, not silently dropped)

- **Re-triggering enrichment after the initial bootstrap/migration-catchup
  pass.** Per finding 3–4 above, this is the literal, one-shot scope the
  locked breakdown names. Code edited after a repo's daemon-lifetime
  bootstrap completes accumulates new `AMBIGUOUS`/unresolved sites (via
  tier 1–4 incremental reindexing, all of which remain fully functional and
  unchanged) that no further automatic enrichment pass will ever revisit
  for the life of that daemon process. `acie scan` (D5) remains the
  existing, already-shipped way to force a fresh full-repo enrichment pass
  on demand — this spec does not duplicate or supersede it. See "Not yet
  specified for a future slice" below.
- **A per-site staleness guard for the bootstrap-trigger race window.**
  See "Resolving the D3-review staleness-window follow-up" above —
  deliberately deferred, with a named upgrade trigger.
- **Rate-limiting, cancellation, or de-duplication of concurrent triggered
  passes.** Not needed structurally: finding 3 already establishes at most
  one trigger fires per `repo_id` per daemon process lifetime, and
  different repos' triggers run on independent threads against independent
  `PyrightProcessRegistry` per-`repo_root` state (already thread-safe per
  D1's own per-root creation locks, `pyright_process.py` lines 105–107) —
  there is no scenario under this design where two triggered passes for
  the *same* repo could ever run concurrently.
- **Joining or explicitly cancelling an in-flight triggered-enrichment
  background thread on daemon shutdown.** See design decision 5 below —
  named `# shortcut:`, not silently omitted.

## Design decisions

1. **`BootstrapCoordinator.__init__` gains one new keyword parameter:
   `on_indexed: Callable[[str, str], None] = lambda repo_id, repo_root:
   None`.** Default is a no-op so every existing caller/test that
   constructs a `BootstrapCoordinator` without it (`tests/daemon/
   test_bootstrap.py`'s `_make_coordinator` helper, `runtime.py`'s own
   existing construction before this edit) is completely unaffected —
   this is the regression-proof shape D1–D5 have each used for their own
   signature extensions (e.g. D4's `confidence_rank()` promotion,
   `store.py`'s optional `conn=` kwarg from the write-queue slice).
   `bootstrap.py` itself gains **no** new import — it stays entirely
   agnostic of pyright/LSP/enrichment concerns; the callback's *name* is
   deliberately generic ("an indexing pass for this repo just finished"),
   not `on_enrichment_ready` or similar, so this module's existing
   single responsibility (readiness tracking + the walk) is not
   compromised by a second concern. `DAEMON.md` already names `runtime.py`
   as "the seam where the daemon's existing transport, bootstrap, write
   queue, and dispatch modules become one running process" — this is
   exactly that seam doing its job.

2. **Two call sites inside `bootstrap.py`, both firing `on_indexed` only
   after a real (non-empty-repo) walk-and-index pass has fully committed:**

   ```python
   def on_bootstrap_done() -> None:
       self._mark_ready(repo_id)
       self._mark_cross_file_migration_done(repo_id)
       self._on_indexed(repo_id, repo_root)
   ```

   and, inside `_run_cross_file_migration_if_needed` (replacing the
   current one-line lambda with a small named closure so both post-pass
   actions happen in order):

   ```python
   if not files:
       self._mark_cross_file_migration_done(repo_id)
       return

   def _finish_migration() -> None:
       self._mark_cross_file_migration_done(repo_id)
       self._on_indexed(repo_id, repo_root)

   self._run_indexing_pass(repo_id, files, on_pass_done=_finish_migration)
   ```

   The existing `if not files:` empty-repo short-circuits in **both**
   `_run_bootstrap` (line 120–128) and `_run_cross_file_migration_if_needed`
   (line 211–213) deliberately do **not** call `on_indexed` — an empty
   repo has nothing for `walk_repo` to hand `run_enrichment_pass` either,
   so triggering a pyright spawn for zero possible sites would be pure
   waste. This is a real, named decision (not a silent gap): a repo that
   starts empty and later gains files goes through tier 1 (the watcher)
   for those files, same as any other post-bootstrap edit — consistent
   with "What D6 does not do"'s first bullet.

3. **New module-level function in `runtime.py`, `trigger_enrichment`,
   mirroring `ensure_fresh`'s existing shape** (a plain, directly
   unit-testable function taking its dependencies as explicit parameters,
   not a `create_daemon()` closure) **— and its own helper, `_run_triggered_
   enrichment`, which does the actual work on a dedicated thread:**

   ```python
   def trigger_enrichment(
       process_registry: PyrightProcessRegistry,
       write_queue: WriteQueue,
       db_path_for: Callable[[str], str],
       repo_id: str,
       repo_root: str,
       *,
       walk_repo: Callable[[str], Iterable[tuple[str, str]]] = walk_repo,
   ) -> None:
       """D6: fires an opportunistic enrichment pass after BootstrapCoordinator
       finishes indexing repo_id for the first time (or completes its one-time
       cross-file-pass migration catch-up). Always hands off to a fresh thread
       and returns immediately -- see this spec's finding 2 for why calling
       run_enrichment_pass inline here would deadlock repo_id's own writer
       thread (this function is itself invoked from on_indexed, which fires
       synchronously on that writer thread).
       """
       threading.Thread(
           target=_run_triggered_enrichment,
           args=(process_registry, write_queue, db_path_for, repo_id, repo_root, walk_repo),
           daemon=True,
       ).start()


   def _run_triggered_enrichment(
       process_registry: PyrightProcessRegistry,
       write_queue: WriteQueue,
       db_path_for: Callable[[str], str],
       repo_id: str,
       repo_root: str,
       walk_repo: Callable[[str], Iterable[tuple[str, str]]],
   ) -> None:
       try:
           conn = open_connection(db_path_for(repo_id))
       except Exception:  # noqa: BLE001 -- opportunistic, never load-bearing (D1/D3's own principle).
           _logger.warning("D6 enrichment trigger: could not open index for repo %r", repo_id, exc_info=True)
           return
       try:
           run_enrichment_pass(
               repo_root=repo_root,
               repo_id=repo_id,
               process_registry=process_registry,
               write_queue=write_queue,
               walk_repo=walk_repo,
               symbol_store=SymbolStore(conn=conn),
               relation_store=RelationStore(conn=conn),
           )
       except Exception:  # noqa: BLE001 -- same "never break anything else" principle; logged, not raised.
           _logger.warning("D6 enrichment trigger: enrichment pass failed for repo %r", repo_id, exc_info=True)
       finally:
           conn.close()
   ```

   This deliberately reuses `scan.py`'s own established pattern (a fresh,
   short-lived `open_connection(db_path)` plus fresh `SymbolStore`/
   `RelationStore` bound to it, `finally: conn.close()`) rather than
   inventing a second convention — the only structural difference from
   `scan.py`'s call is that `process_registry` and `write_queue` are the
   daemon's own long-lived instances, injected, not constructed fresh per
   call (see finding 1). `run_enrichment_pass` itself needs **zero**
   changes — every keyword argument here matches its existing signature
   exactly (`lsp_enrichment.py` lines 38–47).

4. **`create_daemon()` constructs exactly one `PyrightProcessRegistry()`
   for the daemon's whole process life**, alongside its existing
   `write_queue`/`bootstrap`/`watchers` construction, and wires `on_indexed`
   through a small closure:

   ```python
   write_queue = WriteQueue(db_path_for=db_path_for)
   process_registry = PyrightProcessRegistry()

   def _on_indexed(repo_id: str, repo_root: str) -> None:
       trigger_enrichment(process_registry, write_queue, db_path_for, repo_id, repo_root, walk_repo=walk_repo)

   bootstrap = BootstrapCoordinator(
       write_queue=write_queue,
       db_path_for=db_path_for,
       walk_repo=walk_repo,
       on_indexed=_on_indexed,
   )
   watchers = WatcherRegistry(write_queue)
   ```

   `_on_indexed` is the *only* place `runtime.py` couples `bootstrap` to
   `lsp_enrichment`/`pyright_process` — everything else in `create_daemon()`
   is unchanged. No new CLI flag, no new `DaemonServer` constructor
   argument: `PyrightProcessRegistry()`'s own defaults (basedpyright-first
   binary discovery, 900s idle timeout, 5s terminate timeout — all D1
   decisions, unmodified) are reused exactly as-is, matching this slice's
   "no new runtime dependencies, no new configuration surface" scope.

5. **`on_shutdown()` gains one new line, sharing the existing drain
   deadline:**

   ```python
   def on_shutdown() -> None:
       deadline = time.monotonic() + _SHUTDOWN_DRAIN_TIMEOUT_SECONDS
       watchers.close(timeout=max(0.0, deadline - time.monotonic()))
       process_registry.close(timeout=max(0.0, deadline - time.monotonic()))
       write_queue.close(timeout=max(0.0, deadline - time.monotonic()))
   ```

   Placed between `watchers.close()` and `write_queue.close()`: closing
   `process_registry` first among the two new/existing enrichment-adjacent
   paths terminates any live pyright children (so an in-flight triggered
   pass's next LSP request fails fast with `ConnectionError`, which
   `run_enrichment_pass` already handles by breaking out of its loop and
   returning — `lsp_enrichment.py` lines 89–91) *before* `write_queue.close()`
   drains whatever merge jobs that pass had already submitted — this
   ordering lets an in-flight pass's already-resolved sites still commit
   cleanly while stopping it from starting any new pyright round-trips
   during shutdown. One shared deadline continues to span all three calls
   (not a fresh budget each — this project's own established shutdown-
   budget discipline, `runtime.py`'s existing comment above
   `_SHUTDOWN_DRAIN_TIMEOUT_SECONDS`), so total shutdown time stays bounded
   by `_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` overall, not 3× it.

   `# shortcut:` a still-running `_run_triggered_enrichment` background
   thread (from `trigger_enrichment`, design decision 3) is **not**
   explicitly joined or cancelled here — it is a `daemon=True` thread, so
   it can never itself block process exit, and by the time
   `write_queue.close()` returns, both of its own dependencies
   (`process_registry`, that repo's writer thread) have already stopped
   accepting new work, so the thread's own `run_enrichment_pass` call
   fails fast on its very next LSP request or write-queue submit rather
   than hanging. Upgrade trigger: observed evidence that a shutdown
   immediately followed by a fresh daemon spawn for the same repo races a
   still-running D6 background thread against the new process's own fresh
   `PyrightProcessRegitry`/`WriteQueue` state in a way that actually
   matters (structurally implausible today, since `os._exit(0)` — see
   `DAEMON.md`'s "Shutdown / Stop Semantics" — terminates the whole
   process, and every thread including this one, the instant `on_shutdown()`
   returns and the shutdown RPC/signal path calls it).

6. **Imports added to `runtime.py`**: `threading` (already imported by
   `bootstrap.py`/`watcher.py`, new to `runtime.py` itself — needed for
   `trigger_enrichment`'s `threading.Thread(...)`), `from
   acie.daemon.lsp_enrichment import run_enrichment_pass`, `from
   acie.daemon.pyright_process import PyrightProcessRegistry`, `from
   acie.storage.connection import open_connection`, `from
   acie.storage.relation_store import RelationStore`, `from
   acie.storage.symbol_store import SymbolStore`. No new third-party
   dependency — every one of these is an already-existing first-party
   module (D1/D3/the storage layer), reused, not duplicated.

## Files to touch

1. `src/acie/daemon/bootstrap.py` — add the `on_indexed` parameter
   (design decision 1) and its two call sites (design decision 2). No
   other line changes; `bootstrap.py` gains no new import.
2. `src/acie/daemon/runtime.py` — add `trigger_enrichment`/
   `_run_triggered_enrichment` (design decision 3), construct
   `process_registry` and wire `on_indexed` in `create_daemon()` (design
   decision 4), add `process_registry.close(...)` to `on_shutdown()`
   (design decision 5), add the six new imports (design decision 6). No
   other line changes — `dispatch`, `ensure_fresh`, `_resolve_repo`,
   `register_repo`, `dispatch()`'s own closure, and `_dispatch_notify_hook`
   are all untouched.
3. `tests/daemon/test_bootstrap.py` — extend `_make_coordinator` (or add a
   second helper) with an optional `on_indexed` fake; new tests: (a) the
   default no-op preserves every existing test's behavior unmodified
   (regression proof — every current test in this file must still pass
   with zero edits, since the new parameter defaults to a no-op); (b) a
   from-scratch bootstrap for a non-empty repo calls `on_indexed(repo_id,
   repo_root)` exactly once, only after both indexing passes have fully
   resolved (assert ordering against a fake `walk_repo`/write-queue that
   records call order, or assert it fires only once `repo_ready(repo_id)`
   is already `True`); (c) an empty repo's from-scratch bootstrap does
   **not** call `on_indexed`; (d) an already-`ready` repo needing the
   one-time cross-file-pass migration catch-up calls `on_indexed` exactly
   once after that catch-up's own pass resolves; (e) an empty-repo
   migration catch-up does **not** call `on_indexed`; (f) `on_indexed` is
   never called a second time for the same `repo_id` across two `register()`
   calls once the repo is fully `ready` and migration-checked (confirms
   finding 3's one-shot claim as an executable test, not just prose).
4. `tests/daemon/test_runtime.py` — new tests covering `trigger_enrichment`
   directly and its end-to-end wiring:
   - **The deadlock-regression test (must be included, not optional; this
     is the one test that directly proves finding 2 is actually fixed, not
     just designed around):** using a real `WriteQueue` against a real
     `tmp_path` sqlite file, submit a job for some `repo_id` whose body
     itself calls `trigger_enrichment(...)` (simulating exactly what
     `on_indexed` does when invoked from that repo's own writer-thread
     callback), where the injected `run_enrichment_pass`-reachable path
     (via a fake/monkeypatched `process_registry`+`walk_repo` producing at
     least one resolvable site, or a fake `run_enrichment_pass` substituted
     via monkeypatch that itself submits-and-waits-on a follow-up job for
     the same `repo_id`) exercises the exact reentrant-submit-then-`.result()`
     shape `run_enrichment_pass` uses — assert the whole scenario completes
     within a bounded timeout (e.g. `Future.result(timeout=5.0)`, failing
     the test on `TimeoutError` rather than hanging the test suite forever
     if the deadlock were ever reintroduced).
   - `trigger_enrichment` returns near-instantly regardless of how long
     the underlying pass takes — verified with a slow fake `process_registry
     .ensure_process` (blocks on a `threading.Event` the test controls) and
     asserting the calling thread's own `trigger_enrichment(...)` call
     returns before that event is ever set.
   - An exception raised inside `_run_triggered_enrichment` (e.g. a fake
     `db_path_for` that raises, or a monkeypatched `run_enrichment_pass`
     that raises) is caught and logged, never propagated to or crashing
     the thread that called `trigger_enrichment`.
   - An end-to-end test (real git repo via `create_daemon()`+`server.start()`,
     matching this file's existing `test_runtime_bootstraps_a_real_repo_...`
     convention) with two files whose cross-file `calls`/`inherits` edge
     the tree-sitter pass alone would leave `AMBIGUOUS`/unresolved: after
     the repo becomes ready (polling `find_symbol`/`INDEX_NOT_READY`
     exactly like the existing bootstrap tests already do), poll
     (bounded, generous timeout — a real pyright spawn is slow) for the
     site to have become `INFERRED` via `graph`/`impact_analysis`, with
     **no** `acie scan` CLI or manual trigger involved — confirming the
     daemon really did fire enrichment on its own after bootstrap. Skipped
     (not failed) if `shutil.which("basedpyright-langserver")` finds
     nothing in the test environment, matching D1's own graceful-
     degradation precedent for CI/dev-machines without basedpyright
     installed.
   - `create_daemon()` constructs exactly one `PyrightProcessRegistry`
     shared across multiple repos' triggers (two real repos registered
     against one daemon instance; assert both repos' triggered passes
     observe/reuse the same registry object — or, more simply, assert
     `create_daemon()`'s returned closures close over one single
     `PyrightProcessRegistry` instance via a light introspection/monkeypatch
     seam, avoiding a slow double-pyright-spawn test).
   - `server.shutdown()` calls `process_registry.close(...)` within the
     existing shared drain deadline (monkeypatch/spy on
     `PyrightProcessRegistry.close` the same way an existing test might
     already spy on `watchers.close`/`write_queue.close`, or assert no live
     pyright child process remains after shutdown when one was actually
     spawned).
5. `DAEMON.md` — edit the LSP/pyright Enrichment section's existing
   sentence "Trigger wiring remains D6 work." (currently the trailing
   sentence of the `run_enrichment_pass` paragraph) to describe what D6
   actually built, and add a short new "Resolved during implementation"
   paragraph documenting: `BootstrapCoordinator`'s new `on_indexed` hook,
   the daemon's one long-lived `PyrightProcessRegistry`, the always-a-
   fresh-thread dispatch (and why — finding 2, briefly), the one-shot-
   per-repo trigger scope (finding 3–4), and this spec's resolution of the
   `87e7e80b` staleness-window follow-up (one sentence, pointing at this
   spec doc for the full reasoning — matching how D4's own paragraph in
   `DAEMON.md` stays terse and defers detail to its own spec doc). Also
   edit `merge_policy.py`'s existing `DAEMON.md` paragraph, whose own
   trailing sentence currently reads "...and trigger cadence remain
   outside this slice." — trigger cadence is now resolved; update in
   place, same "extend in place" convention C2–C6/D1–D5 already
   established. No `ARCHITECTURE.md` change (no MCP surface, no schema
   change — D6 adds no new column, no new tool, no new CLI flag).

## Not yet specified for a future slice

Explicitly out of the locked D1–D6 breakdown (Capability D is complete
once D6 lands), not silently forgotten:

- **Ongoing re-enrichment after the initial bootstrap trigger.** Per
  "What D6 does not do," code edited after a repo's one-shot trigger has
  already fired accumulates new unresolved/`AMBIGUOUS` sites that nothing
  automatic ever revisits again for that daemon process's life — only a
  manual `acie scan` (D5) or a full daemon restart (which re-triggers
  bootstrap/migration-catchup only if the repo's `index.sqlite` doesn't
  already exist, or only re-runs the migration catch-up if
  `CURRENT_CROSS_FILE_PASS_VERSION` bumps again) reaches those newer sites.
  A future slice building a debounced, whole-repo-cost-aware recurring
  trigger (e.g. wired off tier 1's own debounce timer, but coalesced to at
  most once per some much longer interval given `_worklist`'s whole-repo
  cost — finding 4) is a real, plausible next step but is not part of this
  locked breakdown and needs its own design pass, not an assumption
  smuggled into D6.
- **The `# shortcut:` upgrade named in "Resolving the D3-review
  staleness-window follow-up"** — a per-site freshness check against
  `BootstrapCoordinator`'s own walk snapshot, only worth building once a
  real repo demonstrates the race actually matters.

## Workflow constraints carried into the spec

- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the 3 known pre-existing failures (baseline
  reconfirmed this session: 731 passed, 3 failed).
- No new runtime dependencies — every new import in `runtime.py` is an
  already-existing first-party module.
- Do not touch `dispatch.py`, `mcp_server.py`/`server.py`, `merge_policy.py`,
  `lsp_client.py`/`lsp_protocol.py`/`pyright_process.py` internals,
  `lsp_enrichment.py`, or `scan.py` — see "Scope" above for why none of
  these need any change.
- Do not wire tier 1 (the filesystem watcher) to re-trigger enrichment —
  see finding 4; this is a deliberate, named scope boundary, not an
  oversight to "complete" during implementation.
- The triggered enrichment call **must** run on its own thread, never
  inline from `on_indexed`/`BootstrapCoordinator`'s own writer-thread
  callback — see finding 2. The deadlock-regression test in "Files to
  touch" item 4 is not optional; implementation is not done until it
  exists and passes.
- Do not add a per-site staleness guard — this spec's resolution of
  `87e7e80b` is "no guard in v1, named `# shortcut:` with an explicit
  upgrade trigger," not "leave the question open."
- Standing dogfooding ground rule (wayfinder map `5d8fa498`'s Notes) still
  applies: live `mcp__acie__*` tools stay v0-pinned throughout
  implementation.

## Verification note

This spec was written against the actual current source read directly this
session: `runtime.py` (full read — `create_daemon`, `ensure_fresh`,
`on_shutdown`, `_resolve_repo`, `register_repo`, `dispatch`,
`_dispatch_notify_hook`), `bootstrap.py` (full read — `BootstrapCoordinator.
__init__`, `repo_ready`, `register`, `_run_bootstrap`,
`_maybe_schedule_cross_file_migration`, `_run_cross_file_migration_if_needed`,
`_run_indexing_pass`, `make_index_job`), `watcher.py` (full read —
`make_reindex_job`, `_DebouncedEventHandler`, `RepoWatcher`,
`WatcherRegistry`, confirming tier 1's own per-file, whole-repo-agnostic
trigger shape that finding 4 explicitly declines to reuse), `write_queue.py`
(full read — `WriteQueue.submit`'s reentrant-submit handling and its own
docstring on synchronous done-callbacks, `_RepoWriter._run`'s
`future.set_result` call site, confirming finding 2's deadlock claim from
primary source, not inference), `lsp_enrichment.py` (full read —
`run_enrichment_pass`'s exact signature and its own
`submitted[-1].result()` blocking-drain call, confirming exactly which
line would deadlock), `merge_policy.py` (full read — confirming D4's
guard needs zero changes, the triggered pass writes through the identical
`_make_merge_job` path), `pyright_process.py` (full read —
`PyrightProcessRegistry`'s constructor defaults, per-root creation locks,
`close()`'s bounded-teardown shape, confirming it's already safe to share
across concurrently-triggered repos with no new synchronization), `scan.py`
(full read — the exact `open_connection`/fresh-store/`finally: conn.close()`
pattern design decision 3 reuses), `index_meta_store.py` (full read,
confirming `cross_file_pass_done()`/`mark_cross_file_pass_done()`'s
monotonic-version shape and that migration catch-up is itself already a
real, one-time-per-repo event D6 can safely hook), `DAEMON.md`'s current
LSP-enrichment and merge-policy paragraphs (to find the exact trailing
sentences this slice's `DAEMON.md` edit replaces), and
`tests/daemon/test_runtime.py`/`tests/daemon/test_bootstrap.py` in full
(to match this slice's new tests to their established conventions —
real-git-repo end-to-end tests polling on `INDEX_NOT_READY`, and
`_make_coordinator`'s fake-injection shape for bootstrap-level unit tests).

No source code was modified this session — only this spec doc was written
(`git status --short` confirms `?? D6-DAEMON-TRIGGER-SPEC.md` as the only
change).
