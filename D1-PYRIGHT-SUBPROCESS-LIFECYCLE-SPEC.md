# Implementation Spec: v1 Slice D1 — pyright/basedpyright Subprocess Lifecycle

**Status: spec ready for implementation, not yet built.** Written for
hand-off to a different implementing agent ("omp"); review happens in a
fresh Claude session afterward, this project's usual review role. Do not
skip straight to coding without first reading this spec's "Context" and
"Design decisions" sections in full — D1 is the *first* slice of a brand
new capability (D), so unlike C6's spec it has no existing module
docstring/decision list to extend; this spec establishes that numbering
for D1's own module, starting at 1.

## Context

Capability C (architecture-level queries, wayfinder ticket `47d8cd0d`) is
now **fully complete**: C1-C6 all committed and pushed (commits `8183c78`,
`260caec`, `48cfec3`, `bf2a5ac`, `933e2d9`, `45f4698`). Per the locked v1
slice breakdown (SALTMDB memory `3627eece`, "Full v1 Slice Breakdown (A-D)
Recovered via Direct DB Read"), **Capability D — pyright/LSP background
enrichment — is next**, and was deliberately ordered last: "pure
opportunistic enrichment (never load-bearing) and the heaviest
infrastructure lift (subprocess lifecycle, JSON-RPC client)... doing it
last means A/B/C's simpler storage/query work is done first and D can
enrich an already-complete schema" (verbatim order rationale from that
memory).

Capability D's own two wayfinder tickets — `bfa054b4` ("What LSP
capabilities does pyright expose...") and `89be4cc1` ("How does the daemon
integrate pyright as an opportunistic enrichment layer over the tree-
sitter IR?") — are both already closed, and their resolutions are locked
in wayfinder map `5d8fa498` ("[ACIE] Wayfinder Map: v1 design spec",
context_id `wayfinder:acie:v1-design-spec`). The map's relevant locked
decisions this spec builds on:

- **Server choice**: `pyright`, single-server, run via the `basedpyright`
  PyPI package (ships the JS language server directly in its wheel — no
  hidden Node.js/npm runtime side-install the plain `pyright` PyPI wrapper
  carries). Both packages install an identically-named `pyright-langserver`
  binary (research memory `7d12f681`, §4).
- **No CLI configuration surface**: `pyright-langserver` takes no argv
  config beyond its transport flag (`--stdio` here) — everything else
  (workspace root, Python interpreter, analysis mode) goes through the LSP
  `initialize` request or `pyrightconfig.json`, never argv (research memory
  `7d12f681`, §2, sourced directly from pyright maintainer Eric Traut and
  `languageServerBase.ts`).
- **One subprocess per repo**, opportunistic and never load-bearing: "tool
  calls never wait on pyright, so there's no per-call timeout/fallback path
  to design at all" (map decision on ticket `89be4cc1`).
- **Idle-timeout teardown, diverging from `WriteQueue`'s own deliberate
  "no teardown" shortcut** (`DAEMON.md` "Write-Queue Concurrency"), because
  a pyright subprocess's real memory cost (~3GB on large repos, per
  research memory `7d12f681`'s cited numpy/pandas full-check figures) makes
  the same "unbounded, never torn down" shortcut a bad fit here — "respawns
  lazily" once torn down.
- **Failure always silently degrades to the caller** — matches
  `ARCHITECTURE.md`'s "Determinism is the north star": "If no LSP server
  is available, ACIE still works, just with less precision."

The full D1-D6 slice breakdown (memory `3627eece`) scopes **D1** narrowly:
*"basedpyright subprocess lifecycle (lazy per-repo spawn, idle-timeout
teardown, ~3GB memory cost)"* — nothing else. This spec covers exactly
that. It deliberately does **not** cover:

- **D2** (LSP JSON-RPC stdio client, the `initialize`/`initialized`
  handshake, evaluating `multilspy` vs. hand-rolled framing) — D1 spawns
  the process and wires its pipes; nothing in D1 ever writes to `stdin` or
  reads `stdout`.
- **D3** (the actual background enrichment pass, `WriteQueue`-routed writes
  tagged `confidence=INFERRED`) or **D4** (the AMBIGUOUS/unresolved-only
  merge rule).
- **D5** (`acie scan [path]` CLI command).
- **D6** (daemon trigger wiring — `runtime.py`'s `register_repo()` calling
  into this registry the way it already does for `WatcherRegistry`).

D1 is built and tested **standalone**, independently importable and
independently unit-testable with zero real `basedpyright` install required
— the same "prove the isolated piece works before wiring it into the
daemon" shape slice C1 (the dotted-name index builder) used before C2
wired it into the `architecture` tool. **Do not wire this registry into
`runtime.py`/`create_daemon()` in this pass** — that is D6's job, later,
once D2/D3 exist and there's something real for the wiring to trigger.

## Design decisions

1. **New module: `src/acie/daemon/pyright_process.py`.** Narrow,
   mechanism-named, same convention as `watcher.py` (owns the OS
   filesystem watch) and `write_queue.py` (owns per-repo write
   concurrency) — this module owns exactly one thing, a repo's pyright
   subprocess lifecycle. Deliberately *not* named `enrichment.py`: D3 will
   need that name for the actual enrichment pass (the piece that reads
   LSP responses and writes `INFERRED` facts through `WriteQueue`), which
   is a distinct concern from spawning/reaping the subprocess itself.
2. **Two public types**: `PyrightProcess` (a thin wrapper around one live
   `subprocess.Popen`, holding `repo_root`, `binary_path`, `version:
   str | None`, and an `is_alive` property delegating to
   `popen.poll() is None`) and `PyrightProcessRegistry` (owns lazy
   per-`repo_root` creation, idle-timeout teardown, and an explicit,
   shutdown-ready `close()`).
3. **Keyed by `repo_root`, not `repo_id`** — same rationale
   `WatcherRegistry` already established (`watcher.py`'s own module
   docstring, decision 10 follow-up): pyright's `initialize` request names
   a concrete workspace root (`rootUri`/`workspaceFolders`), a filesystem
   concept, not a repo-wide identity. Two genuinely distinct worktrees of
   one repo (sharing one `repo_id`) legitimately need two separate pyright
   subprocesses, each rooted at its own directory; a symlink and its
   realpath'd twin (one worktree, two spellings) still collapse to one,
   because `resolve_repo_root` is already canonical before it reaches this
   registry.
4. **Binary discovery**: `_default_locate_binary() -> str | None` returns
   `shutil.which("pyright-langserver")`. Deliberately generic — this looks
   for whichever package provides that binary name on `PATH` (`basedpyright`
   is what ACIE's own `pyproject.toml` will offer, per decision 10 below,
   but a user with a pre-existing plain `pyright` install still gets a
   working `pyright-langserver`, matching the "genuinely novel provenance"
   principle: record what was actually observed, never assume). Injectable
   via `PyrightProcessRegistry(locate_binary=...)`, mirroring
   `WriteQueue`'s injectable `db_path_for` — tests never depend on a real
   `basedpyright` install.
5. **Spawn command**: `[binary_path, "--stdio"]`, `cwd=repo_root`,
   `stdin=PIPE`, `stdout=PIPE`, **`stderr=DEVNULL`**. Injectable via
   `PyrightProcessRegistry(spawn=...)`, same seam shape as
   `locate_binary`. `stderr=DEVNULL`, not `PIPE`, is deliberate: D1 never
   drains any pipe (D2 hasn't been built yet to consume `stdout`, and
   nothing in D1 ever writes to `stdin`), and an undrained `PIPE` risks
   the child blocking once its OS pipe buffer fills from log spam —
   `DEVNULL` removes that risk entirely rather than accepting it and hoping
   idle-teardown reaps the process before it matters. **D2 must actively
   drain `stdout` (and decide independently whether `stderr` needs to
   become a real pipe for its own JSON-RPC debugging) as soon as it starts
   writing to `stdin`** — flagged here as a constraint D2 inherits, not
   solved in this slice.
6. **Version probe is best-effort, not guaranteed.** At spawn time,
   attempt `subprocess.run([binary_path, "--version"], capture_output=True,
   text=True, timeout=5.0)` in a `try/except Exception`, storing its
   stripped stdout as `PyrightProcess.version` on success, `None` on any
   failure (nonzero exit, timeout, unsupported flag, anything). **This is
   deliberately not verified against real `pyright-langserver`/
   `basedpyright` behavior** — research memory `7d12f681` confirms `pyright`
   maintainer Eric Traut states the *langserver* binary has "no CLI surface
   beyond LSP", which casts real doubt on whether `--version` against
   `pyright-langserver` itself (as opposed to a separate `pyright`/
   `basedpyright` main-CLI entry point) does anything useful at all. Rather
   than guess an unverified binary-name convention (e.g. stripping a
   `-langserver` suffix) into this spec, D1 ships the honest best-effort
   probe against `binary_path` itself and accepts `version` may come back
   `None` in practice. **Named follow-up, not solved here**: D3 (the slice
   that actually needs `provenance.version` for real writes) must verify
   empirically what `--version` (against which binary) actually returns
   before depending on it — do not assume this probe already answered that
   question.
7. **Idle-timeout teardown via `threading.Timer`, matching the debounce-
   timer pattern this codebase already uses** (`watcher.py`'s
   `_DebouncedEventHandler`) rather than a polling background thread: each
   registry entry holds one `threading.Timer(idle_timeout_seconds,
   self._on_idle_timeout, args=(repo_root,))`, cancelled and restarted by
   `touch()`. Default `idle_timeout_seconds=900.0` (15 minutes) —
   deliberately conservative: research memory `7d12f681`'s cited full-repo
   warm-up costs (numpy ≈71s, pandas ≈144s, up to ~1680s on very large
   proprietary monorepos) make a short timeout actively harmful (thrashing
   respawn-plus-rewarm during a normal active editing session), while 15
   minutes still reclaims the ~3GB footprint well within a single idle
   coffee break. Injectable constructor parameter, same as every other
   tunable here — tests use a small value (e.g. `0.05`) to keep teardown
   tests fast.
8. **`ensure_process(repo_root) -> PyrightProcess | None` always refreshes
   the idle deadline as a side effect** — a caller fetching the handle to
   actually use it is itself activity, not just an explicit `touch()`
   call. `touch(repo_root) -> None` is a separate, lighter-weight method
   for a caller (D2/D3, later) that already holds a `PyrightProcess`
   reference and wants to signal continued liveness without re-resolving
   it through the registry each time.
9. **Crash detection is lazy, on the next `ensure_process()` call — no
   background health-check loop.** If an entry exists but
   `process.is_alive` is now `False` (the child exited or was killed
   outside this registry's own teardown path), `ensure_process` cancels
   that entry's idle timer, drops it, and falls through to spawn a fresh
   one transparently — no exception, no special "crashed" state exposed
   to the caller, extending the same "failure silently degrades" principle
   the map's `89be4cc1` resolution already applies to the enrichment path
   itself down to this foundational layer.
10. **Binary-absent handling**: `ensure_process` returns `None` (never
    raises) when `locate_binary()` returns `None`. Logged **once** per
    registry instance (a single `self._warned_missing_binary` flag,
    checked-then-set before logging), not once per call — a daemon
    fielding many requests against a repo with no LSP server installed
    must not spam its log once per request.
11. **Creation concurrency: double-checked locking with a per-`repo_root`
    creation lock**, reusing `WriteQueue._worker_for`'s exact proven shape
    (`write_queue.py`) rather than inventing a simpler-but-riskier
    alternative — "reuse over reinvention" applies to *patterns* already
    battle-tested in this codebase, not just to code. A single global lock
    held across the whole spawn (which can itself take a real, if usually
    small, amount of wall-clock time) would let one repo's first-ever
    `ensure_process` call stall an unrelated repo's own first call; the
    per-key creation lock (held only during that key's own creation)
    avoids that exactly the way `WriteQueue` already does.
12. **Termination: plain bounded `Popen.terminate()` → `Popen.wait(timeout=
    ...)` → `Popen.kill()` on timeout — no helper-thread indirection.**
    This is a deliberate, simpler contrast with `RepoWatcher.stop()`
    (`watcher.py`), which *needs* a helper thread specifically because
    `watchdog`'s own `Observer.stop()` has no timeout parameter of its own
    and can itself block indefinitely (verified against watchdog 6.0.0's
    source, per that method's docstring). `subprocess.Popen.wait(timeout=
    ...)` is natively bounded by the stdlib — there is nothing here that
    can hang past the timeout the way an opaque third-party `stop()` call
    could, so the extra indirection watcher.py needed would be
    unjustified complexity here, not a missing safeguard.
    `terminate_timeout_seconds` (default `5.0`) is injectable, same
    pattern as every other tunable. A process already dead
    (`popen.poll() is not None`) when termination is attempted is a silent
    no-op, not an error.
13. **`close(timeout=None)` mirrors `WatcherRegistry.close()`'s already-
    fixed shared-deadline-across-N-resources shape exactly** (a deadline
    computed once, re-diffed against the clock before each process's own
    terminate call) — this bug (giving each of N resources its own fresh
    budget, making total shutdown time N × timeout instead of timeout) was
    found and fixed once already in this codebase for both
    `WatcherRegistry` and `WriteQueue` (both cite the same 2026-09-02 codex
    review); D1 builds it correctly the first time rather than needing an
    identical follow-up fix later. Every pending idle timer is cancelled
    *before* any terminate call begins (avoids a timer firing mid-`close()`
    and redundantly double-terminating an already-being-closed process).
    Sets a `self._closed` `threading.Event()`; `ensure_process()` returns
    `None` immediately once closed, without attempting a spawn — consistent
    with this registry's existing "return `None` on any non-fatal
    failure" vocabulary (no new exception type needed, unlike
    `WriteQueue.submit()`'s Future-based rejection, since this registry's
    contract was already `Optional`-returning from the start).
14. **No `ARCHITECTURE.md` change in this slice.** C1's precedent added a
    new paragraph to `ARCHITECTURE.md`'s "MCP Tool Surface" section because
    C1 was foundational work toward an eventual MCP-facing tool. D1 exposes
    no MCP surface at all — it's purely internal daemon infrastructure,
    unreachable by any tool until D6 wires it in. `DAEMON.md` (decision 15
    below) is the correct home, matching where `WriteQueue`/
    `WatcherRegistry`'s own equivalent lifecycle documentation already
    lives ("Write-Queue Concurrency", "Incremental Indexing Wiring").
15. **`DAEMON.md` gets one new section**, `## LSP/pyright Enrichment
    Subprocess Lifecycle`, inserted after "Incremental Indexing Wiring" and
    before "Auth Token Stance" — both existing neighbors are per-repo
    background daemon subsystems, the same category this belongs to.
    Content: a paragraph describing D1's scope (this spec's decisions 1-13,
    condensed, in the same "Resolved during implementation" style
    `DAEMON.md` already uses for `WatcherRegistry`/`BootstrapCoordinator`),
    plus one explicit sentence distinguishing this registry's idle-timeout
    teardown from the *different*, still-deliberately-unbuilt shortcut
    named in "Write-Queue Concurrency"'s own `Shortcut` callout (`WriteQueue`
    threads still have no teardown at all — that shortcut is untouched by
    this slice) — worth calling out explicitly so a future reader doesn't
    conflate the two.
16. **`pyproject.toml` gains a new optional-dependency extra**:
    `[project.optional-dependencies]` / `lsp = ["basedpyright"]`. Not a
    core dependency — `ARCHITECTURE.md`'s "Determinism is the north star"
    principle is explicit that the tree-sitter baseline "must work
    completely standalone with zero optional dependencies," and LSP output
    is "always an opportunistic... enrichment layer... never a correctness
    dependency." An extra, not a hard `dependencies` entry, is what
    actually encodes that: `uv sync` alone (no `--extra lsp`) must still
    give a fully working ACIE with `PyrightProcessRegistry.ensure_process`
    gracefully returning `None` everywhere (decision 10). **No version
    floor pinned** — `shortcut: no minimum basedpyright version verified
    against this integration; add a floor if a real incompatibility with
    an old install surfaces.** Do **not** add `basedpyright` to the `dev`
    dependency group or to CI/test requirements — every test in this slice
    must pass with zero real `basedpyright` install present, exercised
    entirely through the injectable `locate_binary`/`spawn` seams
    (decisions 4-5).

## Public surface

```python
# src/acie/daemon/pyright_process.py

class PyrightProcess:
    repo_root: str
    binary_path: str
    version: str | None

    @property
    def is_alive(self) -> bool: ...

class PyrightProcessRegistry:
    def __init__(
        self, *,
        locate_binary: Callable[[], str | None] = _default_locate_binary,
        spawn: Callable[[str, str], subprocess.Popen] = _default_spawn,
        idle_timeout_seconds: float = 900.0,
        terminate_timeout_seconds: float = 5.0,
    ) -> None: ...

    def ensure_process(self, repo_root: str) -> PyrightProcess | None: ...
    def touch(self, repo_root: str) -> None: ...
    def close(self, timeout: float | None = None) -> None: ...
```

No daemon wiring, no MCP-facing change, no new error codes, no
`dispatch.py`/`mcp_server.py`/`runtime.py` changes — this slice is a
standalone, independently-testable module only.

## Files to touch

1. **`src/acie/daemon/pyright_process.py`** (new) — `PyrightProcess`,
   `PyrightProcessRegistry`, `_default_locate_binary`, `_default_spawn`,
   per decisions 1-13. Follow this codebase's existing module-docstring
   convention (see `watcher.py`'s top-of-file docstring style, citing
   which decisions this file implements and why) — this module's own
   docstring should number its own decisions 1-13 as listed above, for
   D2/D3 to extend later the way C2-C6 extended `architecture.py`'s.
2. **`tests/daemon/test_pyright_process.py`** (new) — see test list below.
   Use `sys.executable` running a short inline script (e.g.
   `[sys.executable, "-c", "import time; time.sleep(3600)"]`) as the
   injected `spawn` in every test — a real, controllable, always-available
   long-lived child process that responds normally to `SIGTERM`/`SIGKILL`,
   with **zero dependency on a real `basedpyright`/`pyright-langserver`
   install**. Follow `tests/daemon/test_watcher.py`/
   `tests/daemon/test_write_queue.py`'s existing helper/fixture
   conventions.
3. **`pyproject.toml`** — new `[project.optional-dependencies]` table,
   `lsp = ["basedpyright"]`, per decision 16.
4. **`DAEMON.md`** — new `## LSP/pyright Enrichment Subprocess Lifecycle`
   section, per decision 15.

## Test list (new, in `tests/daemon/test_pyright_process.py`)

- `test_ensure_process_lazily_spawns_a_process_for_a_new_repo_root`
- `test_ensure_process_returns_the_same_process_on_a_second_call_for_the_same_repo_root`
- `test_ensure_process_spawns_independent_processes_for_different_repo_roots`
- `test_ensure_process_returns_none_when_the_binary_is_not_located` (injected
  `locate_binary` returns `None`; asserts no exception)
- `test_missing_binary_is_logged_only_once_across_repeated_ensure_process_calls`
  (caplog, `locate_binary` returns `None`, call `ensure_process` 3x, assert
  exactly one warning)
- `test_touch_extends_the_idle_deadline_past_the_original_timeout` (short
  injected `idle_timeout_seconds`; repeated `touch()` calls keep the
  process alive well past what the original single deadline would have
  allowed)
- `test_an_untouched_process_is_torn_down_after_the_idle_timeout` (short
  `idle_timeout_seconds`, no `touch()`; assert `is_alive` becomes `False`
  and the underlying `Popen` actually exited)
- `test_ensure_process_after_idle_teardown_respawns_a_fresh_process` (assert
  the injected `spawn` was called a second time, and the returned
  `PyrightProcess` is a distinct object from the first)
- `test_a_crashed_process_is_detected_and_transparently_respawned` (kill the
  underlying `Popen` directly, bypassing the registry, then call
  `ensure_process` again; assert a fresh process comes back, no exception)
- `test_close_terminates_every_live_process` (register 2+ repo roots,
  `close()`, assert every one's `Popen` has exited)
- `test_close_is_bounded_by_one_shared_deadline_across_multiple_processes`
  (same shared-deadline regression shape as
  `WatcherRegistry`'s own close-timeout test — a slow/stuck terminate for
  one process must not multiply the total `close()` budget by the number
  of registered processes)
- `test_close_cancels_pending_idle_timers_so_none_fire_after_close` (assert
  no idle-timeout teardown attempt runs against an already-closed registry's
  now-stale entries)
- `test_ensure_process_returns_none_after_close` (no new spawn attempted
  post-`close()`)
- `test_default_locate_binary_uses_shutil_which_for_pyright_langserver`
  (monkeypatch `shutil.which`, assert it's called with exactly
  `"pyright-langserver"`)
- `test_version_probe_failure_does_not_prevent_spawn` (injected `spawn`
  succeeds; the `--version` probe subprocess call raises/times out; assert
  `PyrightProcess.version is None` and the process is still usable)

## Workflow reminders for the implementing agent

- Repo TDD convention: write the failing tests first, then implement — see
  this project's `tdd` skill and memory `9b020543` ("v1 Implementation
  Workflow: One Slice Per Session, Stop Before Commit for Review").
- **This is one slice.** Implement D1, run the **full** test suite
  (`cd /home/zbalint/workspace/ACIE && .venv/bin/pytest`), not just the new
  tests, and **stop before committing** — leave the diff uncommitted for
  review in a fresh session. Do not `git commit` or push.
- Expect the pre-existing daemon/MCP-transport election-port flake (2
  failures, tracked since slice A1, confirmed unrelated — see memory
  `c125ccdf`) and nothing else. Any *other* new failure must be
  root-caused and fixed, not waved through as "probably that flake."
- **Do not wire this registry into `runtime.py`/`create_daemon()`, and do
  not start D2/D3/D5/D6 in this same pass.** D1 finishes once its own
  module and tests are green and standalone — the daemon has zero live
  pyright processes running until D6 lands.
- Do not add `basedpyright` (or any LSP-related package) to the `dev`
  dependency group, to CI, or to any test's actual runtime requirements —
  every test must pass with the real binary absent, per decision 16 and
  the test list's exclusive use of a `sys.executable`-based fake `spawn`.
- Standing dogfooding ground rule (map `5d8fa498`'s Notes) still applies
  unchanged: the live `mcp__acie__*` MCP tools stay v0-pinned throughout
  (dev-repo edits are inert to them until the eventual end-of-v1 cutover,
  which still hasn't happened — D1 doesn't change that). Fine to use them
  as the working code-intelligence tool while implementing; spot-check any
  result against ground truth before it drives a consequential decision.

## Acceptance criteria

- All new tests above pass; full existing suite still passes with only the
  known pre-existing flake.
- `PyrightProcessRegistry` is fully standalone: importable and testable
  with zero real `basedpyright` install, zero daemon/runtime wiring, zero
  MCP-facing change.
- Binary-absent, crash, and idle-timeout paths all degrade silently
  (`None`/transparent respawn), never raise to the caller.
- `close()` is bounded by one shared deadline across every registered
  process, matching `WatcherRegistry.close()`'s already-fixed shape —
  not a fresh budget per process.
- `pyproject.toml`'s new `lsp` extra installs `basedpyright` without
  touching core `dependencies`.
- `DAEMON.md` gains the new "LSP/pyright Enrichment Subprocess Lifecycle"
  section; `ARCHITECTURE.md` is untouched (decision 14).
- Diff left uncommitted, ready for review.
