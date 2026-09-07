# structural_search's `files` Parameter — Docstring Contradicts Actual MCP Behavior

Status: spec-and-plan only, written for external implementation (same role
split as the D/H-series and MCP-*-SPEC docs — this document was **not**
implemented in the session that wrote it; a fresh session/agent should
implement it, and another fresh session should review the resulting diff
against this spec before commit).

Triggered by the 2026-09-07 third same-day ACIE reliability sweep (SALTMDB
memory `8791b995`). That sweep found `structural_search` now returns real,
byte-accurate results (the earlier `INVALID_PATTERN`/`DAEMON_UNAVAILABLE`
failures are separately fixed — see `STRUCTURAL-SEARCH-RELIABILITY-SPEC.md`,
merged at commit `45c6b14`) — but a follow-up test showed the tool's `files`
parameter, which its own docstring calls "a caller-supplied `{path:
source_text}` mapping," is completely ignored: a fake nonexistent path with
unique marker content never appeared in results, and `files={}` (empty)
still returned a real file's actual on-disk content. Companion memory
`49a1861c` flagged that this hadn't yet been tested against the tool's real
documented use case (a real indexed path with *modified* content, not a
fake path or an empty dict) — that follow-up test was run directly against
this repo's own source this session (self-hosted dogfooding, see below) as
part of writing this spec, not skipped.

This spec touches only `src/acie/tools/structural_search.py` and
`tests/test_mcp_server.py`. Companion spec `FIND-SYMBOL-KIND-VALIDATION-SPEC.md`
fixes a separate, unrelated finding from the same sweep (`find_symbol`'s
`kind` filter). The two specs touch entirely non-overlapping files and can
be implemented and reviewed independently, in parallel, in either order.

## Root cause, read directly from source this session (not re-guessed from
the report)

`structural_search`'s pure function (`src/acie/tools/structural_search.py`)
is not the bug — it correctly and exclusively iterates
`files.items()` (line 103) with no disk I/O of its own; that part of the
tool works exactly as its docstring describes, and always has.

The bug is two daemon-layer decisions, both confirmed by direct read, that
together make `files` unreachable and unoverridable for any real MCP
caller, while the docstring copied onto the exposed tool never says so:

1. **`files` is not part of the MCP-exposed schema at all.**
   `src/acie/mcp_server.py`:

   ```python
   _DAEMON_INJECTED_PARAMETERS = {
       "symbol_store", "relation_store", "index_meta_store", "files", "observed_at", "repo_root",
   }
   ...
   public_parameters = [
       parameter
       for name, parameter in inspect.signature(tool).parameters.items()
       if name not in _DAEMON_INJECTED_PARAMETERS
   ]
   ```

   `call.__signature__` — what any well-behaved MCP client actually reads to
   build its tool-call schema — is built from `public_parameters`, which
   excludes `files` by construction. Confirmed live this session: the
   `structural_search` tool schema visible to this very session lists only
   `cursor, full, limit, path_glob, pattern` — no `files`. A caller using
   the real schema cannot supply `files` at all.

2. **Even a caller who bypasses the schema and injects `files` into the raw
   RPC `params` anyway (as the sweep did) still gets it discarded.**
   `src/acie/daemon/dispatch.py::_call_tool`:

   ```python
   kwargs = dict(params)
   ...
   if "files" in sig_params:
       repo_root = resolve_repo_root(repo_path)
       is_ignored = ignore.get_ignore_matcher(repo_root).matches
       kwargs["files"] = _read_source_files(repo_root, params.get("path_glob"), is_ignored=is_ignored)
   ```

   Whatever `kwargs["files"]` was set to by `dict(params)` (including a
   caller-supplied value) is unconditionally overwritten with a fresh,
   ignore-scoped disk walk. This is documented as intentional daemon
   behavior in the module docstring's own comment: *"DAEMON.md
   'structural_search's disk-I/O seam': dispatch itself reads matching
   files off disk ... "* — this dispatch-level design was a deliberate
   choice (confirmed via the `_DAEMON_INJECTED_PARAMETERS`/`public_parameters`
   split above, not an accident), not a regression.

**Self-hosted confirmation this session** (per companion memory `49a1861c`'s
open follow-up — the actual documented use case, not just a fake path):
called `dispatch_request` directly against this repo with
`params={"pattern": "(function_definition) @fn", "path_glob":
"src/acie/tools/structural_search.py"}` and a `files` entry for that exact
real, indexed path containing deliberately different content (a single
uniquely-named stub function replacing the real body). Result: the match
came back from the real on-disk `structural_search`/`_build_match`/
`_ordering_key`/`_render` functions, not the stub — confirming the ignoring
is total, not just for fake/empty inputs. `49a1861c`'s open question is now
closed: this is not a narrower bug (e.g. "only ignored for untracked
paths") — `files` is unconditionally daemon-controlled for every real MCP
caller, full stop.

## Design decision (confirmed with the user this session)

Two ways to close this gap were on the table: (a) make the exposed MCP tool
actually honor a caller-supplied `files` override (matching the pure
function's original documented intent — checking modified-but-unsaved or
synthetic source before it's saved to disk), or (b) leave the daemon's
"always search live, ignore-scoped on-disk content" behavior exactly as it
is today and fix the docstring to describe it accurately.

**Decision: (b), docstring-only.** No behavior change to
`dispatch.py`/`mcp_server.py`/`structural_search.py`'s logic. Rationale:
this matches how the other 5 daemon-injected parameters already work for
every one of the other 9 tools — none of them let a caller override
daemon-controlled state (`symbol_store`, `relation_store`,
`index_meta_store`, `observed_at`, `repo_root` are all always
daemon-supplied, never caller-settable) — so today's `structural_search`
behavior is actually the consistent case, and the docstring is the outlier
that's wrong. Adding a real caller-content-override capability would be new
scope (arbitrary source-text injection into the daemon, a capability no
other tool has) that the live report never asked for and that isn't needed
to close the finding — the finding is "the tool silently does something
different from what it claims," which a docstring fix fully resolves.

## Locked decisions

1. **Fix location: `structural_search`'s own function docstring** in
   `src/acie/tools/structural_search.py` — same "one canonical docstring,
   no separate out-of-band description" precedent
   `MCP-TOOL-DOCSTRINGS-SPEC.md` already established (`mcp_server.py:60`
   already prefers `tool.__doc__` verbatim; no change needed there or to
   `_DAEMON_INJECTED_PARAMETERS`/`public_parameters`).

2. **Replace the `files` sentence.** Current text (added by
   `MCP-TOOL-DOCSTRINGS-SPEC.md`, itself accurately describing the *pure
   function's* contract but not the *exposed MCP tool's* actual behavior —
   this spec corrects that gap, it doesn't re-litigate that spec's own
   work):

   > `files` is a caller-supplied `{path: source_text}` mapping because
   > ACIE's IR stores no source text to query.

   New text must state, plainly, for the caller who will only ever see the
   MCP-exposed tool (not the internal pure function):
   - `files` is **not** a parameter this tool accepts — it is not part of
     the tool's schema.
   - The daemon always searches the live, `.gitignore`-scoped on-disk
     Python source of the repository at call time, matching the same file
     set `find_symbol`/`list_imports`/etc. index from — there is no way to
     search modified-but-unsaved or synthetic content through this
     interface.
   - `path_glob` narrows which on-disk files are walked (this part of the
     existing docstring is already accurate and unchanged).

   Exact wording is an implementation detail, but must not use the phrase
   "caller-supplied" for `files` in the exposed docstring, since that is
   precisely the claim this spec disproves.

3. **The pure function's own internal docstring (the parameter-level
   description inside the function body, and the module-level docstring at
   the top of `structural_search.py`) stay accurate as they are** — they
   correctly describe the pure function `structural_search(files, ...)`
   itself, which genuinely does take and use caller-supplied `files`. This
   spec only corrects the text that becomes the *MCP-exposed tool's*
   caller-facing description; per `MCP-TOOL-DOCSTRINGS-SPEC.md` decision 4,
   module/internal-design docstrings are a distinct artifact from the
   function's own top-level docstring, which is the one `mcp_server.py`
   copies verbatim as `call.__doc__`. Concretely: only the top-level
   `structural_search()` function's docstring changes; do not touch the
   module docstring at the top of the file, and do not touch
   `_build_match`/`_ordering_key`/`_render`'s docstrings.

4. **No change to `_DAEMON_INJECTED_PARAMETERS`, `public_parameters`,
   `_call_tool`'s files-injection, or `_read_source_files`.** This is a
   documentation-only fix, per the confirmed decision above.

## Test plan (write first, red before green)

New test in `tests/test_mcp_server.py`:

- Assert that `structural_search`'s exposed `call.__signature__` (built by
  `_daemon_tool`) does not include a `files` parameter — codifies the
  already-true "files is daemon-injected, not caller-settable" contract as
  a permanent regression guard, so a future refactor of
  `_DAEMON_INJECTED_PARAMETERS` can't silently reopen this gap unnoticed.
- Assert the exposed tool's docstring (`call.__doc__`) does **not** contain
  the substring `"caller-supplied"` in the `files`-describing sentence —
  simplest possible guard against this exact wrong claim recurring; check
  by asserting the new expected phrase (e.g. `"not a parameter"` or
  whatever exact wording is chosen) is present instead of a fragile
  negative-string-match alone.

New test in `tests/daemon/test_dispatch.py` (mirrors the existing
`test_dispatch_request_fills_structural_search_files_from_real_disk` style
already in that file — codifies the *behavior* this spec deliberately keeps
unchanged, as a permanent regression test against the exact scenario the
live sweep and this spec's own self-hosted confirmation both exercised):

- Given a real on-disk file and a `structural_search` dispatch request whose
  `params["files"]` supplies different (fabricated) content for that same
  path, assert the returned match reflects the real on-disk content, not
  the caller-supplied fabricated content — proves the daemon's "always
  live disk" behavior is intentional, tested, and stays intentional, not
  silently reintroduced as an accidental override path (or accidentally
  fixed into an override) by a future change to `_call_tool`.

All new/changed tests must go red before the fix, green after (TDD — the
docstring-content test is red before decision 2's text change; the
signature test and the dispatch-behavior test should already be green
today since they codify existing behavior, but write them anyway per this
project's "turn every live-discovered contract gap into a permanent test"
norm, same as `MCP-TOOL-DOCSTRINGS-SPEC.md` decision 5 and
`MCP-STRUCTURED-INPUT-VALIDATION-SPEC.md`'s test plan both already did for
their own findings).

## Files to touch

- `src/acie/tools/structural_search.py` — rewrite the `files` sentence in
  the top-level `structural_search()` function's docstring only (Decisions
  1-3).
- `tests/test_mcp_server.py` — new signature/docstring-content regression
  tests (Test Plan).
- `tests/daemon/test_dispatch.py` — new "files is ignored by design"
  regression test (Test Plan).
- No change to `src/acie/daemon/dispatch.py`, `src/acie/mcp_server.py`, or
  any other tool file (Decision 4).

## Verification note

Run `.venv/bin/python -m pytest -q` (the project's actual venv — plain
`python3 -m pytest` fails to collect, `acie` isn't installed on system
Python) before and after, from this spec's own dedicated worktree. Confirm
the suite's failure count doesn't grow beyond whatever pre-existing
baseline is observed fresh at implementation time (recent baselines on
master have been clean at 852-855 passed; the `test_daemon_stop_actually_
terminates_the_os_process`/`test_runtime_isolates_divergent_worktree_
indexes_and_storage` pair has intermittently appeared and disappeared
across sessions as pre-existing OS-process/worktree-runtime flakiness
unrelated to this spec's scope — if either reappears, don't treat it as
caused by this change).
