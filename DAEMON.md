# ACIE Daemon & MCP-Server Design Specification

This document is the detailed design for ACIE's daemon and MCP-server layer: process lifecycle, IPC transport, the RPC envelope and dispatch, repo/session identity, bootstrap indexing, write-queue concurrency, the auth-token stance, shutdown semantics, the CLI surface, and the dev/prod deployment split. It assumes and never re-litigates the principles already locked in `ARCHITECTURE.md` — exactly one ACIE daemon per computer, the on-disk state layout (`~/.acie/repos/<repo-id>/{index.sqlite, manifest.json, cache/}`), the 4-tier incremental-indexing precedence, the `acie notify-hook` integration contract, and Python as the implementation language.

**Spec only.** Nothing here has been implemented yet; it is the locked design that a future TDD slice-based implementation phase builds against, mirroring the Slice 1–8 process that built ACIE's 8 pure-function tools. Every decision below was checked against the separate, unrelated SALTMDB project's live installed source (`~/.mcp/SALTMDB`) as battle-tested prior art — never as a code or runtime dependency, per `ARCHITECTURE.md`'s design principles.

## Process Lifecycle: Auto-Spawn on Demand

An agent host spawns `acie serve-mcp`; that process finds or spawns the one daemon for this machine itself, with no separate manual "start the daemon first" step required.

- **Discovery file**: `~/.acie/daemon.json` holds `{service_port, auth_token, daemon_pid}`. Written atomically (`O_CREAT|O_EXCL`, mode `0600`, to a PID-qualified temp path, then `os.replace`) so a reader never observes a partially-written file.
- **Client probe**: `acie serve-mcp` reads the discovery file and sends an authenticated ping/identify RPC. Success reuses the running daemon; a missing file, unreadable file, or failed ping triggers a spawn.
- **Spawn mechanism**: a detached background subprocess — `subprocess.Popen([sys.executable, "-m", "acie.daemon.server"], start_new_session=True, ...)`, stdout/stderr redirected to `~/.acie/daemon.log` — the same internal spawn target `acie daemon start` uses (see "CLI Surface").
- **Race safety**: a fixed election port is bound exclusively as an OS-level mutex during daemon startup. Two `acie serve-mcp` processes racing to spawn simultaneously both attempt the bind; only one wins and proceeds to full startup, so no double-spawn is possible. (ACIE needs only one fixed election port, unlike SALTMDB's per-database hash-derived ports — ACIE has exactly one daemon per machine, not one per database.)
- **Retry**: the client polls the discovery file with bounded retry and periodic re-spawn while waiting — never a single one-shot timing race against daemon startup.

**Resolved during implementation** (Slice 5, `src/acie/daemon/server.py` + `src/acie/daemon/discovery.py`): `DaemonServer.start()` binds the election port (when given one) *before* binding the real service socket — a bind failure raises `AnotherDaemonRunningError` and startup goes no further, leaving any already-running daemon's election socket, service socket, and discovery file untouched. The election port is injected (`election_port: int | None`), not hardcoded to one fixed module-level constant, so tests exercise the exact race-safety contract (two `DaemonServer`s racing for the same port; the loser raises, the winner keeps serving) against ephemeral ports instead of a real magic number that would collide with a live dev-machine daemon or other tests. The daemon-server slice that wires this into `acie daemon start`/the auto-spawn path (a later slice, per "CLI Surface" below) is where the actual fixed election-port value gets chosen and threaded through. `discovery.py` implements the atomic write exactly as specified (`O_CREAT|O_EXCL`, mode `0600`, PID-qualified temp path, then `os.replace`); `DaemonServer.start()` writes it using the real bound port once `listen()` has succeeded, and `shutdown()` deletes it.

## IPC Transport

Loopback TCP, `127.0.0.1:<port>` — the port read from the discovery file each time, not a fixed well-known port. Chosen over a Unix domain socket specifically to be cross-platform from day one: `ARCHITECTURE.md` already names Windows (`ReadDirectoryChangesW`) as an intended filesystem-watcher platform, so paying transport-portability cost now avoids a migration later — the same reason SALTMDB made this choice for its own daemon.

**Wire framing**: a 4-byte big-endian length prefix (`struct.pack(">I", ...)`) followed by a UTF-8 JSON body. A `DAEMON_RPC_MAX_MESSAGE_BYTES`-style cap applies on both send and receive, rejecting an oversized or malformed frame outright rather than attempting to consume it. The exact byte-cap value is deferred to implementation (see "Deferred to Implementation").

## Request/Response Envelope

**Connection model: connect-per-call.** Every RPC — including the repo-identity handshake — connects, sends one length-prefixed JSON frame, reads one response, and closes. There is no persistent multiplexed connection. (This corrects an initial draft assumption during design: SALTMDB's own tool-call RPCs are themselves connect-per-call — its persistent `SessionConnection` exists only for hello/goodbye session bookkeeping, never for tool calls. ACIE has no identified need for that mechanism at all.)

**Request**:

```
{"id": <uuid4>, "token": <str|null>, "method": <str>, "repo_path": <str>, "params": <dict>}
```

**Success response**: `{"id": <same uuid4>, "ok": true, "result": <the tool's own return shape, passed through directly>}`

**Error response**: `{"id": <same uuid4>, "ok": false, "error": {"code": <str>, "message": <str>}}`

**Two-layer error codes**:

- *Transport-level* (the RPC layer itself, before any tool runs): `UNKNOWN_METHOD`, `MALFORMED_REQUEST`, `INTERNAL_ERROR` now; `AUTH_FAILED` and `DAEMON_SHUTTING_DOWN` reserved for the mechanisms described in "Auth Token Stance" and "Shutdown Semantics" respectively. (A `CALLER_SESSION_INVALID` code was considered and skipped — the connect-per-call model has no persistent session for it to describe.)
- *Tool-level* (the pure function itself raised): the existing `AcieToolError` subclass codes from `src/acie/errors.py`, plus `INDEX_NOT_READY` (see "Bootstrap Indexing").

`token` is sent on every request, not just a handshake — nullable, and its enforcement is fully specified in "Auth Token Stance" below (spoiler: none, in v0). There is no protocol-version field in the envelope.

## RPC Dispatch

The daemon wires the 10 already-implemented pure functions (`src/acie/tools/*.py`) to incoming requests via a `DISPATCH_TABLE: dict[str, Callable]`, keyed verbatim on the 10 locked tool names — `method` equals the key exactly, with no namespacing:

```
find_symbol | get_definition | find_references | list_imports
structural_search | graph | impact_analysis | explain | affected_tests | architecture
```

Adapted from SALTMDB's `daemon/dispatch.py`, simplified for ACIE's shape:

- **No per-tool kwarg-coercion wrapper** — ACIE's tool functions already validate their own inputs.
- **No mutating/coordinator-submit split** — all 10 tools are read-only (per `ARCHITECTURE.md`'s MCP tool-annotation rule), so none of them touch the write queue described below.
- **`repo_path` is a new top-level envelope field**, sibling to `id`/`token`/`method`/`params`. The connect-per-call model has no session to carry repo identity forward implicitly, so dispatch resolves `repo_path` to store instances on every call.

**Store lifecycle: fresh-per-call.** New `SymbolStore`/`RelationStore`/`IndexMetaStore` instances (and their SQLite connections) are constructed on every RPC and never cached. This is the deliberately simpler default: `sqlite3` connections aren't thread-safe by default, and request threads run genuinely concurrently (see "Write-Queue Concurrency"), so a shared cache would need its own concurrency story with no measured need yet to justify that complexity. Revisit only if profiling demands it.

**`structural_search`'s disk-I/O seam**: the tool's `files: dict[str, str]` parameter is filled by dispatch itself — reading matching files off disk, scoped by `path_glob` against the repo root resolved from `repo_path`, before calling the pure function. This is the exact seam the tool's own docstring already flags as deferred to daemon wiring.

**Error wrapping**: a single shared wrapper inside `dispatch_tool` catches `AcieToolError` subclasses, mapping `.code` and `str(exc)` into the tool-level error envelope (matching `errors.py`'s own stated design intent). Anything else maps to transport-level `INTERNAL_ERROR`.

**`INDEX_NOT_READY` short-circuit**: dispatch checks a per-repo readiness flag *before* looking up `DISPATCH_TABLE` or constructing any store. An unready repo never opens a store connection at all. That short-circuit response reports `index_generation: 0`, matching `IndexMetaStore`'s own schema default rather than inventing a new sentinel value.

## Repo & Session Identity

The daemon learns which repo (and therefore which `index.sqlite`) a session's tool calls target from **ambient `cwd`**, not an explicit config flag.

`acie serve-mcp` captures `os.path.realpath(os.getcwd())` once at process startup — one MCP-server process serves exactly one agent session end-to-end, so this is safe as a per-process value, never shared or mutated across sessions. It resolves that path to a repo-id via the already-implemented `repo_id.py` / `resolve_index_db_path` (the same logic already validated for git worktrees), and reports the resolved repo-id (or the raw path — dispatch resolves it either way via `repo_path` on every request; see "RPC Dispatch") to the daemon.

This reverses an earlier draft answer during design (an explicit `--repo-path` config flag) once SALTMDB's actual production-proven `identity.py` / `SessionConnection.open()` approach was checked directly against live source rather than assumed.

## Bootstrap Indexing & `INDEX_NOT_READY`

When the daemon registers a repo it hasn't seen before (no `index.sqlite`, or a `manifest.json` indicating an incomplete prior bootstrap), it kicks off a full walk-and-index pass in the background — via the same per-repo write queue as any other reindex, described next — rather than blocking the caller.

Every tool call touching that repo returns the already-named `INDEX_NOT_READY` structured error until the bootstrap pass completes; the calling agent is expected to retry. A blocking first call would leave `INDEX_NOT_READY` a dead code path with nothing that could ever trigger it, and a multi-second blocking call is a worse agent-facing experience than a clear, retryable "not ready yet" signal.

**Resolved during implementation** (Slice 4, `src/acie/daemon/bootstrap.py`): `BootstrapCoordinator` is the real `repo_ready` implementation behind dispatch.py's seam. Readiness is a daemon-process-lifetime concept -- a repo already holding an `index.sqlite` from any prior run is trusted ready immediately (tiers 1-3 now keep it current after that; see "Incremental Indexing Wiring" below), so only a first-ever bootstrap needs a walk; an unseen repo's `register()` call walks it and submits one write-queue job per file, becoming ready the instant the last file's job completes. This surfaced and fixed a genuine race: naively trusting `os.path.exists(db_path)` for readiness is wrong even for the coordinator's own in-flight bootstrap, because opening the write-queue's per-repo connection creates that file on disk immediately, well before its first write commits -- a concurrent `repo_ready()` call could otherwise observe a just-created, still-empty index as "ready". Fixed by having `repo_ready()` trust the in-memory `_in_progress` flag over disk state for any repo_key this coordinator has itself started bootstrapping.

## Write-Queue Concurrency

**Per-repo write-queue isolation.** The daemon runs one dedicated writer thread — not an asyncio task — plus one FIFO queue, *per repo*, not a single global thread/queue for the whole daemon. Each thread owns its own exclusive SQLite connection to that repo's `index.sqlite`, opened once at thread creation. This matches the storage layer's existing one-file-per-repo isolation (`ARCHITECTURE.md`'s on-disk layout) and prevents one repo's heavy reindex from starving another repo's queries — with per-repo isolation, an unrelated repo's queries never wait behind another repo's write queue at all.

Request-handling threads never touch a SQLite connection directly: they enqueue a transaction closure onto the target repo's queue and block on a `concurrent.futures.Future` for the result. This is safe because incoming tool-call dispatch genuinely runs across multiple threads — confirmed against the installed `mcp==2.1.1` package, whose synchronous tool-handler path uses `anyio.to_thread.run_sync` (worker threads, not the event loop) — exactly the situation a dedicated writer thread + queue is designed to serialize.

`index_file()` (`src/acie/indexer.py`) is already the natural per-file unit of write work: bootstrap indexing already produces one write-queue item per file, not one per whole-repo job, so no batching change is needed to make this per-repo model work.

**Resolved during implementation** (Slice 4, `src/acie/daemon/bootstrap.py`): a write-queue job needs `SymbolStore`/`RelationStore`/`IndexMetaStore` instances bound to *that thread's own connection*, not a second `sqlite3.connect()` to the same file from inside the job -- which would defeat "opened once at thread creation" above. All three store classes now accept an optional keyword-only `conn: sqlite3.Connection` that, when given, is reused as-is instead of the class opening its own; every existing `db_path`-only caller (dispatch.py's fresh-per-call stores included) is unaffected.

- **Creation**: lazy, on-demand. A repo's first submitted write spins up that repo's writer thread and queue; nothing is pre-created for repos the daemon hasn't seen a write for yet.
- **Teardown**: none. A repo's thread lives for the daemon process's entire life, even if that repo goes idle forever.
- **Cap**: none. No limit on the number of simultaneously-open per-repo threads or per-repo queue depth.

> **Shortcut** (per this project's deliberate-shortcut convention): unbounded per-repo threads with no idle teardown is acceptable while a daemon's lifetime repo count stays in the tens. Upgrade trigger: observed thread/resource pressure from accumulated per-repo threads on a long-lived daemon — at which point add idle-timeout teardown (respawn lazily on the next write for that repo) rather than redesigning the isolation model itself.

**Live-diagnosed follow-up fix (2026-09-04, SALTMDB memory `c90f7a6e`): WAL mode, not a write-queue redesign.** The per-repo single-writer-thread model above was never the bug: it correctly serializes writers against each other. What was missing is reader/writer isolation at the SQLite layer itself. Every connection previously ran SQLite's default rollback-journal mode, under which a writer's commit takes an exclusive lock that blocks *every reader* — including `dispatch.py`'s fresh-per-call read connections — for the commit's entire duration, however long that turns out to be (live-traced on the running daemon to a multi-minute WSL2 ext4 journal-commit stall, `ps -T -p <pid> -o tid,stat,wchan` showing `jbd2_log_wait_commit`). Every raw `sqlite3.connect()` in the storage layer (the 4 store classes' `db_path`-only fallback, plus `write_queue.py`'s `_RepoWriter._run`) now goes through a shared `acie.storage.connection.open_connection()`, which sets `journal_mode=WAL` + `synchronous=NORMAL`. Readers now see a stable snapshot and are never blocked by an in-progress writer commit, regardless of how slow that commit's own fsync is. `open_connection()` retries the journal-mode transition briefly (only ever needed once per file, ever — WAL is sticky) since a concurrent connection holding an open write transaction at that exact moment makes the one-time transition fail with `SQLITE_LOCKED`, a failure mode that (unlike an ordinary read/write) does not respect sqlite3's own busy-timeout retry loop.

SALTMDB's dual foreground/background priority-lane split within one queue remains available to adopt later if ACIE ever needs finer-grained fairness *within* a single repo's own queue; v0 ships with a single FIFO lane per repo.

## Incremental Indexing Wiring

**Resolved during implementation** (`src/acie/daemon/watcher.py`, `src/acie/daemon/ignore.py`, `src/acie/daemon/notify_hook.py`): tiers 1-3 of `ARCHITECTURE.md`'s incremental-indexing precedence, previously named as out of scope for the daemon-design phase, are now wired in.

- **`WatcherRegistry`** is the tier-1 counterpart to `BootstrapCoordinator`/`WriteQueue` above — same shape (lazy per-repo creation, no idle-teardown, lives for the daemon's whole process life) and the exact same trigger point: `runtime.py`'s `register_repo()` starts a repo's `RepoWatcher` in the same call that starts its bootstrap walk, the first time any RPC (or `notify_hook` call) touches that `repo_path`.
- Each `RepoWatcher` wraps one `watchdog.observers.Observer` scoped to that repo's root, debounces/coalesces raw filesystem events per-path over a ~500ms window (`threading.Timer`-based — `watchdog` has no built-in debounce), then submits one write-queue job per touched path (`make_reindex_job`, promoted to a public name 2026-09-02 once tier 4 became its second caller — see below). That job does the hybrid mtime-then-hash staleness check (backed by a new `file_state` table, `src/acie/storage/file_state_store.py`) and calls the same `index_file()` every other reindex path uses — a delete is `index_file(path, "")` plus an explicit cleanup of the always-present module-kind symbol `index_file` alone doesn't remove (see the corrective memory linked from the grilling decision), and a rename is exactly that delete decomposed alongside a normal index of the new path.
- **`notify_hook` is a control-plane RPC method**, handled in `runtime.py`'s `dispatch()` closure before it ever reaches `dispatch.py`'s `DISPATCH_TABLE` — the same pattern `server.py` already uses for `shutdown`/`ping`, since it's a mutating call that must work on a repo that isn't indexed yet (`register_repo()` runs unconditionally first, exactly like any other RPC touching `repo_path`). `handle_notify_hook` (`notify_hook.py`) dispatches on `params.agent`:
  - `"git"` diffs the daemon's own tracked `last_indexed_head_sha` (a new column on `index_meta`, `src/acie/storage/index_meta_store.py`) against the repo's current `git rev-parse HEAD`, deliberately ignoring whatever SHA(s) the calling git hook itself passed — `post-commit`/`post-merge` don't reliably provide one, so all four hook types funnel through this one uniform path.
  - `"claude-code"` and `"codex"` each parse `params.payload` (the hook's raw stdin JSON, forwarded as-is by the CLI) for the edited file path(s) — a clean `tool_input.file_path` for Claude Code, a unified-diff body in `tool_input.command` for Codex's `apply_patch`/`Bash` tool calls (verified against `developers.openai.com/codex/hooks`).
  - An unrecognized `agent` value is a silent no-op, matching `ARCHITECTURE.md`'s "never break or delay the caller" contract for the whole feature.
- **`src/acie/daemon/ignore.py`** is a single `.gitignore`-aware predicate (via `pathspec`, composing every `.gitignore` in the repo — root plus nested, with real git precedence) shared between the watcher and bootstrap's own walk (`dispatch.py`'s `_read_source_files`, now taking an optional `is_ignored` callable) — the two were previously each independently deciding scope (bootstrap: dotdir-skip + `.py`-extension only, no `.gitignore` awareness at all) and would otherwise silently disagree about which files are indexable.
- **CLI**: `acie notify-hook --agent {git,claude-code,codex}` reads the hook's raw payload from stdin and sends it via the `notify_hook` RPC with the architecture's locked ~200ms client-side timeout, always returning exit code 0 regardless of outcome (see "CLI Surface" below — this replaces that row's earlier "wiring is out of this document's scope" note).

**Follow-up fix** (SALTMDB f4bdfc9d decision 10, grilled and shipped 2026-09-02): `runtime.py`'s `dispatch()` now resolves a raw `repo_path` to its canonical `(repo_id, repo_root)` pair once per request (`resolve_repo_id`/`resolve_repo_root`, `repo_id.py`) and threads both through — `WriteQueue`/`BootstrapCoordinator` key their in-memory state on `repo_id` (`db_path_for` is now a pure `repo_id -> index.sqlite` string join, no `git` subprocess), `WatcherRegistry` keys on `repo_root` (the actual directory an `Observer` watches — genuinely different worktree directories still get separate watchers, but two spellings of the same worktree no longer get a duplicate one), and `BootstrapCoordinator.register`/its injected `walk_repo` became 2-arg (`repo_id` for readiness bookkeeping, `repo_root` for the actual walk, since a `repo_id` hash can't be reversed into a directory). `handle_notify_hook` (`notify_hook.py`) no longer registers or resolves `repo_root` itself — it trusts `dispatch()`'s unconditional prior resolution and receives `repo_id`/`repo_root` directly. See `ARCHITECTURE.md`'s "Not Yet Specified" for what's still deliberately unsolved (multi-worktree walk-merging semantics, as opposed to the keying consistency itself).

**Tier 4 implemented** (2026-09-02, `src/acie/daemon/staleness.py`, `runtime.py`'s `ensure_fresh`): the final safety-net fallback, scoped to the 3 tools that name exactly one file in their params — `list_imports(file=...)` and `get_definition`/`find_references` when called with `position={file, ...}` (a `symbol_id` call names no file, so it's skipped). `find_symbol`/`graph`/`impact_analysis`/`explain` stay out of scope (no single file named up front, or — for `explain` — staleness is beside the point); `structural_search` needs nothing here since it already reads its `files` mapping live off disk every call.

`extract_staleness_target(method, params)` (`staleness.py`) is a pure function returning the one repo-relative `.py` path a request's params name, or `None`. `dispatch()` calls the module-level `ensure_fresh(write_queue, repo_id, repo_root, method, params, timeout=2.0)` (`runtime.py`) after `register_repo()` but only once `bootstrap.repo_ready(repo_id)` is already `True` (no point queueing staleness work ahead of a first bootstrap that will index the file anyway). `ensure_fresh` is a plain module-level function, not a `create_daemon()` closure, specifically so it stays unit-testable against a real `WriteQueue` without a full daemon/socket harness.

When `extract_staleness_target` returns a path, `ensure_fresh` submits `make_reindex_job(repo_root, rel_path)` — the exact same job tier 1's watcher already uses, promoted from `_make_watch_job` to a public name once tier 4 became its second caller (reuse, not a parallel staleness-check implementation) — to that repo's write queue and blocks on the Future for up to 2 seconds. On success the query proceeds against a guaranteed-fresh index for that one file; on timeout or any exception, it logs a warning and proceeds anyway with whatever the index currently has — this must never delay or fail a query beyond its bounded budget, the same "never break the caller" principle `notify-hook`'s own 200ms fire-and-forget timeout already uses (see `ARCHITECTURE.md`'s "Agent Hook Integration"). A redundant reindex racing an in-flight watcher job for the same edit is harmless and cheap (the mtime check makes a no-op reindex nearly free), matching tier 1/3's already-shipped "no cross-tier dedup" decision.

## Auth Token Stance

**No enforcement in v0.** ACIE holds no sensitive data and is a single-machine, single-user local dev tool binding only to `127.0.0.1` — SALTMDB's token-in-discovery-file precedent (defending against another local process connecting to the wrong daemon) was judged unwarranted here.

The envelope's `token: str|null` field (see "Request/Response Envelope") is kept exactly as specified: every request sends `token: null`, and dispatch never checks it. This is a zero-cost future extension point should a real need for it emerge later. The reserved transport-level `AUTH_FAILED` error code stays reserved but unused. The discovery file (`~/.acie/daemon.json`) is still created `0600` owner-only as a sane filesystem default — that permission choice is not itself a contested branching decision, just good hygiene.

## Shutdown / Stop Semantics

**Manual-stop-only for v0.** No idle-timeout auto-shutdown — deliberately deferred as a named shortcut; revisit if an idle daemon proves a real nuisance in dogfeeding use.

- `acie daemon stop` calls an RPC `shutdown` method over the already-locked transport/envelope — not a raw signal-by-PID kill.
- On receiving `shutdown`, the daemon: stops accepting new RPCs (returning `DAEMON_SHUTTING_DOWN` to anything that arrives after this point), drains every per-repo write queue to completion, deletes `~/.acie/daemon.json`, sends the RPC's success response, then forces the process to exit outright.
- `SIGTERM`/`SIGINT` are wired to the exact same graceful drain-and-cleanup routine, so `systemd`, containers, and a plain `kill` all get a clean exit too — not just the CLI path.
- No `acie daemon restart` subcommand exists: `stop` followed by the next auto-spawn covers it (see "CLI Surface").

**Resolved during implementation** (Slice 5, `src/acie/daemon/server.py`): `DaemonServer.shutdown()` runs the full drain-and-cleanup sequence synchronously in the calling thread (the `shutdown` RPC's own connection-handler thread, or a signal handler via `install_signal_handlers`) — set the shutting-down flag first, run the injected `on_shutdown` drain callback, delete the discovery file, then close the listening (and election) sockets. "Stops accepting new RPCs (returning `DAEMON_SHUTTING_DOWN` to anything that arrives after this point)" is real for the entire window between the flag being set and the socket actually closing — including while `on_shutdown` is still draining, since the accept loop keeps running on its own thread throughout, dispatching every other concurrent connection straight to the `DAEMON_SHUTTING_DOWN` short-circuit without ever reaching the injected `dispatch` callable. `shutdown()` is idempotent (a second call, from either trigger, is a no-op) since the RPC path and the signal-handler path both call the identical method and either could fire first (e.g. `acie daemon stop`'s RPC racing an operator's `kill`).

**Live-diagnosed follow-up fix** (2026-09-04, SALTMDB memory `2ff82fd1`): closing the listening socket does **not**, by itself, guarantee the process exits. The accept loop blocks in a plain, timeout-less `socket.accept()` on its own thread; on Linux, closing that socket from a *different* thread (the RPC-handler thread running `shutdown()`, or the signal handler) does not reliably wake a thread already parked inside `accept()`. Left alone, `serve_forever()`'s `.join()` on that thread — and therefore `main()` — would never return, so the process would sit forever needing `kill -9`, even though the `shutdown` RPC itself reports success. The fix: after the RPC response has been sent (or its send attempted — `_handle_connection`, after `shutdown` dispatch) or, for the signal-handler path, immediately after `shutdown()` returns (no RPC response to wait for), the daemon calls `os._exit(0)` to terminate the process outright, bypassing the accept loop entirely rather than waiting for it to notice. This mirrors the SaltMDB daemon's identical fix for the same class of bug (its `_shutdown_sequence()`'s own `os._exit(0)`, reached via a dedicated shutdown-watcher thread). The force-exit is wired in only for the real daemon subprocess (`runtime.py::create_daemon`'s `exit_process=lambda: os._exit(0)`) — `DaemonServer` itself defaults `exit_process` to `None` (a no-op), so constructing one directly in-process (as most of `tests/daemon/test_server.py` does) never risks taking down the calling process.

## CLI Surface

Stdlib `argparse` — no new dependency. Entry-point wiring: `src/acie/cli.py` + `src/acie/__main__.py`, with `[project.scripts] acie = "acie.cli:main"` in `pyproject.toml`.

| Subcommand | Shape |
|---|---|
| `acie serve-mcp` | Zero required flags; optional `--log-level`. Auto-spawns the daemon on demand per "Process Lifecycle" — no separate manual daemon-start step needed for normal agent-host use. |
| `acie daemon start` | Backgrounds by default via the same internal spawn target the auto-spawn path uses (`subprocess.Popen([sys.executable, "-m", "acie.daemon.server"], start_new_session=True, ...)`). `--foreground` runs that module's main loop directly in the current process — the shape needed for `systemd`/container supervision. |
| `acie daemon stop` | Triggers the graceful shutdown RPC described above. |
| `acie daemon status` | Plain-text by default; `--json` for machine-readable output. |
| `acie daemon restart` | **Does not exist** — use `stop`, then let the next `acie serve-mcp` auto-spawn a fresh daemon. |

**Resolved during implementation** (2026-09-02, fixing SALTMDB `4083924d-ed96-4356-8002-c3ce224daeb5`'s two observability gaps): `acie daemon status` reports one of three states, not two — `running`, `shutting_down` (the daemon is mid-drain: still holding its port/PID, but answering everything except `shutdown` with `DAEMON_SHUTTING_DOWN`), or `stopped` (unreachable). `--json` emits `{"running": bool, "status": "..."}`, `running` being `true` only for the `running` state. `acie daemon stop`'s own `shutdown` RPC uses a client-side timeout of `_SHUTDOWN_DRAIN_TIMEOUT_SECONDS + 1.0` (currently 11s) rather than the transport's 2.0s default, since the RPC only responds once the server's whole drain has completed — a shorter client timeout made `stop`'s exit code meaningless for any drain slower than 2s.
| `acie notify-hook --agent <name>` | Reads stdin, sends it as the `notify_hook` RPC's `payload` param over the already-locked transport/envelope, always exits 0 (see "Incremental Indexing Wiring" above and `ARCHITECTURE.md`'s "Agent Hook Integration"). |

## Deployment: Dev Repo vs. Dogfeeding Install

The dev repo stays at `/home/zbalint/workspace/ACIE` — where all implementation and this design work happens. Once a working daemon + MCP-server version exists, a separate install lands at `~/.mcp/ACIE` and gets registered in the harness's MCP-server configuration as ACIE's live "prod" server for ongoing dogfeeding use — mirroring exactly the dev-repo/prod-install split the separate SALTMDB project already uses (`~/.mcp/SALTMDB` installed, `/home/zbalint/workspace/SALTMDB` dev repo).

This also resolves a design gap raised earlier in planning (daemon staleness during active self-development): the *installed* daemon only picks up ACIE code changes on a deliberate reinstall-plus-restart cycle, never automatically from dev-repo edits. No special self-check or auto-reload mechanism is needed to keep the installed daemon and the dev repo from interfering with each other.

## Deferred to Implementation

Not open design questions — settled in direction, but with exact values or mechanics left for implementation time rather than invented here:

- ~~The exact `DAEMON_RPC_MAX_MESSAGE_BYTES` cap value.~~ **Resolved during implementation** (Slice 1, `src/acie/daemon/protocol.py`): `MAX_MESSAGE_BYTES = 16 * 1024 * 1024` (16 MiB), enforced on both `encode_frame` (send) and `decode_length_prefix` (receive).
- Exact auto-spawn retry/backoff timing (poll interval, max attempts before giving up with a clear error).
- `~/.acie/daemon.log` rotation/size-management policy.
- The idle-timeout teardown mechanics named as the write-queue's upgrade trigger, once actually needed (see "Write-Queue Concurrency").

## Out of Scope

Ruled beyond this document's destination, not merely deferred detail within it:

- **Implementation of this spec** — a separate TDD slice-based phase, following the same process that built ACIE's 8 pure-function tools.
- **Multi-repo behavior validation** — the design above accounts for multi-repo structurally throughout (per-repo write queues, per-repo store instances, per-repo readiness flags); actually exercising it under load is deferred to the implementation phase.
- **Automated hook-installer tooling** (`acie install-hooks`) — deferred per `ARCHITECTURE.md`'s existing no-installer stance and SALTMDB's own precedent (its agents read docs and set up hooks by hand).
