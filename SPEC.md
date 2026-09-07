# SPEC: `architecture(root=<absolute path>)` must not silently return empty

Status: LOCKED — ready for OMP/Luna implementation (TDD, red-green-refactor).
Source finding: SALTMDB memory `5021d1a9` (2026-09-05, re-confirmed unchanged
by three subsequent sweeps through `8a1a0a7b`, 2026-09-08). ACIE Full Tool
Sweep, 2026-09-08.

## 1. Problem statement

`architecture(root=..., ...)` (`src/acie/tools/architecture.py`) scopes its
result to files under `root` via `_in_scope`, a plain path-prefix check
against every symbol's `.path` — and every `.path` in ACIE's storage layer is
repo-relative (e.g. `"src/saltmdb/domain/services/relation_service.py"`,
never an absolute filesystem path). Passing an **absolute** path as `root` —
including the exact `repo_root` string `acie scan --json` itself echoes back,
or any absolute prefix of it — therefore matches zero symbols and returns a
schema-valid, silently-empty `{"nodes": [], "edges": [], ...}`, indistinguishable
from "this scope genuinely has no files in it" (already a legitimate, tested
outcome — see `test_root_matching_no_files_returns_an_empty_view_not_an_error`
in `tests/tools/test_architecture.py`).

Every other tool that takes a path-shaped argument (`find_symbol`,
`find_references`, `get_definition`, `graph`, `impact_analysis`,
`affected_tests`, `list_imports`) takes symbol IDs or paths already in the
repo-relative convention and does not exhibit this. `architecture`'s `root`
is the one place doing its own separate, unvalidated scope check.

## 2. Root cause

`architecture()` never normalizes or validates `root` before using it as a
raw string-prefix filter (`_in_scope`, `src/acie/tools/architecture.py:513-517`).
An absolute path is neither rejected nor converted — it is silently treated
as a valid-but-non-matching prefix.

## 3. Fix

`architecture()` already receives `repo_root: str | None` as a real
parameter (added for C5 layering, `src/acie/tools/architecture.py:316`) —
dispatch.py already resolves and injects it on every request today (the same
seam `structural_search`'s `files` uses; excluded from the public MCP schema
by `_DAEMON_INJECTED_PARAMETERS`, per this module's own C5 docstring section).
No new plumbing is needed: this fix is confined to `architecture()`'s own
function body.

Reuse the existing containment helper `acie.repo_id.to_repo_relative`
(already used by `staleness.py` for the identical "caller-supplied path,
absolute or relative, must resolve safely against `repo_root`" job — do not
write a second implementation of this, per Coding Standards rule 2/4) to
normalize `root` immediately after the existing `granularity`/`node_cap`
validation, before `root` is used anywhere else in the function:

```python
from acie.repo_id import to_repo_relative

...

if root is not None and os.path.isabs(root):
    if repo_root is None:
        raise InvalidArgumentError(
            "root must be repo-relative: an absolute path was given but no "
            "repo_root is available to resolve it against"
        )
    relative_root = to_repo_relative(root, repo_root)
    if relative_root is None:
        raise InvalidArgumentError(f"root {root!r} is outside repo_root {repo_root!r}")
    root = None if relative_root == "." else relative_root
```

Notes on exact behavior this produces:

- `root == repo_root` (the exact string `acie scan --json` echoes back, the
  case that first surfaced this bug) → `to_repo_relative` returns `"."` →
  normalized to `root = None`, i.e. whole-repo scope. This matches what a
  caller passing back its own `repo_root` obviously means.
- `root` is an absolute path strictly under `repo_root`
  (e.g. `f"{repo_root}/src/saltmdb"`) → normalized to the equivalent
  repo-relative string (`"src/saltmdb"`), then flows into `_in_scope`
  completely unchanged from today's relative-root path — no other code in
  `architecture()` needs to know this normalization happened.
- `root` is absolute but **not** under `repo_root` (a genuine caller mistake —
  a different repo's path, a typo) → `INVALID_ARGUMENT`, naming both the bad
  `root` and the `repo_root` it was checked against, instead of a silent
  empty result.
- `root` is absolute and `repo_root` was never supplied at all (a direct,
  non-dispatch caller — `architecture()` is documented as "a plain pure
  function" per its own C5 docstring section) → `INVALID_ARGUMENT` explaining
  why, instead of a silent empty result. This is a **behavior change** for
  any such direct caller, but strictly a better one: silent-wrong becomes
  explicit-and-actionable, matching every other validation in this tool
  (granularity, node_cap) and the sweep's own standard ("every enum/bound
  check ... raises the correct typed error with an actionable message").
- `root` already repo-relative (the overwhelmingly common case, and every
  existing test's case) → `os.path.isabs(root)` is `False`, this whole block
  is skipped, zero behavior change.
- `root is None` → skipped entirely, zero behavior change (matches
  `_in_scope`'s own existing `root is None` short-circuit).

`os` is not currently imported in `architecture.py` — add the import (stdlib,
no new dependency).

## 4. Why this shape, not the alternatives

Memory `5021d1a9`'s own two suggested directions were "normalize an absolute
root that's a prefix of repo_root down to relative" or "return an explicit
error/empty-with-reason when root doesn't match any indexed path prefix."
This spec locks **both, split by case**: normalize when it's unambiguously
resolvable (repo_root known, root genuinely under it), error otherwise
(repo_root unknown, or root escapes it) — silently guessing in the second
case would just move the silent-wrong failure mode rather than closing it.

Reusing `to_repo_relative` rather than hand-rolling a second prefix/relpath
check is a direct application of Coding Standards rule 4 (extend/reuse over
a near-duplicate second helper) — it already exists, is already unit-tested
(`tests/test_repo_id.py`), and already encodes the exact "reject anything
that would escape repo_root, including a `..`-walking relative path" safety
property this fix also wants (relevant if a future caller passes a relative
`root` containing `..` — out of today's reported bug, but free correctness
from reuse rather than a narrower hand-rolled check).

## 5. Explicitly out of scope

- Any change to `_in_scope`, `_compute_file_edges`, `_file_granularity`,
  `_package_granularity`, or any other `architecture()` internals — this fix
  is entirely a pre-filter on `root` before those run.
- Any change to how `repo_root` itself is resolved/injected by dispatch.py —
  it already exists and is already correct for this purpose (proven by C5's
  layering feature already depending on it).
- The tier-4/position-mode tombstoning bug (memory `daa3157f`) — separate
  spec.

## 6. Required tests (write first, red before green)

Add to `tests/tools/test_architecture.py`, near the existing
`test_root_scopes_nodes_to_files_under_that_path_prefix` /
`test_root_none_includes_every_file_in_the_repo` /
`test_root_matching_no_files_returns_an_empty_view_not_an_error` group. Use
the file's existing `_stores()`/`_index()` helpers; use `tmp_path` for a real
filesystem `repo_root` string exactly as the `repo_root=`-taking layering
tests further down this same file already do (e.g.
`test_repo_root_supplied_but_no_acie_config_layering_is_disabled`).

1. `test_absolute_root_equal_to_repo_root_scopes_to_the_whole_repo`
   — index files under two different subdirectories with no shared prefix,
   call `architecture(..., root=str(tmp_path), repo_root=str(tmp_path))` where
   `root` is the literal `repo_root` string. Assert every indexed file
   appears in `nodes` (i.e. behaves identically to `root=None`) — this is the
   exact case that first surfaced the bug (`acie scan --json`'s own echoed
   `repo_root`).
2. `test_absolute_root_under_repo_root_is_normalized_to_the_equivalent_relative_scope`
   — index `pkg/sub/a.py` and `pkg/other/c.py` under a `tmp_path` repo_root,
   call with `root=f"{tmp_path}/pkg/sub"`, `repo_root=str(tmp_path)`. Assert
   the result is identical in `nodes`/`edges` to the existing relative-root
   test's `root="pkg/sub"` call — i.e. it must pass the *same*
   prefix-boundary assertion `test_root_scopes_nodes_to_files_under_that_path_prefix`
   already makes (a sibling directory like `pkg/subx` must not leak in).
3. `test_absolute_root_outside_repo_root_raises_invalid_argument_error`
   — `root` an absolute path with no relation to `repo_root` (e.g. a
   different `tmp_path`-style directory). Assert `InvalidArgumentError` is
   raised, and the message names both the given `root` and the `repo_root`
   it was checked against (don't just assert *an* error — assert the message
   is actually actionable, matching this sweep's own bar for every other
   tool's validation).
4. `test_absolute_root_with_no_repo_root_supplied_raises_invalid_argument_error`
   — `root=str(tmp_path)` or any absolute path, `repo_root` omitted
   (defaults to `None`, the direct-caller case). Assert
   `InvalidArgumentError`, not a silent empty result.
5. `test_relative_root_behavior_is_unchanged_when_repo_root_is_also_supplied`
   — repeat `test_root_scopes_nodes_to_files_under_that_path_prefix`'s exact
   setup but also pass `repo_root=str(tmp_path)` alongside the existing
   relative `root="pkg/sub"`. Assert identical output to the existing
   test — proves the new normalization block is a true no-op whenever `root`
   isn't absolute, even with `repo_root` present.
6. `test_root_none_is_unaffected_by_repo_root_being_supplied`
   — `root=None`, `repo_root=str(tmp_path)`. Assert identical to the
   existing `test_root_none_includes_every_file_in_the_repo` output — proves
   the new block's `root is not None` guard is correct and doesn't
   accidentally fire for the whole-repo-scope case.

Run the repo's actual test command (`uv run pytest`, or whatever
`CONTRIBUTING`/CI documents — check before assuming) as the mandatory final
step; paste real failures, fix, re-run, never declare done on assumed
correctness (Coding Standards rule 18).

## 7. Non-goals / do not touch while here

- Do not touch `dispatch.py`'s existing `repo_root` injection/exclusion-set
  wiring — it is already correct and already tested
  (`tests/test_mcp_server.py`'s public-schema assertion for `architecture`).
- Do not add `os.path.isabs` handling to any other tool's `root`/`file`
  parameter as part of this change — `find_references`, `list_imports`,
  etc. already go through `to_repo_relative` or their own established path
  handling; this spec is scoped to `architecture()` alone, which is the one
  proven gap.
- Do not reorder `architecture.py`'s existing imports; add the new `os`
  import and `to_repo_relative` import in the existing import block, in the
  existing alphabetical/grouping style already used there.

## 8. Definition of done

- All 6 new tests pass; full existing suite (including every existing
  `test_architecture.py` case, byte-for-byte unchanged in assertions) still
  passes with no regressions.
- `git diff` shows changes confined to `src/acie/tools/architecture.py` and
  `tests/tools/test_architecture.py`. No incidental changes elsewhere.
