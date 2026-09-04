# v1 Slice D5 — `acie scan` CLI

Status: spec-and-plan only, written for external implementation (same role
split as D1–D4 — this document was **not** implemented in the session that
wrote it; a fresh session should implement it, and another fresh session
should review the resulting diff against this spec before commit).

**Implementation method: TDD.** The implementing session must use this
workspace's `tdd` skill (red-green-refactor, one vertical slice at a time,
testing at the public seams named below — `run_scan()`, the promoted
`make_index_job`/`walk_repo`, and the `acie scan` CLI surface) rather than
writing the implementation first and tests after. This is a standing
instruction for this slice, not a suggestion.

Implements the fifth piece of Capability D (wayfinder ticket `89be4cc1`) per
the locked D1–D6 breakdown (SALTMDB memory `3627eece`): **`acie scan [path]`
— a blocking foreground full-pipeline CLI command, pre-warming convenience**.
It runs the exact same walk-and-index-and-enrich work a daemon would
eventually do lazily on first RPC touch, but synchronously, in the invoking
process, with no daemon involved at all — so a repo's `index.sqlite` can be
fully warmed (tree-sitter index *and* one pyright enrichment pass) ahead of
time, e.g. in a CI step or right after cloning, and a later `acie serve-mcp`
/ `acie daemon start` never has to pay that first-touch bootstrap cost
itself. It is named explicitly in the locked breakdown as **never
load-bearing** — nothing else in this system requires `acie scan` to have
been run; the daemon's own on-demand bootstrap (`BootstrapCoordinator`,
already committed) is fully sufficient on its own.

## Why D5 is next

D4 is committed (`0ee3250`) and independently review-signed off (memory
`3521a58a`), whose own "Next per the locked breakdown" section named D5 as
next, with D6 (daemon trigger wiring for LSP enrichment, plus the D3-review
staleness-window follow-up `87e7e80b` explicitly handed to D6 by D4's spec)
still to come after.

Baseline reconfirmed this session: `.venv/bin/pytest` → **716 passed, 3
failed** (the same pre-existing election-port/MCP-transport flake tracked
since slice A1, unchanged: `test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down`,
`test_daemon_stop_actually_terminates_the_os_process`,
`test_serve_mcp_exposes_and_routes_the_ten_tools`).

## What this session verified live against actual source (not guessed)

### 1. No prior design intent for `acie scan` exists anywhere in this repo

`DAEMON.md`'s "CLI Surface" table (currently five rows: `serve-mcp`,
`daemon start`, `daemon stop`, `daemon status`, `notify-hook`) names no
`scan` row, and grepping `ARCHITECTURE.md`/`DAEMON.md`/`README.md` for
"scan"/"pre-warm" finds nothing — the only source of truth for this slice's
shape is the locked breakdown's one line ("`acie scan [path]` CLI command
(blocking foreground full pipeline, pre-warming convenience)") plus the
existing daemon internals it must reuse. Every concrete decision below is
this spec's own, not a recovery of some earlier plan.

### 2. `BootstrapCoordinator.register()` cannot be reused as-is — its "trust an existing `index.sqlite`" semantics would make a second `acie scan` run silently do nothing

Read `bootstrap.py` in full. `register()`'s very first line is
`if self.repo_ready(repo_id): ...; return` — and `repo_ready()` treats *any*
pre-existing `index.sqlite` on disk as proof the repo needs no walk (module
docstring: "a repo that already has an `index.sqlite` from any prior run is
trusted as ready immediately"). That trust is exactly right for a **daemon**
(avoid re-walking a repo on every RPC across its whole process lifetime) but
exactly wrong for a **user-invoked `acie scan` command**: a user re-running
`scan` after editing files clearly wants a real re-walk-and-index of the
current file contents, not a silent no-op (or, at best, the unrelated
one-time `_maybe_schedule_cross_file_migration` catch-up check, which itself
no-ops once its own flag is already set). **Decision: D5 does not call
`BootstrapCoordinator.register()` at all** — it runs its own
always-unconditional two-pass walk (design decision 3), independent of
whatever `repo_ready()` would currently say for this repo.

### 3. Skipping `BootstrapCoordinator` still requires reproducing its "second pass" and its migration-flag write, or a later daemon touching the same repo silently redoes the walk anyway

`_run_bootstrap`'s own comment explains why a from-scratch bootstrap runs
`_run_indexing_pass` **twice** over the identical file list: `os.walk`'s
discovery order is arbitrary, so a file indexed before something it
cross-file-calls/inherits/overrides into leaves that edge unresolved after
just one pass; a second pass, now that the full repo's `SymbolStore` is
populated, resolves whatever the first pass's arbitrary order missed. Any
`acie scan` pipeline that does only one pass would produce a **strictly
worse** index than what the daemon's own bootstrap would have produced —
directly defeating "pre-warming" (a later daemon touch would still need to
notice and backfill the gap). Separately, `register()`'s "repo already
ready" branch calls `_maybe_schedule_cross_file_migration`, which checks
`IndexMetaStore.cross_file_pass_done()` and, if unset, does a **third**
full walk-and-index pass as one-time catch-up. If `acie scan` never sets
that flag, the very first daemon RPC touching this repo after a scan
would trigger exactly this redundant third pass — silently burning the
entire time `acie scan` was meant to save. **Decision: `run_scan()` must
(a) run the identical two-pass discipline `_run_bootstrap` uses, and (b)
explicitly call `IndexMetaStore(conn).mark_cross_file_pass_done()` as its
final write**, exactly mirroring `_mark_cross_file_migration_done`'s own
job shape, so a daemon started after a scan sees both `repo_ready() ==
True` *and* the migration flag already set, and does zero redundant work.
Confirmed no other flag or on-disk marker exists that daemon startup checks
before trusting a pre-existing index — this one `index_meta` column is the
only one.

### 4. `run_enrichment_pass` (D3) is directly reusable with zero changes, given a real `WriteQueue` and a `PyrightProcessRegistry` for this one repo

Re-read `lsp_enrichment.py`'s committed `run_enrichment_pass(repo_root,
repo_id, process_registry, write_queue, walk_repo, symbol_store,
relation_store, observed_at_fn=...)` in full. Every dependency it takes is
already a small, independently-constructible object with no daemon-process
lifetime requirement: `PyrightProcessRegistry()` (D1, no daemon needed to
spawn a `pyright-langserver` child), a `WriteQueue(db_path_for=...)` (D3's
`_make_merge_job`/D4's `merge_policy.apply_enrichment_write` already flow
through this unmodified), and fresh, caller-owned `SymbolStore`/
`RelationStore` instances (matching `DAEMON.md`'s already-established
"Store lifecycle: fresh-per-call" pattern for every read-path store in this
codebase — `dispatch.py`'s nine tools all do exactly this). No new
"scan-mode" branch or parameter is needed inside `lsp_enrichment.py` at all.
Confirmed the module already degrades gracefully with no process/binary
available (`ensure_process` returns `None` → `run_enrichment_pass` logs a
warning and returns `[]`), so `acie scan` on a machine where
`basedpyright-langserver` fails to spawn for any reason still completes
successfully with an indexing-only result — no special-casing needed in
`scan.py` for that path either.

### 5. `walk_repo` and `_make_index_job` are private/inline, and are D5's actual points of necessary reuse

`runtime.py`'s `create_daemon()` builds `walk_repo` as a **local closure**
(`.gitignore`-aware, composing `ignore.get_ignore_matcher` +
`dispatch._read_source_files`) — not importable by `scan.py` as written.
`bootstrap.py`'s `_make_index_job(path, source_text)` (private, single
leading underscore) builds the per-file write-queue job `index_file()`
itself, but its `job(conn)` closure currently **discards** `index_file()`'s
return value (calls it as a bare statement, not `return index_file(...)`) —
matching `BootstrapCoordinator`'s own accepted shortcut of never inspecting
a bootstrap job's result (`_run_indexing_pass`'s `on_job_done` comment:
"its exception is never surfaced anywhere else since bootstrap doesn't
retain per-file futures"). `acie scan`'s own summary output needs those
counts (files indexed, symbols/relations upserted), so this return value
must start flowing back through the `Future`. This is now a **second**
genuine caller for both (Coding Standard 4 — extend rather than duplicate),
with direct precedent already set twice in this codebase for promoting a
private single-caller helper to a public multi-caller one the moment a real
second caller appears: `watcher.py`'s `_make_watch_job` → `make_reindex_job`
(promoted when tier 4's `ensure_fresh` became its second caller, `DAEMON.md`
line 110) and D3→D4's `_make_upsert_job` → `_make_merge_job` rename. See
design decisions 1–2.

## Scope (narrow, deliberately not D6)

One new top-level module, `src/acie/scan.py` (`run_scan`, `ScanResult`,
`ScanError`) — placed at `src/acie/` rather than under `src/acie/daemon/`
because, unlike `bootstrap.py`/`lsp_enrichment.py`/`merge_policy.py`, it
holds no daemon-process-lifetime state and never touches the daemon's
transport/discovery/dispatch layer at all (no socket, no RPC, no
`~/.acie/daemon.json`) — it is a one-shot pipeline function, closer in kind
to `indexer.py` (also top-level) than to anything under `daemon/`. `cli.py`
already imports daemon internals directly today (`daemon.client`,
`daemon.server`, `mcp_server`), so `scan.py` (top-level) importing from
`daemon/` (`write_queue`, `pyright_process`, `lsp_enrichment`, `bootstrap`,
`dispatch`) follows the exact same existing import direction, not a new
one.

Two small, precedented promotions inside already-existing daemon files
(design decisions 1–2), one new `acie scan` CLI subcommand
(`cli.py`/`DAEMON.md`), and their tests. Does **not** touch
`runtime.py`'s `dispatch()`/`register_repo()` request path, `merge_policy.py`,
`lsp_client.py`/`lsp_protocol.py`/`pyright_process.py`'s own internals,
`mcp_server.py`, or any MCP tool surface — D5 adds a CLI entry point that
*assembles* already-existing, already-reviewed daemon building blocks in a
new (foreground, non-daemon) way; it invents no new indexing, merge, or LSP
logic of its own. No D6 (daemon trigger wiring for automatic enrichment
passes, or the D4-deferred staleness-window follow-up `87e7e80b`) is
implemented here.

### What D5 deliberately does **not** do (named explicitly, not silently dropped)

- **No incremental/staleness-aware re-scan.** `acie scan` run a second time
  on an unchanged repo re-walks and re-indexes every file from scratch
  (cheap, idempotent upserts — see finding 2's rationale for why this is
  the *correct* choice, not an oversight) rather than consulting
  `file_state_store.py`'s mtime/hash staleness table the way the tier-1
  watcher does. Matches `BootstrapCoordinator`'s own bootstrap pass, which
  doesn't consult it either — not a new inconsistency `acie scan`
  introduces.
- **No `--no-enrich` / `--skip-pyright` flag.** The locked breakdown calls
  this the "full pipeline" command; the indexing-only case is already
  covered for free by D3's existing graceful degradation when pyright is
  unavailable (finding 4), so a flag to force that path adds a second way
  to reach a state the pipeline already reaches safely on its own whenever
  needed. A minimal, natural follow-on if a real need for it surfaces later
  (e.g. a very large repo where the enrichment pass's LSP round-trips
  dominate wall time) — not built speculatively here.
- **No `--log-level` flag.** Unlike `serve-mcp`, `acie scan` is a one-shot
  foreground command with no long-running server loop to tune. It fixes
  `logging.basicConfig(level=logging.WARNING)` in `cli.py`'s own `_scan()`
  handler (a CLI-entry-point-only global side effect — never inside
  `scan.run_scan()` itself, which stays a plain, repeatedly-callable
  function safe to unit-test without mutating global logging state) purely
  so the `_logger.warning(...)` calls already present in
  `bootstrap`/`pyright_process`/`lsp_enrichment` become visible on stderr
  during a scan, matching those modules' own existing severity choices —
  no new logging configuration surface is added.
- **No retrofit of `BootstrapCoordinator`'s own silent per-file failure
  tolerance.** `_run_indexing_pass`'s `on_job_done` deliberately never
  surfaces a failing file's exception (named shortcut, "one bad file can't
  wedge the repo in `INDEX_NOT_READY` forever") — correct for the daemon's
  readiness-flag purpose. `acie scan` is an interactive, foreground command
  where silent-on-failure is the wrong default, so design decision 3 adds
  its **own** per-file failure counting/logging inside `scan.py`, not by
  changing `BootstrapCoordinator`'s behavior — this is a deliberate,
  named divergence in only one of the two callers, not a project-wide
  policy change.
- **No sharing of the tiny `mark_job`/`check_job`-shaped write-queue-job
  closures already duplicated in spirit across `bootstrap.py`
  (`_mark_cross_file_migration_done`, `check_job` inside
  `_run_cross_file_migration_if_needed`) and this slice's own migration-flag
  write.** Each is a two-line closure over a single store call — promoting
  them into a shared free function would be extracting an abstraction for
  something used effectively once per call site (Coding Standard 14), not
  a real behavioral duplication worth a new cross-module name.

## Design decisions

1. **Promote `bootstrap.py`'s `_make_index_job` to public `make_index_job`,
   and make its job return `index_file()`'s `IndexResult` instead of
   discarding it.**

   ```python
   def make_index_job(path: str, source_text: str):
       def job(conn) -> IndexResult:
           return index_file(
               path=path, source_text=source_text,
               observed_at=datetime.now(timezone.utc).isoformat(),
               symbol_store=SymbolStore(conn=conn),
               relation_store=RelationStore(conn=conn),
               index_meta_store=IndexMetaStore(conn=conn),
           )
       return job
   ```

   The one existing call site (`_run_indexing_pass`'s
   `self._write_queue.submit(repo_id, _make_index_job(path, source_text))`)
   is updated to the new name only. Behavior-preserving for
   `BootstrapCoordinator`: `on_job_done`'s callback receives the `Future`
   but still never calls `.result()` on it (unchanged), so the now-non-`None`
   return value is inert there — `tests/daemon/test_bootstrap.py` must pass
   unmodified, proving this (matching D4's identical "existing test file
   passes unmodified" convention for `tools/confidence.py`).

2. **Promote `runtime.py`'s inline `walk_repo` closure to a public
   `walk_repo()` function in `dispatch.py`** (the module that already owns
   `_read_source_files`, its one real dependency):

   ```python
   # dispatch.py
   from acie.daemon import ignore

   def walk_repo(repo_root: str) -> Iterable[tuple[str, str]]:
       is_ignored = ignore.get_ignore_matcher(repo_root).matches
       return _read_source_files(repo_root, path_glob=None, is_ignored=is_ignored).items()
   ```

   `runtime.py`'s `create_daemon()` deletes its own local `def walk_repo`
   closure and imports `dispatch.walk_repo` in its place — one line changed
   at each of `create_daemon`'s two current uses (`BootstrapCoordinator`
   construction). Behavior-preserving: identical logic, only its home
   module changes. `tests/daemon/test_runtime.py::test_runtime_bootstrap_
   skips_files_the_repos_own_gitignore_excludes` (already exercises this
   closure end-to-end through `create_daemon()`) must pass unmodified as
   the regression proof, plus one new direct unit test of
   `dispatch.walk_repo` itself in `test_dispatch.py` (today only its two
   ingredients — `_read_source_files` and `ignore.get_ignore_matcher` — are
   tested individually, never their composition as one function).

3. **`src/acie/scan.py`, new module.** Single public entry point:

   ```python
   @dataclass(frozen=True)
   class ScanResult:
       repo_id: str
       repo_root: str
       files_scanned: int
       files_failed: int
       symbols_upserted: int
       relations_upserted: int
       relations_enriched: int
       elapsed_seconds: float

   class ScanError(Exception):
       """path is not inside a git repository."""

   def run_scan(repo_path: str, *, base_dir: str | None = None) -> ScanResult:
       ...
   ```

   `base_dir` mirrors `repo_id.resolve_index_db_path`'s own existing test
   seam (default `None` → real `~/.acie`; tests pass `tmp_path`) — kept
   symmetric with, not a new pattern next to, that existing signature.

   Body, in order:

   a. `repo_id = resolve_repo_id(repo_path)`; `repo_root =
      resolve_repo_root(repo_path)`; if either is `None`, raise
      `ScanError(f"{repo_path!r} is not inside a git repository")`.
      `db_path = resolve_index_db_path(repo_path, base_dir=base_dir)` reuses
      the exact existing `repo_id.py` helper for the `~/.acie/repos/
      <repo-id>/index.sqlite` path (parent directory creation included) —
      note this means `resolve_repo_id`'s underlying `git rev-parse
      --git-common-dir` subprocess runs twice (once directly here, once
      again inside `resolve_index_db_path`'s own call to
      `resolve_repo_state_dir`). This is a small, already-accepted class of
      redundancy in this codebase, not new to this slice —
      `runtime.py`'s own `dispatch()` docstring names an identical
      instance ("`dispatch.py`'s own separate `resolve_index_db_path` call
      ... is the one remaining resolution this doesn't collapse") as an
      accepted redundancy outside a narrower fix's scope; reusing
      `resolve_index_db_path` as-is here (rather than re-deriving its
      path-join logic a third time) is the same trade-off, made the same
      way.

   b. `write_queue = WriteQueue(db_path_for=lambda _repo_id: db_path)`.

   c. `files = list(dispatch.walk_repo(repo_root))`.

   d. Pass 1: submit `make_index_job(path, source_text)` for every
      `(path, source_text)` in `files` via `write_queue.submit(repo_id,
      ...)`; wait on every returned `Future` (`[f.result() for f in
      futures]` inside a small private `_run_pass` helper — see below) so
      pass 2 only starts once pass 1 has fully drained through the repo's
      single writer thread (same ordering guarantee `run_enrichment_pass`'s
      own `submitted[-1].result()` idiom already relies on — FIFO,
      one-job-at-a-time-per-repo, `write_queue.py`'s own documented
      invariant).

   e. Pass 2: identical submission over the same `files` list (repopulates
      whatever pass 1's arbitrary walk order left deferred/unresolved —
      finding 3). This pass's results are the ones `ScanResult` reports.

   f. `_run_pass(write_queue, repo_id, files) -> list[IndexResult | None]`:
      submits every job, then for each `Future` calls `.result()` inside a
      `try/except Exception`, logging a `WARNING` naming the file and
      appending `None` on failure rather than letting one bad file abort
      the whole pass (deliberate divergence from `BootstrapCoordinator`'s
      *silent* tolerance — see "What D5 does not do" — `acie scan` still
      tolerates one bad file, but now the user is told). `run_scan`'s
      caller-facing counts (`files_failed`, `symbols_upserted`,
      `relations_upserted`) are computed from pass 2's returned list only:
      `files_failed = sum(1 for r in pass2_results if r is None)`,
      `symbols_upserted = sum(r.symbols_upserted for r in pass2_results if
      r is not None)`, same shape for `relations_upserted`. Pass 1's own
      per-file failures are logged the same way but not separately
      counted in `ScanResult` — reporting both passes' failure counts
      would double-count the same one bad file appearing in both passes
      without adding real information (see "What D5 does not do").

   g. Mark the migration flag done (finding 3b), through the same
      `write_queue` (reusing its already-open writer connection for this
      repo, same discipline `_mark_cross_file_migration_done` already
      establishes):

      ```python
      def _mark_job(conn) -> None:
          IndexMetaStore(conn=conn).mark_cross_file_pass_done()
      write_queue.submit(repo_id, _mark_job).result()
      ```

   h. Run the enrichment pass (finding 4). Opens one **fresh**, short-lived
      `open_connection(db_path)` (matching `DAEMON.md`'s "Store lifecycle:
      fresh-per-call" pattern — this connection is never the write queue's
      own long-lived writer connection) purely to construct the
      `SymbolStore`/`RelationStore` instances `run_enrichment_pass` reads
      from; constructs one `PyrightProcessRegistry()` scoped to this call;
      calls `run_enrichment_pass(repo_root=repo_root, repo_id=repo_id,
      process_registry=process_registry, write_queue=write_queue,
      walk_repo=dispatch.walk_repo, symbol_store=symbol_store,
      relation_store=relation_store)` unmodified; closes the fresh
      connection in a `finally` immediately after (caller-owned, matching
      how every other caller of a fresh read-path store already closes its
      own connection — `run_enrichment_pass` itself never closes stores
      passed into it, matching the pattern `dispatch.py`'s nine tools
      already use).

   i. `finally`: `process_registry.close(timeout=_CLEANUP_TIMEOUT_SECONDS)`
      then `write_queue.close(timeout=_CLEANUP_TIMEOUT_SECONDS)` — always
      run, even if an earlier step raised, mirroring `runtime.py`'s own
      `on_shutdown()` bounded-drain discipline for exactly these two
      object types. `_CLEANUP_TIMEOUT_SECONDS = 10.0` is `scan.py`'s own
      independently-declared constant (same value as `runtime.py`'s
      `_SHUTDOWN_DRAIN_TIMEOUT_SECONDS`, same reasoning — "generous enough
      under ordinary conditions, bounded so a stuck job can't hang the
      process forever") — not imported from `runtime.py`, since that
      module's job is assembling a full `DaemonServer`, an unrelated and
      heavier object graph `scan.py` has no reason to depend on for one
      float. A one-line comment in `scan.py` cross-references
      `runtime.py`'s constant by name so the two don't silently drift
      without a future editor noticing the intended parity.

   j. Returns `ScanResult(repo_id=repo_id, repo_root=repo_root,
      files_scanned=len(files), files_failed=<from f>,
      symbols_upserted=<from f>, relations_upserted=<from f>,
      relations_enriched=len(resolved), elapsed_seconds=<wall clock via
      time.monotonic() spanning the whole call>)`.

4. **`acie scan [path]` CLI subcommand (`cli.py`).**

   ```python
   scan = subcommands.add_parser("scan")
   scan.add_argument("path", nargs="?", default=".")
   scan.add_argument("--json", action="store_true")
   ...
   if args.command == "scan":
       return _scan(path=args.path, as_json=args.json)
   ```

   `_scan(*, path: str, as_json: bool) -> int`: calls
   `logging.basicConfig(level=logging.WARNING)` first (see "What D5 does
   not do" — CLI-entry-point-only global side effect), then calls
   `scan.run_scan(path)` inside a `try/except ScanError as exc`. On
   `ScanError`: `print(f"error: {exc}", file=sys.stderr)`, return `1`. On
   success: if `as_json`, `print(json.dumps(dataclasses.asdict(result)))`;
   else a one-line human summary, e.g. `f"Scanned {result.repo_root}
   ({result.repo_id}): {result.files_scanned} files, {result.
   symbols_upserted} symbols, {result.relations_upserted} relations
   indexed, {result.relations_enriched} relations enriched via pyright,
   {result.files_failed} failed. ({result.elapsed_seconds:.1f}s)"` — both
   shapes matching `_daemon_status`'s existing plain-text-default /
   `--json`-opt-in precedent; return `0`.

   `path` defaults to `"."`, not `os.getcwd()` — `resolve_repo_id`/
   `resolve_repo_root`/`resolve_index_db_path` all shell out to `git -C
   <path> ...`, which already resolves a relative `.` against the
   process's own cwd; computing `os.getcwd()` in `cli.py` first and passing
   the absolute string would be redundant, not more correct.

## Files to touch

1. `src/acie/scan.py` (new) — `ScanResult`, `ScanError`, `run_scan`,
   `_run_pass` (design decision 3).
2. `src/acie/daemon/bootstrap.py` — rename `_make_index_job` →
   `make_index_job` (public), add `return index_file(...)` (design
   decision 1); update the one internal call site.
3. `src/acie/daemon/dispatch.py` — add public `walk_repo()` (design
   decision 2), add the `from acie.daemon import ignore` import (not
   previously needed in this file).
4. `src/acie/daemon/runtime.py` — delete the inline `walk_repo` closure in
   `create_daemon()`; import and use `dispatch.walk_repo` instead. No
   other line of `create_daemon()`/`dispatch()` changes.
5. `src/acie/cli.py` — add the `scan` subcommand and `_scan()` handler
   (design decision 4); add `import logging` (not previously imported
   here) and `import dataclasses`.
6. `tests/test_scan.py` (new) — the bulk of new coverage, against a real
   `WriteQueue`/`open_connection` over `tmp_path` sqlite files (no fakes
   needed for the indexing side — same reasoning as D4's `merge_policy`
   tests: no LSP/process dependency in the two-pass-indexing path itself).
   Named scenarios: a fresh repo with a cross-file call unresolved after
   one arbitrary-order pass but resolved after `run_scan`'s two passes
   (mirrors `test_bootstrap.py::test_register_resolves_a_cross_file_
   imported_call_regardless_of_walk_order`'s own scenario, proving D5's
   pipeline gets the same completeness as `BootstrapCoordinator`'s);
   re-running `run_scan` on an already-scanned, unchanged repo re-walks
   and re-reports full counts rather than a near-zero "already ready"
   no-op (the finding-2 regression this whole design avoids — assert via
   a spy/counting `walk_repo` fake that it was actually called both
   times, not skipped the second time); the migration flag
   (`IndexMetaStore.cross_file_pass_done()`) is `True` after `run_scan`
   returns, verified by opening a fresh connection to the same
   `db_path` and checking it directly (finding 3's regression proof); a
   file whose `index_file()` call raises is caught, logged, counted in
   `files_failed`, and does not abort indexing of the remaining files;
   `run_scan` against a `repo_path` with no git repo raises `ScanError`
   with a message naming the path, before any `WriteQueue`/
   `PyrightProcessRegistry` object is even constructed (cheap, no leaked
   background threads on the error path); enrichment integration using a
   `FakeRegistry`/`FakeClient` pair matching `test_lsp_enrichment.py`'s own
   existing fakes (constructed by having `run_scan` accept the same kind of
   injectable process-registry seam `run_enrichment_pass` itself already
   takes, OR — simpler, avoiding a new parameter on `run_scan`'s own public
   signature for a test-only need — monkeypatching `scan.PyrightProcessRegistry`
   the way `test_lsp_enrichment.py` monkeypatches `LspClient`'s
   construction path today) resolves at least one ambiguous site and
   reports it in `relations_enriched`; a repo where `PyrightProcessRegistry.
   ensure_process` returns `None` (no binary) completes with
   `relations_enriched == 0` and a non-error exit — the graceful-degradation
   path named in finding 4, proven at `run_scan`'s own boundary, not just
   trusted from D3's existing coverage.
7. `tests/test_cli.py` — a handful of new argv-level tests for the `scan`
   subcommand's wiring only (path/`--json` parsing, exit codes, the
   not-a-git-repo error path via `main(["scan", str(tmp_path)])`), thin and
   delegating real coverage to `test_scan.py`'s direct `run_scan()` tests —
   matching this file's existing depth for other subcommands (it tests
   argv wiring and process-level behavior, not `daemon.client`/`server`'s
   own internals a second time).
8. `tests/daemon/test_bootstrap.py` — no new test required; must pass
   unmodified after decision 1's rename (confirms `make_index_job`'s
   promotion and added return value is behavior-preserving for
   `BootstrapCoordinator`, exactly matching D4's own "existing test file
   passes unmodified" convention for `tools/confidence.py`).
9. `tests/daemon/test_runtime.py` — no new test required; must pass
   unmodified after decision 2's extraction (regression proof named in
   decision 2 above).
10. `tests/daemon/test_dispatch.py` — one new direct unit test of
    `dispatch.walk_repo()` (decision 2's own composition, currently
    untested as a unit — only its two ingredients are).
11. `DAEMON.md` — add a `acie scan [path]` row to the existing "CLI
    Surface" table, plus a new `## Scan CLI (Pre-Warming Convenience)`
    section placed after "LSP/pyright Enrichment Subprocess Lifecycle" and
    before "Auth Token Stance" (it depends on and follows conceptually
    from everything described above it — bootstrap indexing, write-queue
    concurrency, and LSP enrichment — matching this document's existing
    top-to-bottom dependency ordering). Describes: what problem it solves
    (pre-warming, never load-bearing), the two-pass-plus-enrichment
    pipeline, the migration-flag write that makes a later daemon touch see
    zero redundant work, and the `make_index_job`/`walk_repo` promotions
    this slice made to already-existing daemon modules — same one-section
    -per-module-or-feature style C2–C6/D1–D4 already established, no
    `ARCHITECTURE.md` change (no MCP surface, no schema change — the
    `index_meta` migration-flag column and every store method used here
    already exist).

## Workflow constraints carried into the spec

- **Implement with the `tdd` skill (red-green-refactor, one vertical slice
  at a time, testing at the public seams named above)** — restated from
  this document's top since it is a standing instruction for this slice,
  not implicit from precedent.
- omp configuration for this slice, per the standing decision recorded in
  memory `418f0111` for D5/D6: `gpt-5.6-luna:max` as implementer,
  `gpt-5.6-terra:medium` as prewalk + advisor, unchanged from D4's run.
- One slice per session, stop before commit for review (memory `9b020543`).
- Full suite must pass except the 3 known pre-existing failures (baseline
  reconfirmed this session: 716 passed, 3 failed).
- No new runtime dependencies — `argparse`, `logging`, `dataclasses`,
  `time`, and `json` are all stdlib and already used elsewhere in `cli.py`.
- Do not touch `runtime.py`'s `dispatch()`/`register_repo()` request path,
  `merge_policy.py`, `lsp_client.py`/`lsp_protocol.py`/`pyright_process.py`'s
  own internals, or `mcp_server.py` — daemon-request-path and MCP-surface
  changes are out of this slice's scope entirely, not just D6's.
- Do not implement D6 (daemon trigger wiring for automatic enrichment
  passes) or its inherited staleness-window follow-up (memory `87e7e80b`,
  explicitly handed to D6 by D4's spec — D5 does not touch it either).
- Do not silently retrofit `BootstrapCoordinator`'s own per-file failure
  tolerance to match `acie scan`'s new failure-visibility behavior, or vice
  versa — the two are deliberately allowed to differ (see "What D5 does not
  do").
- Standing dogfooding ground rule (wayfinder map `5d8fa498`'s Notes) still
  applies: live `mcp__acie__*` tools stay v0-pinned throughout
  implementation.

## Verification note

This spec was written against the actual current source this session:
`cli.py` (full read — argparse wiring, every existing subcommand handler,
confirming no `logging.basicConfig` call exists anywhere in the CLI today),
`pyproject.toml` (confirming `acie = "acie.cli:main"` entry point,
`basedpyright` as a mandatory, not optional, dependency), `indexer.py` (full
read — `index_file`'s exact signature and `IndexResult` fields),
`bootstrap.py` (full read — `BootstrapCoordinator.register`/`repo_ready`/
`_run_bootstrap`/`_maybe_schedule_cross_file_migration`/
`_run_cross_file_migration_if_needed`/`_run_indexing_pass`/
`_make_index_job`, confirming the two-pass rationale and the migration-flag
write path in full, not inferred from `DAEMON.md` prose alone),
`lsp_enrichment.py` (full read — `run_enrichment_pass`'s complete parameter
list and graceful-degradation branches, `_make_merge_job`),
`runtime.py` (full read — `create_daemon()`'s `walk_repo`/`db_path_for`
closures, `_resolve_repo`, `register_repo`, `dispatch()`, `on_shutdown()`
and its `_SHUTDOWN_DRAIN_TIMEOUT_SECONDS`), `write_queue.py` (full read —
confirmed `submit()`/`close()`'s exact semantics this design relies on:
single writer thread per repo key, FIFO ordering, bounded `close()`
draining), `pyright_process.py` (`PyrightProcessRegistry.__init__`/
`ensure_process`/`close` — confirmed zero daemon-lifetime coupling, safe to
construct standalone), `repo_id.py` (full read — `resolve_repo_id`/
`resolve_repo_root`/`resolve_repo_state_dir`/`resolve_index_db_path`'s
exact signatures and the `base_dir` test seam already established there),
`storage/connection.py` (`open_connection`'s WAL-mode pragmas, confirming
no special handling is needed for `scan.py`'s own fresh read-path
connection beyond what every other caller already does), `dispatch.py`
(`_read_source_files`'s exact signature, confirming `ignore` is not
currently imported there), `ignore.py` (`get_ignore_matcher`, confirming no
import-cycle risk from moving `walk_repo` into `dispatch.py`), `DAEMON.md`
in full (confirming no prior `acie scan` mention anywhere, and matching the
"CLI Surface" table's and "Resolved during implementation" paragraphs'
established conventions for decision 11's file-touch edits), and
`tests/test_cli.py`, `tests/daemon/test_bootstrap.py`,
`tests/daemon/test_lsp_enrichment.py`, `tests/daemon/test_dispatch.py`,
`tests/daemon/test_runtime.py` (each read in full or by targeted grep for
their existing test-naming conventions, fakes already available for reuse
— `FakeRegistry`/`FakeWriteQueue`/`FakeClient` in `test_lsp_enrichment.py`
— and the exact existing tests this spec names as required-unmodified
regression proofs).

No new live LSP/pyright smoke tests were written into this spec beyond what
design decision 3h/file 6 already name — D5 does not change pyright
process/protocol behavior at all, only assembles D1–D3's already-verified
pieces in a new (foreground, non-daemon) caller.
