# structural_search's Spurious DAEMON_UNAVAILABLE — Ignore-Scoping + Per-Method Timeout Fix

Status: spec-and-plan only, written for external implementation (same role
split as the D/H-series and MCP-*-SPEC docs — this document was **not**
implemented in the session that wrote it; a fresh session/agent should
implement it, and another fresh session should review the resulting diff
against this spec before commit).

Triggered by the 2026-09-07 re-verification sweep (SALTMDB memory
`2944aa77`), which confirmed `structural_search` still fails with
`DAEMON_UNAVAILABLE` on a trivially valid `.scm` pattern
(`(function_definition) @fn`) even while every other ACIE tool succeeds on
the same connection in the same session — matching the same symptom
already on file at memory `16bf9924` (2026-09-06) and `2533aa12`. This is
**not** the same finding as memory `df744404`'s correction of an earlier
report: that correction concerned `INVALID_PATTERN` from ast-grep-syntax
patterns fed to a tree-sitter-`.scm`-only tool (a caller mistake, tool
working as designed). `DAEMON_UNAVAILABLE` on a syntactically valid `.scm`
pattern is a different failure mode entirely and is still unexplained and
unfixed as of this spec. Companion spec `POSITION-LOOKUP-CONSISTENCY-SPEC.md`
fixes the sweep's other still-open finding (position-mode `SYMBOL_NOT_FOUND`).
The two specs touch entirely non-overlapping files (this one: `daemon/
client.py`, `daemon/dispatch.py`, `mcp_server.py`; the other: `indexer.py`,
`storage/symbol_store.py`, `storage/relation_store.py`, `tools/resolve.py`)
and can be implemented and reviewed independently, in parallel, in either
order.

## Root cause, read directly from source this session (not re-guessed from
the report)

`DAEMON_UNAVAILABLE` has exactly one call site in the whole codebase:
`src/acie/mcp_server.py:42-45`, inside `_daemon_tool`'s wrapper:

```python
response = request_daemon(
    discovery_path, method=method, repo_path=repo_path, params=params
)
if response is None:
    return _error_result(
        {"code": "DAEMON_UNAVAILABLE", "message": "ACIE daemon is unavailable"}
    )
```

`request_daemon` (`src/acie/daemon/client.py`) is a plain connect-per-call
TCP client with a **hardcoded 2.0-second timeout for every method,
uniformly**:

```python
def request_daemon(
    discovery_path: str, *, method: str, repo_path: str, params: dict,
    timeout: float = 2.0,
) -> dict | None:
    ...
    try:
        return _request(port, request, timeout=timeout)
    except (OSError, ValueError, MalformedFrameError):
        return None
```

`socket.timeout` is a subclass of `OSError`, so any request — successful on
the daemon side or not — that takes longer than 2 seconds to answer over the
socket collapses to `None` here, which `mcp_server.py` then reports as "the
daemon is unavailable," even when the daemon process is alive and correctly
mid-request. This is a client-side false negative, not evidence the daemon
crashed or hung.

`structural_search` is the one tool in `DISPATCH_TABLE` that declares a
`files` parameter, which `dispatch.py::_call_tool` fills by reading files
off disk **synchronously, on the same connection-handling thread, before
the tool function (and its tree-sitter query) ever runs**:

```python
if "files" in sig_params:
    repo_root = resolve_repo_root(repo_path)
    kwargs["files"] = _read_source_files(repo_root, params.get("path_glob"))
```

Critically, this call passes no `is_ignored` predicate, so `_read_source_files`
defaults to "nothing ignored" (its own docstring, `dispatch.py:153-163`,
names this explicitly: *"this function's other caller (structural_search's
live disk-read seam in `_call_tool`, which was never part of that grilling
decision) is unaffected unless a future change explicitly opts it in
too."*) — a deliberately-deferred gap, confirmed by the code, not an
oversight. Only the dot-prefixed-directory prune applies
(`dirnames[:] = [d for d in dirnames if not d.startswith(".")]`); any
non-dot-prefixed directory a repo's own `.gitignore` excludes — vendored
dependencies, build output, a `venv`/`node_modules`-style directory without
a leading dot — is walked and read in full on **every single
`structural_search` call**, unlike every other repo-walking call site in
this codebase:

```python
def walk_repo(repo_root: str) -> Iterable[tuple[str, str]]:
    is_ignored = ignore.get_ignore_matcher(repo_root).matches
    return _read_source_files(repo_root, path_glob=None, is_ignored=is_ignored).items()
```

`walk_repo` (used by `scan.py`, `bootstrap.py`, `enrichment_scheduler.py`,
`lsp_enrichment.py` — i.e. every part of the real indexing pipeline) always
wires the repo's `ignore.get_ignore_matcher`; `_call_tool`'s injection for
`structural_search` is the sole caller that doesn't.

Put together: a repo with any sizeable ignorable content (or simply running
on a filesystem with slower per-file I/O — this development environment is
WSL2, whose cross-filesystem I/O for many small reads is measurably slower
than native Linux) makes a single `structural_search` call's disk-read seam
plausibly exceed 2 seconds even though the daemon is healthy and working
correctly — and the fixed, uniform client timeout has no way to
distinguish "daemon is down" from "this one disk-I/O-bound method is still
working." This uniquely explains why only `structural_search`, of all ten
tools, shows this symptom: the other nine are pure SQLite queries against
already-open indexes, with no comparable per-call disk-read cost.

## Locked decisions

1. **Primary fix: apply the existing ignore-matcher to `_call_tool`'s
   `files`-injection walk**, exactly like `walk_repo` already does. This
   closes the gap `_read_source_files`'s own docstring names as deferred
   future work, and is the correct behavior on its own independent merits
   (a caller searching "their repo" should get the same tracked-file scope
   every other ACIE tool already respects, not vendored/build noise) as
   well as being the dominant fix for the excess I/O volume driving the
   timeout:

   ```python
   if "files" in sig_params:
       repo_root = resolve_repo_root(repo_path)
       is_ignored = ignore.get_ignore_matcher(repo_root).matches
       kwargs["files"] = _read_source_files(repo_root, params.get("path_glob"), is_ignored=is_ignored)
   ```

   Do **not** change `_read_source_files`'s own default (`is_ignored=None`
   stays the function's default for any other/future caller) — only this
   one call site changes.

2. **Defense in depth: give `request_daemon` a per-method timeout override,
   and use a longer timeout specifically for `structural_search`.** Even
   after decision 1, a large `.py`-only monorepo with nothing to
   ignore could still legitimately take longer than 2 seconds to walk and
   parse than the nine SQLite-only methods need. Add a `timeout` keyword to
   `_daemon_tool`'s call into `request_daemon` (the function signature
   already accepts one — this just means the caller stops relying on the
   default), and thread a per-method value through `create_mcp_server`/
   `_daemon_tool` in `mcp_server.py`. Concretely:
   - Add a module-level mapping in `mcp_server.py`,
     e.g. `_METHOD_TIMEOUTS: dict[str, float] = {"structural_search": 10.0}`,
     read via `_METHOD_TIMEOUTS.get(method, 2.0)` (2.0 stays the default for
     every other method — no behavior change for them).
   - This is a fixed daemon-side constant, not new user-facing
     configuration surface — matches this codebase's "don't build
     speculative configurability ahead of an observed need" norm (see
     `errors.py`'s own docstring on only-add-what's-observed). If a repo is
     still large enough after decision 1 to need more than 10 seconds,
     that's a new, distinct finding for a future spec, not something to
     anticipate here.

3. **No change to `structural_search`'s own function signature, return
   shape, or the tree-sitter query logic** — this spec is entirely about
   the daemon/client transport layer's handling of one tool's already-
   correct disk-read seam, not the tool's search semantics.

4. **No change to `DAEMON_UNAVAILABLE`'s error code or message text** for
   the case where the daemon genuinely is down — this spec does not
   introduce a new error code for "slow but working"; decisions 1-2 are
   aimed at making that case not happen in practice for a `.scm` query that
   itself parses fine, rather than distinguishing it after the fact.

## Test plan (write first, red before green)

Extend `tests/daemon/test_dispatch.py` (mirrors the existing
`test_dispatch_request_fills_structural_search_files_from_real_disk` /
`test_dispatch_request_honors_structural_search_path_glob_by_only_reading_matching_files`
fixture style already in that file, and the `_read_source_files`
ignore-predicate tests already proving the function supports
`is_ignored` — this spec only wires that existing, tested capability into
the one call site that doesn't use it yet):

- New test: given a repo fixture containing a `.gitignore` that excludes a
  non-dot-prefixed directory (e.g. `vendor/`), and a `.py` file inside that
  directory containing a trivially matchable pattern, a `structural_search`
  dispatch call's `files` mapping (or its returned matches, for an
  end-to-end assertion) must **not** include that file after this fix —
  currently it does. Follow the existing dot-directory-pruning test's
  fixture shape (`test_read_source_files_prunes_ignored_directories_instead_of_just_filtering_their_files`)
  but assert this through `dispatch_request`, not `_read_source_files`
  directly, since the defect is in the wiring between them, not in
  `_read_source_files` itself.
- Regression: the two existing structural_search dispatch tests (files from
  real disk, path_glob filtering) must keep passing unchanged for repos
  with no `.gitignore`d content — confirms this fix is additive scoping,
  not a behavior change for the common case.

New tests in `tests/test_mcp_server.py` (or `tests/daemon/test_client.py`,
whichever this repo's existing convention places daemon-request-timeout
tests in — check for the closest existing precedent before choosing):

- `_daemon_tool`'s wrapper for `method="structural_search"` calls
  `request_daemon` with the larger configured timeout (assert via a
  mocked/spied `request_daemon`, not a real slow socket — this is a unit
  test of the wiring, not a timing-dependent integration test).
- `_daemon_tool`'s wrapper for any other method (e.g. `"find_symbol"`)
  still calls `request_daemon` with the existing default timeout — proves
  decision 2 doesn't regress every other tool's transport behavior.

All new/changed tests must go red before the fix, green after (TDD, not
retrofitted assertions). Do not add a real multi-second sleep to any test
to "prove" the timeout fires — that's a flaky, slow anti-pattern; test the
timeout *value* that gets threaded through, not the live socket behavior
under an artificial delay.

## Files to touch

- `src/acie/daemon/dispatch.py` — wire `ignore.get_ignore_matcher` into
  `_call_tool`'s `files`-injection branch (decision 1). `ignore` is already
  imported in this module.
- `src/acie/mcp_server.py` — add `_METHOD_TIMEOUTS`, thread a per-method
  timeout into the `request_daemon` call inside `_daemon_tool` (decision 2).
- `src/acie/daemon/client.py` — no signature change needed; `request_daemon`
  already accepts a `timeout` keyword. Confirm this while implementing; only
  touch this file if the existing signature turns out not to suffice.
- `tests/daemon/test_dispatch.py` — new ignore-scoping test (Test Plan).
- `tests/test_mcp_server.py` (or the closer-precedent location found while
  implementing) — new per-method timeout wiring tests (Test Plan).
- No change to `src/acie/tools/structural_search.py` itself (decision 3).

## Verification note

Run `.venv/bin/python -m pytest -q` (the project's actual venv — plain
`python3 -m pytest` fails to collect, `acie` isn't installed on system
Python) before and after. Baseline confirmed freshly at spec-write time
(2026-09-07, on current master, commit `2c0b553`): **849 passed, 0
failed** — clean. Note `MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md` recorded
2 failures at its own spec-write time
(`tests/daemon/test_runtime.py::test_runtime_isolates_divergent_worktree_indexes_and_storage`
and `tests/test_cli.py::test_daemon_stop_actually_terminates_the_os_process`,
both OS-process/worktree-runtime tests) that did not reproduce in this
run — treat them as possibly flaky/environment-sensitive rather than fixed
or newly-passing by design. If either reappears during this spec's own
verification runs, do not treat it as caused by this spec's changes (both
are unrelated to daemon transport/dispatch file-scoping); confirm the
suite is still clean of *new* failures beyond that pair.
