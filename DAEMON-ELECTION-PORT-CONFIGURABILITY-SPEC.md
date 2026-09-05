# Daemon — Configurable Election Port for Test/Multi-Instance Isolation

## Status: LOCKED for handoff (planning session, 2026-09-06)

Raised during Capability H / H2 review (SALTMDB memory `6374cc42`) as an
operational incident: a real, long-running workspace `acie.daemon.server`
process (auto-spawned on first daemon-backed tool call in this repo, per
user clarification this session) squats on the fixed production election
port for the lifetime of the dev session. `DaemonServer` itself already
supports an injectable `election_port` at the class level
(`src/acie/daemon/server.py:81`, `runtime.py:143-149`'s `create_daemon`) —
this gap is one layer up, at the CLI/process-spawn boundary, and was
already diagnosed in detail in a prior session: SALTMDB memory `22748000`
("CLI/Server Integration Tests Flake From Orphaned Real Daemons on the
Fixed Production Election Port", 2026-09-01) names the exact same root
cause and sketches the same fix direction. This spec locks that sketch
into an implementable design.

## The problem

- `server.py:332`: `main()` (the real `python -m acie.daemon.server`
  foreground entry point, invoked by both real dev usage and every
  CLI/MCP-server integration test that spawns a subprocess daemon) always
  calls `create_daemon(election_port=DAEMON_ELECTION_PORT)` — the fixed
  module constant `57831` (`server.py:46`), no override point.
- `cli.py:160-176`'s `_spawn_daemon()` always execs
  `[sys.executable, "-m", "acie.daemon.server"]` with `start_new_session=True`
  (full session/process-group detachment) and no environment or argument
  injection point for a caller-specific port.
- Every real dev-machine daemon (auto-spawned on first daemon-backed tool
  call) and every `test_cli.py`/`test_mcp_server.py` real-subprocess
  integration test therefore all contend for the exact same machine-global
  TCP port. A long-lived dev daemon holding that port for its whole
  session is entirely normal and correct on its own terms — the actual bug
  is that tests have no way to ask for a different one, so they collide
  with it instead of using an isolated port the way
  `tests/daemon/test_server.py`'s unit tests already do via `_free_port()`
  against the class-level `election_port` parameter.
- Confirmed live this session: PID 61020 (`/home/zbalint/workspace/ACIE`
  venv), up 34+ minutes, bound to `127.0.0.1:57831`, is exactly this
  legitimate long-lived dev daemon — not an orphan, and not something to
  kill (killing it only causes an immediate respawn on the next MCP tool
  call, per `_ensure_daemon`'s auto-spawn contract). It is, however, live
  proof of the collision: any integration test run on this machine right
  now would contend with it for the fixed port.

## Locked mechanism

Add an environment-variable override, read once at the same layer that
currently hardcodes the constant, and thread it through the one process-
spawn boundary that needs it:

1. `server.py`'s `main()`: read
   `os.environ.get("ACIE_DAEMON_ELECTION_PORT")`; if set and a valid int,
   pass it as `election_port` to `create_daemon` instead of
   `DAEMON_ELECTION_PORT`. Invalid (non-integer) values should fail fast
   with a clear error rather than silently falling back — this is a
   startup-time configuration error, not a runtime condition to degrade
   gracefully from.
2. `cli.py`'s `_spawn_daemon()`: accept an optional `election_port: int |
   None = None` parameter; when given, pass
   `env={**os.environ, "ACIE_DAEMON_ELECTION_PORT": str(election_port)}`
   to `subprocess.Popen` (currently no `env=` kwarg is passed, so the
   child inherits the parent's environment unchanged by default — this
   only needs to *add* the one variable, not reconstruct the environment).
3. `_ensure_daemon()` gains the same optional `election_port` parameter,
   passed through to `_spawn_daemon()`. `_daemon_start()` (the `acie
   daemon start` CLI command) does not need a new public flag for this —
   the override is meant for tests and any future embedding caller that
   constructs the CLI's Python API directly, not for interactive/production
   use, so no `argparse` surface change is required unless the user wants
   one.
4. Tests (`test_cli.py`, `test_mcp_server.py`) call `_ensure_daemon(
   election_port=<value from a per-test _free_port()-equivalent>)` (or
   whatever the actual public entry point each test drives turns out to
   be — confirm exact call sites before implementing) instead of relying
   on the fixed default.

## Explicitly not changing

- `DaemonServer`'s own constructor/`_bind_election_port` — already
  correctly parameterized, this spec only closes the gap one layer above
  it.
- The discovery-file protocol (`discovery.py`) — confirmed live this
  session that `service_port` (the actual client-facing socket) is
  already fully decoupled from the election port (a startup mutex only);
  no discovery-file or client-side change is needed for this fix.
- Real interactive `acie daemon start` behavior for a normal user — with
  no env var set, behavior is byte-identical to today (fixed port 57831).

## Residual gap named, not fixed here

Memory `22748000` also notes that in some sandboxed environments,
`kill`/`pkill` against these detached (`start_new_session=True`) daemon
processes was observed to report success without reliably freeing the
process from `ps aux` (though the port itself did free up) — this was not
root-caused and is not part of this spec's scope. Making the port
configurable removes the *contention* (tests get their own port and never
have to kill anything to proceed), which is the actual fix; positively
confirming a stray daemon is fully dead in an unreliable sandbox remains a
separate, unsolved problem if it resurfaces.

## Files to touch

- `src/acie/daemon/server.py` — `main()`.
- `src/acie/cli.py` — `_spawn_daemon()`, `_ensure_daemon()`.
- `tests/test_cli.py`, `tests/test_mcp_server.py` — the two known-flaky
  integration tests (`test_daemon_start_spawns_a_daemon_and_stop_shuts_it_down`,
  `test_serve_mcp_exposes_and_routes_the_eight_tools`), updated to request
  an isolated election port per test run.

## Workflow constraints carried into this spec

- Implement with the `tdd` skill, one slice, stop before commit for
  review (memory `9b020543`).
- This is daemon/CLI infrastructure, not Capability H — sequence
  independently of the H2 merge-policy fix spec; no ordering dependency
  between the two.

## Verification note

Grounded live this session against `src/acie/daemon/server.py:1-341` (full
file), `src/acie/cli.py:100-181`, `src/acie/daemon/runtime.py:143-156`,
and `src/acie/daemon/discovery.py:1-40` on master at `aa92a51`. Live
process state confirmed via `ps`/`ss` (PID 61020, up 34m47s, cwd
`/home/zbalint/workspace/ACIE`, listening on `127.0.0.1:57831`, distinct
from the ambient MCP server at PID 15865 under `/home/zbalint/.mcp/ACIE`).
Root-cause diagnosis and suggested-fix direction sourced from SALTMDB
memory `22748000` (2026-09-01, prior session) — this spec confirms that
diagnosis still holds against current source and locks it into an
implementable design.
