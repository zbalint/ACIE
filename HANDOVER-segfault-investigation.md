# Handover: ACIE Daemon SIGSEGV During Bootstrap Indexing

**Status: RESOLVED, 2026-09-01.** Root cause: an upstream `py-tree-sitter`
0.26.0 bug, not an ACIE defect. See "## Resolution" below (added at the top;
everything after it is the original, now-historical investigation log, kept
for the trail that led here).

## Resolution

`py-tree-sitter` 0.26.0's `Point.row`/`Point.column` getters returned a
**borrowed** reference instead of a **new** one (upstream
[issue #472](https://github.com/tree-sitter/py-tree-sitter/issues/472),
fixed by [PR #466](https://github.com/tree-sitter/py-tree-sitter/pull/466),
merged 2026-07-08 as commit `afb3836b` — *after* the `v0.26.0` tag, so no
released version contains the fix yet). Every `.row`/`.column` access
under-decrefs the returned `int` by one. This only bites for **non-immortal
ints** (line/column numbers >256, outside CPython's small-int cache) — which
is every real file over ~256 lines (this repo's own `tests/tools/test_explain.py`
is 301 lines) but never ACIE's own small test fixtures, which is why the
existing test suite never caught it. The under-refcount eventually frees a
still-live int, corrupting the pymalloc free list; the actual SIGSEGV lands
later, elsewhere, once something else reuses the corrupted memory — exactly
the moving-crash-site, GC-frame, non-deterministic-iteration-count behavior
documented throughout this file.

`_build_symbol()` in `extract_symbols.py` reads `node.start_point.row`,
`.column`, `node.end_point.row`, `.column` for every extracted symbol,
unconditionally — that's the trigger. No ACIE source change was needed or
made; `extract_symbols.py`/`extract_relations.py` are untouched.

**Fix applied:** `pyproject.toml`'s `tree-sitter>=0.26.0` pin (which
*excluded* every safe version) was changed to `tree-sitter>=0.25.0,<0.26.0`,
with an inline comment citing the upstream issue/PR and the condition for
re-opening the ceiling once a tagged release containing the fix exists.
`uv lock`/`uv sync` regenerated `uv.lock` to resolve `tree-sitter==0.25.2`.

**Verified:** the exact standalone repro below (10/10 clean), a reduced
isolation script crashing on cycle 1 pre-fix vs. 50 clean cycles post-fix,
the full 282-test suite passing, 3 full rounds over this repo's 78-file
corpus with zero crashes, and a live daemon bootstrap walk (same procedure
as "How to reproduce" below) that grew `index.sqlite` past 330 KB — well
past the ~90-95 KB point that reliably killed every earlier attempt — with
an empty `daemon.log` throughout.

Full narrative, upstream links, and every experiment is in this project's
SALTMDB memory graph, entity `5bdac837-c0d6-4bfe-a75e-89cfdc4648f3`
("[ACIE] SIGSEGV Root Cause Found & Fixed..."), linked via `resolves` to the
memories referenced throughout this file.

Two smaller, independent design gaps noted near the bottom of this file
(stale partial index trusted as ready after a crash; no per-call daemon
respawn in the MCP path) are **not** fixed by this change and remain open.

---

## Original investigation (historical, superseded by the Resolution above)

**Status:** open, sharply narrowed, still unresolved. A deterministic
standalone reproducer now exists. The daemon, SQLite, sockets, and threading
are ruled out. The remaining fault is in the `extract_symbols` / tree-sitter
node-walk lifecycle for one known input.

**Written by:** Claude (Claude Code), 2026-09-01, after ACIE's first live
dogfeed test as a registered MCP server crashed twice in a row on its own
repo. Handed to Codex to continue because of Claude-quota pressure, not
because the investigation hit a wall Claude couldn't get past on its own —
the trail below is live and the next steps are concrete.

## Codex continuation — 2026-09-01

This section supersedes the old working estimate that the crash happened
60–70% through the repository walk. The partial database's 94 KiB file size
was misleading: only five files had committed.

### Strongest result: deterministic standalone reproducer

The triggering input is:

```text
tests/tools/test_explain.py
```

This command crashes without the daemon, SQLite, sockets, or threads:

```bash
cd /home/zbalint/workspace/ACIE
.venv/bin/python -X faulthandler -c '
from pathlib import Path
from acie.adapters.python.extract_relations import extract_relations
p = "tests/tools/test_explain.py"
s = Path(p).read_text()
extract_relations(p, s, "2026-09-01T00:00:00Z")
'
```

Observed repeatedly: exit status `139` (`SIGSEGV`). The faulthandler site is
not stable. It has appeared at:

- `extract_relations.py:145` in `_inherits_relations` (the original daemon
  trace and a standalone reproduction);
- `extract_relations.py:122` in `_index_top_level_symbols`;
- `extract_relations.py:247` in `_defines_relations`, marked
  `Garbage-collecting`;
- `extract_symbols.py:71` in `_unwrap_decorated`;
- `extract_symbols.py:91` in `_build_symbol`.

The moving crash site proves those ordinary Python lines are victims of
earlier invalid state, not individual logic bugs at each reported line.

### How the exact file was recovered

The crashed partial index was read in SQLite read-only mode. Its
`index_meta` generation was `5`, and `symbols_live` contained exactly these
five paths, in insertion order:

1. `tests/test_cli.py`
2. `tests/__init__.py`
3. `tests/test_indexer.py`
4. `tests/test_mcp_server.py`
5. `tests/test_repo_id.py`

`acie.daemon.dispatch._read_source_files()` currently returns 78 Python
files. Its sixth path is `tests/tools/test_explain.py`. Direct testing then
confirmed that file alone reproduces the crash.

### Isolation matrix

All runs used the dev repo's `.venv/bin/python` (CPython 3.11.15),
`tree-sitter==0.26.0`, and `tree-sitter-python==0.25.0`.

| Experiment | Result |
| --- | --- |
| Focused existing tests | `33 passed` |
| `has_syntax_error()` 1,000 times | Pass |
| Parse + `root.named_children` 100 times | Pass |
| Parse + child `.type` 100 times | Pass |
| Parse + child start/end points 100 times | Pass |
| Parse + function-name `Node.text` 100 times | Pass |
| Parse + function-name `Node.text.decode()` 1,000 times | Pass |
| `extract_symbols()` repeatedly | SIGSEGV by iteration 5 in one run; by iteration 7 with cyclic GC disabled |
| Equivalent manual loop combining name text + start/end points into tuples | SIGSEGV by iteration 2 |
| `extract_relations()` alone | SIGSEGV on first call |
| Full `has_syntax_error` → `extract_symbols` → `extract_relations` sequence | SIGSEGV on first iteration |

Therefore:

- Parse construction alone is safe in this reproducer.
- Basic node accessors are safe when exercised separately.
- The fault appears when ACIE combines name-text and position reads while
  building extracted records.
- Disabling Python cyclic GC does not remove it.
- Keeping the encoded source bytes in a separate local variable did not
  distinguish the simplified name-only walk: both temporary-byte and
  retained-byte variants completed 1,000 iterations. The earlier
  "temporary `source_text.encode()` lifetime" hypothesis is not proven and
  should not be presented as the root cause.

### Environment drift ruled out

The installed crashing clone (`/home/zbalint/.mcp/ACIE`) and the dev repo use
the same CPython 3.11.15 and dependency versions. SHA-256 hashes of both
native bindings are identical across clones:

```text
tree_sitter/_binding.cpython-311-x86_64-linux-gnu.so
  d9cf72ce2e6fbcc316ccefe6a6303ea01c26cadb75173141c9719e67bc2ed903
tree_sitter_python/_binding.abi3.so
  edf0862b2fda605a9c439955b2f60b248941c9af709222e7b1566bc603a0aa04
```

`extract_symbols.py` and `extract_relations.py` are byte-for-byte identical
between the clones. The installed clone's only working-tree modification is
the copied `faulthandler` change in `server.py`.

### Exact next investigation step

A temporary harness exists at `/tmp/acie_segfault_isolate.py`. It is not a
repository artifact and may disappear after reboot. It supports these modes:

```text
syntax, symbols, relations, full, named_children, child_types,
child_points, function_names, function_names_decode, symbol_tuples,
symbol_objects
```

Continue by splitting the crashing `symbol_tuples` mode one operation at a
time. The smallest known bad combination currently performs, for every
top-level function node:

```python
name = child.child_by_field_name("name").text.decode("utf-8")
fields = (
    path,
    name,
    "function",
    child.start_point.row + 1,
    child.start_point.column,
    child.end_point.row + 1,
    child.end_point.column,
)
```

Test these boundaries in separate subprocesses because a SIGSEGV kills the
interpreter:

1. `name text + start_point` only;
2. `name text + end_point` only;
3. change accessor order (points before `Node.text`);
4. retain each `name_node` wrapper until the tree walk ends;
5. traverse with a `TreeCursor` instead of materializing
   `root.named_children`;
6. reduce `tests/tools/test_explain.py` by top-level function until the
   smallest source still crashes;
7. compare against one adjacent tree-sitter version only after the API-level
   reproducer is minimal.

Do not implement a production fix until one of these experiments produces a
single causal lifetime/API hypothesis. No ACIE source fix was made during the
Codex continuation.

### Delegated-agent note

Three requested Luna Max lanes were started (binding audit, standalone
reproduction, upstream issue research), but all terminated without results
because the delegated-agent account reached its usage limit. Do not wait for
or rely on those lanes; their evidence contribution is zero.

**Companion evidence:** the full narrative (with exact timestamps, dmesg
output, and reasoning for each ruled-out hypothesis) is also stored in
this project's shared SALTMDB memory graph, entity id
`9695414f-b66b-4309-8bc9-37ffea3baadb` ("[ACIE] SIGSEGV Root-Cause
Narrowed..."), with lineage back through two prior versions of the same
memory as the investigation progressed. If your environment has SALTMDB
MCP access, reading that entity (and its `ancestors`/`related_to` lineage)
gives the same information as this file plus some tangential context.
This file is the standalone version for an environment without that access.

## Symptom

The ACIE daemon (`acie.daemon.server`, spawned via `acie serve-mcp`'s
auto-spawn-on-demand path, `cli.py`'s `_ensure_daemon()`/`_spawn_daemon()`)
reliably crashes with a **SIGSEGV** (kernel-confirmed, not a Python
exception) while running its first-ever bootstrap indexing pass over this
repo (`/home/zbalint/workspace/ACIE`, 86 `git ls-files`-tracked files).

Reproduced twice, both times partway through the walk (94208 bytes into a
partially-written `index.sqlite`, roughly 60-70% of the way through by file
count) — not on the first file, not deterministically on the same file
either time as far as confirmed (the crash *site* was identical both times,
see below, but nothing pins which specific *input file* triggered it).

## Confirmed root-cause class: a real, kernel-level SIGSEGV

Via `dmesg -T | tail -50` on the host (WSL2, `6.18.33.2-microsoft-standard-WSL2`):

```
[Tue Sep 1 03:53:13 2026] python[16633]: segfault at 5e0b56 ip 00005e0998245267 sp 000073df96fd1710 error 4 in python3.11[...] likely on CPU 2 (core 2, socket 0)
[Tue Sep 1 03:53:13 2026] python3.11: python: potentially unexpected fatal signal 11.
```

This is **not** a Python-level uncaught exception, deadlock, or OOM kill —
it's a fatal signal (11 = SIGSEGV) inside the CPython 3.11 interpreter
itself. A segfault bypasses Python's exception/logging machinery entirely,
which is why the daemon's own `daemon.log` was completely empty (0 bytes)
after the first crash despite ~1 minute of runtime.

`journalctl -k --since "-10min"` returned **no entries** on this host even
though `dmesg` had the segfault — journald isn't wired to this WSL2
instance's kernel ring buffer the way `dmesg` is. **Use `dmesg -T`, not
`journalctl -k`, on this host.**

## Fix already applied (commit `e75eaf4`, this repo, `master`)

Added `faulthandler.enable()` as the first line of `acie.daemon.server.main()`:

```python
def main() -> int:
    """Run the production daemon in the foreground until it shuts down."""
    from acie.daemon.runtime import create_daemon

    faulthandler.enable()
    server = create_daemon(election_port=DAEMON_ELECTION_PORT)
    ...
```

This doesn't fix the segfault — it makes the *next* one leave a trace.
It already proved its worth: the second reproduction (below) came with a
full native stack trace where the first one had nothing. **This fix is
already merged; don't re-add it.** Full test suite (282 tests) passes with
it in place.

## The actual crash-site evidence (from the second reproduction, with faulthandler active)

```
Fatal Python error: Segmentation fault

Current thread (most recent call first):
  File ".../src/acie/adapters/python/extract_relations.py", line 145 in <listcomp>
  File ".../src/acie/adapters/python/extract_relations.py", line 145 in <dictcomp>
  File ".../src/acie/adapters/python/extract_relations.py", line 144 in _inherits_relations
  File ".../src/acie/adapters/python/extract_relations.py", line 32 in extract_relations
  File ".../src/acie/indexer.py", line 37 in index_file
  File ".../src/acie/daemon/bootstrap.py", line 131 in job
  File ".../src/acie/daemon/write_queue.py", line 139 in _run
  ... (thread bootstrap frames)

Extension modules: tree_sitter_python._binding (total: 1)
```

The other two threads in the dump (the socket accept loop, and the main
thread blocked in `serve_forever()`'s `.join()`) were idle — not implicated.

The crashing code, `extract_relations.py:144-145`:

```python
class_candidates_by_name = {
    name: [s for s in candidates if s.kind == "class"]
    for name, candidates in top_level_by_name.items()
}
```

**Important: `s` here is a plain `Symbol` dataclass instance** (produced
earlier by `extract_symbols()`, indexed by `_index_top_level_symbols()`),
**not** a tree-sitter `Node`. This comprehension touches zero tree-sitter
objects directly. That is strong evidence this is a **downstream symptom
of heap corruption caused earlier**, not the actual bug site — the
classic segfault profile where the crash surfaces at the next unrelated
allocation after memory gets corrupted, not at the corruption's own
origin.

The two most likely upstream culprits, both of which walk a
`tree_sitter_python`-parsed tree immediately before this comprehension
runs, in the same function:
- `_import_relations(tree.root_node, ...)` — called at `extract_relations.py:29`, right before `_inherits_relations` at line 32.
- Something inside `extract_symbols()` itself (`extract_relations()` calls it at line 15, before any of its own tree-sitter work) — worth checking with equal suspicion, not yet examined in detail.

## Hypotheses already ruled out

1. **Uncaught Python exception in a background thread / shutdown-drain
   deadlock.** This was the first working theory (before `dmesg` was
   checked) — wrong. Superseded once the kernel log confirmed a real
   SIGSEGV.
2. **`sqlite3` connection shared/misused across threads.** Checked
   `write_queue.py`'s `_RepoWriter._run()`: the `sqlite3.connect(db_path)`
   call happens inside the writer thread's own `_run()` method and the
   resulting `conn` is used exclusively within that same thread for the
   repo's whole lifetime. Correct discipline — not the classic
   cross-thread-sqlite3-connection crash pattern.
3. **`Tree` garbage-collected while a `Node` derived from it is still in
   use** (a known historical footgun in some tree-sitter Python bindings —
   older/naive bindings let a `Node` outlive its parent `Tree` and then
   dereference freed memory). Checked `extract_relations()`: the local
   `tree` variable is referenced throughout the whole function body (used
   at lines 29, 33, 38+), so ordinary CPython refcounting keeps it alive
   for the full call — this specific mechanism is not what's happening
   here, at least not in this simple a form.
4. **Stale/old, since-fixed tree-sitter version.** Installed versions are
   `tree-sitter==0.26.0` and `tree-sitter-python==0.25.0`, both matching
   this repo's `pyproject.toml` `>=` pins — i.e. current versions, not an
   old release with a long-since-patched bug. (Doesn't rule out an
   *unfixed* bug in these current versions — just rules out "you're on an
   ancient known-broken release.")

## Current best (unproven) hypothesis

A memory-safety bug in the `tree-sitter`/`tree-sitter-python` 0.26.0/0.25.0
native binding layer, triggered by this codebase's specific usage pattern:

- A module-level `_LANGUAGE = Language(tspython.language())` singleton is
  shared across all calls (in both `extract_symbols.py` and
  `extract_relations.py` — **note these are two separate `_LANGUAGE`
  objects, one per module**, each independently wrapping the same
  underlying grammar; worth checking whether that duplication itself
  matters).
- Each file gets **two independent fresh `Parser` instances and two
  independent `Tree` objects** — `extract_relations()` calls
  `extract_symbols()` internally (which parses once), then parses the
  *same* `source_text` again itself (`extract_relations.py:20-21`) to get
  its own tree for relation extraction. So every file during bootstrap is
  parsed twice, by two separately-constructed `Parser`/`Tree` pairs, in
  rapid succession, all within the same writer thread.
- The crash consistently happens ~60-70% through the file walk, not near
  the very start — consistent with corruption that needs some number of
  parse/GC cycles to actually manifest as a fault (rather than a
  first-call-always-crashes bug), which fits typical heap-corruption
  behavior (a bad write into freed/adjacent memory that only faults once
  something else reallocates that region).

This is **not confirmed** — it's the most plausible explanation given
everything ruled out above, not a proven root cause.

## Suggested next steps, roughly in order of effort/payoff

1. **Minimal standalone repro, outside ACIE's daemon/threading entirely.**
   Write a bare script that just does what `index_file()` does — call
   `extract_symbols()` then `extract_relations()` — in a tight loop over
   this repo's own 86 `git ls-files`-tracked `.py` files, repeated (say)
   10-20x, with **no daemon, no threads, no write queue, no sockets**. If
   it still segfaults, that isolates the bug to the tree-sitter usage
   itself and rules out anything daemon/threading-related. If it does
   *not* crash after a very generous number of iterations, threading (or
   something else in the daemon path) is actually implicated after all,
   and hypothesis 2 below becomes the priority again despite being
   checked once already (check it more thoroughly — e.g. does anything
   *else* touch tree-sitter concurrently with the bootstrap writer thread,
   such as a `structural_search` read-path call landing on a different
   thread from `DaemonServer._accept_loop`/`_handle_connection`, while
   bootstrap is mid-walk? That specific interleaving was **not** ruled out
   this session — only the write-path's sqlite3 usage was checked.).
2. **Reproduce under `faulthandler.enable()` several more times** and see
   whether the crash site is *always* `extract_relations.py:144-145`,
   varies within `extract_relations.py`/`extract_symbols.py`, or ever lands
   somewhere unrelated entirely — that tells you how "downstream" the
   symptom really is. (This session only captured one instance of the
   trace; more samples would meaningfully narrow things.)
3. **Reproduce under a debug/ASAN-instrumented CPython build**, or under
   `gdb` with a core dump (`ulimit -c unlimited` before spawning, then
   `gdb python3.11 core` after a crash) to get an actual native
   backtrace *inside* the crash, rather than the Python-level
   `faulthandler` trace (which shows the Python frame at time of crash,
   not the C call stack inside `tree_sitter_python._binding` where the
   fault actually occurred).
4. **Check `tree-sitter-python`'s and `py-tree-sitter`'s GitHub issue
   trackers** for `0.26.0`/`0.25.0` + "segfault"/"SIGSEGV" — if this is a
   known upstream bug, there may already be a fix, a workaround, or a
   version to pin away from.
5. If confirmed as an upstream tree-sitter bug: the ACIE-side fix is
   probably either pinning to a different tree-sitter/tree-sitter-python
   version pair, or restructuring `extract_relations()`/`extract_symbols()`
   to share a single `Tree` per file (parse once, pass the tree into both)
   instead of parsing twice — worth trying as a workaround even before the
   upstream root cause is fully understood, since it directly removes one
   of the two parse operations per file that's part of the current best
   hypothesis.

## How to reproduce (exact steps used both times this session)

```bash
# From the installed clone (or the dev repo directly — either works,
# they were on the same commit both times this session):
cd /home/zbalint/.mcp/ACIE   # or /home/zbalint/workspace/ACIE

# Clear any prior state so a full fresh bootstrap walk actually happens
# (a partial index.sqlite from a prior crash is trusted as "ready" by
# BootstrapCoordinator.repo_ready() without re-walking -- see the
# separate, still-open "stale partial index" design gap noted in the
# SALTMDB memory lineage above; not this bug, but it'll mask a retry if
# you don't clear it):
rm -rf ~/.acie/repos/*
rm -f ~/.acie/daemon.json
: > ~/.acie/daemon.log

# Spawn the daemon directly (bypasses the MCP stdio adapter, which only
# auto-spawns once at its own process startup -- see the separate
# "no per-call daemon respawn" gap noted in the same SALTMDB memory;
# also not this bug, but it's why a live MCP tool call may just return
# DAEMON_UNAVAILABLE forever after a crash instead of retrying):
.venv/bin/acie daemon start

# Trigger a bootstrap walk of this repo by making any one daemon RPC
# against it -- easiest is through a live `acie` MCP connection's
# find_symbol/list_imports/etc. tool calls if you have one, or you can
# drive the daemon's socket protocol directly (see acie.daemon.client
# for the wire format) without an MCP client at all.

# Poll for the crash:
watch -n1 'ls -la ~/.acie/repos/*/index.sqlite 2>&1; python3 -c "
import socket
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(1)
print(\"57831 (election port) ->\", s.connect_ex((\"127.0.0.1\", 57831)))
"'

# Once the election port goes to connection-refused (111), check:
cat ~/.acie/daemon.log
```

Both reproductions crashed within ~15-20 seconds of the bootstrap walk
starting, with `index.sqlite` around 90-95 KB at the moment of death both
times (out of the full 86-file corpus) — consistent enough to expect a
fresh repro to land in a similar timeframe, though not necessarily on the
exact same file.

## Two separate, smaller, independent issues surfaced along the way

Neither of these is the segfault, but both were found investigating it and
are worth fixing separately if you have spare cycles:

1. **Stale partial index is trusted as "fully indexed" after a crash.**
   `BootstrapCoordinator.repo_ready()` in `bootstrap.py` treats
   "`index.sqlite` exists on disk" as sufficient once a *new* daemon
   process starts — the in-memory `_in_progress` tracking that guards
   against a same-process race doesn't survive the daemon dying. So: this
   segfault leaves a partial `index.sqlite` → daemon auto-respawns → the
   new process immediately marks the repo "ready" with no resumed walk
   and no error → every tool call from then on silently returns
   incomplete results with no `INDEX_NOT_READY` signal at all.
2. **No per-call daemon respawn in the MCP path.** `mcp_server.py`'s
   `_daemon_tool`'s `call()` only ever calls `request_daemon()` — the
   actual daemon-ensuring/auto-spawn logic (`cli.py`'s `_ensure_daemon()`)
   only ever runs once, at `serve-mcp` process startup. Once the daemon
   dies mid-session, every subsequent tool call returns
   `DAEMON_UNAVAILABLE` for the rest of that MCP connection's lifetime —
   contradicting DAEMON.md's "Process Lifecycle: Auto-Spawn on Demand"
   framing, which reads as if this should self-heal per call.
